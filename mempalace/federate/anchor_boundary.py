"""
Anchor-boundary ingress for external assertions.

# What this module is

When a Finding arrives from another palace (via federation), the
substrate must absorb the claim without overwriting any local
memory. The original drawer/node remains read-only; the external
claim is attached as a *decoration* — a separate assertion node
with `asserter_is_external=True`, plus a derived link binding it
to the local memory it concerns.

This is the missing federation-ingress wiring the previous turn
identified.

# The "anchor boundary" concept

A local memory is an anchor — fixed, content-addressed, signed by
the operator's own attestation chain. External claims attach to
the anchor without modifying it. The boundary between
"read-only anchor" and "decoratable surface" is dynamic:

  - Initially, the boundary tightly hugs the anchor: only the most
    recent / most confident decorations are surfaced when reading.
  - As the operator queries memory and engages with the
    decorations (clarifies, corrects, accepts), the boundary
    stretches: more decorations are surfaced, in richer context.
  - The original anchor never changes. Stretching the boundary
    just expands what's *read together with* the anchor.

This is structurally similar to RAM-decorated ROM in computer
architecture: the ROM (anchor) is immutable; the RAM
(decorations) is mutable; reading the address yields the ROM
value plus whatever RAM decorations have accumulated.

# Boundary mechanics

The boundary is a per-(anchor, query-context) decision: which
decorations to include when this anchor is read. It "stretches"
through reinforcement learning — but the version shipped here is
static rules:

  - Confidence threshold: only decorations above CONF_THRESHOLD.
  - Recency window: prefer recent over old (decay per
    DECORATION_HALF_LIFE).
  - Asserter-trust: a per-palace_id trust score weights the
    decoration; trust is updated when the operator confirms or
    rejects the decoration.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md (read-only anchors),
HANDLES_DESIGN.md (decoration accumulation).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.identifiers import SELF_ENTITY_ID
from ..schema.kinds import DerivationType, EdgeKind, NodeKind
from ..views import current as views
from ..views.graph import AssertionAsserter, Graph

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================


DEFAULT_CONF_THRESHOLD = 0.4
"""Decorations below this confidence are not surfaced when reading the
anchor. Adjustable per query."""

DEFAULT_DECORATION_HALF_LIFE_MS = 90 * 24 * 60 * 60 * 1000
"""90 days. After one half-life, a decoration's effective weight is
halved."""

DEFAULT_PER_ASSERTER_TRUST = 0.5
"""Initial trust score for an unfamiliar palace_id. Operator
confirmations push it up; corrections push it down."""


# =============================================================================
# Decoration shape
# =============================================================================


@dataclass(frozen=True)
class ExternalDecoration:
    """One external claim attached to an anchor.

    Read by query helpers; immutable. The substrate stores
    decorations as assertion nodes with `asserter_is_external=True`;
    `from_assertion_node` builds this shape from a NodeState.
    """

    asserter_palace_id: str
    """Who said this."""

    predicate: str
    """What was said. Matches the assertion node's predicate."""

    object_id: str
    """The other endpoint of the assertion."""

    confidence: float
    asserted_at_ms: int
    assertion_node_id: str

    signature_hex: str = ""
    """Caller can verify before trusting. Verification is the caller's
    responsibility (this module just surfaces the decoration)."""

    @classmethod
    def from_assertion_node(
        cls, node: Any,  # NodeState; Any keeps this module decoupled
    ) -> "ExternalDecoration | None":
        """Build a decoration view of a NodeState. Returns None if
        the node isn't an external-assertion node."""
        if node.node_kind != NodeKind.ASSERTION.value:
            return None
        if not node.properties.get("asserter_is_external"):
            return None
        # Find the asserted_object edge to get the object_id
        edges = views.outgoing_edges(node.node_id, kind=EdgeKind.ASSERTED_OBJECT)
        object_id = edges[0].target_node_id if edges else ""
        return cls(
            asserter_palace_id=node.properties.get(
                "asserter_palace_id", "",
            ),
            predicate=node.properties.get("predicate", ""),
            object_id=object_id,
            confidence=float(node.properties.get("confidence", 0.0)),
            asserted_at_ms=int(node.properties.get("asserted_at_ms", 0)),
            assertion_node_id=node.node_id,
            signature_hex=node.properties.get(
                "asserter_signature_hex", "",
            ),
        )


# =============================================================================
# Trust scoring
# =============================================================================


@dataclass
class AsserterTrustStore:
    """Per-palace_id trust scores. Lives in-process for now;
    production persists to its own log scope.

    A trust score is a [0, 1] value:
      0   = treat the asserter as adversarial
      0.5 = unknown / default
      1   = treat the asserter as fully trusted
    """

    _scores: dict[str, float] = field(default_factory=dict)

    def get(self, palace_id: str) -> float:
        return self._scores.get(palace_id, DEFAULT_PER_ASSERTER_TRUST)

    def confirm(self, palace_id: str, *, weight: float = 0.1) -> None:
        """Operator confirmed a decoration from this asserter. Bump
        trust toward 1.0 by `weight`."""
        cur = self.get(palace_id)
        self._scores[palace_id] = min(1.0, cur + weight)

    def reject(self, palace_id: str, *, weight: float = 0.2) -> None:
        """Operator rejected a decoration. Push trust toward 0.0.
        Rejections weigh more than confirmations because false
        positives are more costly than false negatives."""
        cur = self.get(palace_id)
        self._scores[palace_id] = max(0.0, cur - weight)

    def reset(self, palace_id: str) -> None:
        self._scores.pop(palace_id, None)


_TRUST_STORE: AsserterTrustStore | None = None


def get_default_trust_store() -> AsserterTrustStore:
    global _TRUST_STORE
    if _TRUST_STORE is None:
        _TRUST_STORE = AsserterTrustStore()
    return _TRUST_STORE


def reset_default_trust_store() -> None:
    global _TRUST_STORE
    _TRUST_STORE = None


# =============================================================================
# Boundary computation
# =============================================================================


def decoration_weight(
    decoration: ExternalDecoration,
    *,
    now_ms: int | None = None,
    trust_store: AsserterTrustStore | None = None,
    half_life_ms: int = DEFAULT_DECORATION_HALF_LIFE_MS,
) -> float:
    """Compute the effective surfacing weight of a decoration.

    weight = confidence * trust * recency_decay

    All in [0, 1]. Decorations with higher weight surface earlier in
    a stretched-boundary read.
    """
    now_ms = now_ms or int(time.time() * 1000)
    trust_store = trust_store or get_default_trust_store()

    age_ms = max(0, now_ms - decoration.asserted_at_ms)
    # Exponential decay: weight halves every half_life_ms
    if half_life_ms > 0:
        recency = 0.5 ** (age_ms / half_life_ms)
    else:
        recency = 1.0

    trust = trust_store.get(decoration.asserter_palace_id)
    return decoration.confidence * trust * recency


def decorations_for_anchor(
    anchor_node_id: str,
    *,
    confidence_threshold: float = DEFAULT_CONF_THRESHOLD,
    asserter_palace_id: str | None = None,
    sort_by_weight: bool = True,
    now_ms: int | None = None,
) -> list[ExternalDecoration]:
    """Return external decorations attached to an anchor.

    "Anchor" here means the local memory the decoration is *about*.
    An assertion's subject is the anchor; this function reverses
    the asserted_subject edge.

    Args:
      anchor_node_id: typically SELF_ENTITY_ID for "what others
        say about me", or any local node for "what others say
        about this thing".
      confidence_threshold: drop decorations with confidence <
        this value.
      asserter_palace_id: if non-None, restrict to one asserter.
      sort_by_weight: if True, return strongest decorations first.
    """
    edges = views.incoming_edges(anchor_node_id, kind=EdgeKind.ASSERTED_SUBJECT)
    decorations: list[ExternalDecoration] = []
    for edge in edges:
        node = views.current_node(edge.source_node_id)
        if node is None:
            continue
        dec = ExternalDecoration.from_assertion_node(node)
        if dec is None:
            continue
        if dec.confidence < confidence_threshold:
            continue
        if asserter_palace_id is not None and \
                dec.asserter_palace_id != asserter_palace_id:
            continue
        decorations.append(dec)

    if sort_by_weight:
        decorations.sort(
            key=lambda d: decoration_weight(d, now_ms=now_ms),
            reverse=True,
        )
    return decorations


# =============================================================================
# Federation ingress
# =============================================================================


def ingest_external_assertion(
    *,
    asserter_palace_id: str,
    asserter_session_pubkey_hex: str,
    asserter_signature_hex: str,
    subject_id: str,
    predicate: str,
    object_id: str,
    confidence: float = 1.0,
    asserted_at_ms: int | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    log_client: LogClient | None = None,
) -> str:
    """Ingest one external assertion. Decorates the local subject
    without overwriting it.

    The caller is responsible for verifying `asserter_signature_hex`
    before invoking this function. Once invoked, the assertion is
    accepted into the substrate and decorates the subject.

    Returns the new assertion node id.
    """
    asserter = AssertionAsserter(
        palace_id=asserter_palace_id,
        session_pubkey_hex=asserter_session_pubkey_hex,
        signature_hex=asserter_signature_hex,
    )
    g = Graph(client=log_client) if log_client else Graph()
    aid = g.add_assertion(
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        confidence=confidence,
        valid_from_ms=valid_from_ms,
        valid_to_ms=valid_to_ms,
        derivation=DerivationType.INFERENCE,
        asserter=asserter,
    )
    logger.info(
        "ingested external assertion %s from palace=%s about subject=%s "
        "predicate=%s",
        aid, asserter_palace_id, subject_id, predicate,
    )
    return aid


__all__ = [
    "DEFAULT_CONF_THRESHOLD",
    "DEFAULT_DECORATION_HALF_LIFE_MS",
    "DEFAULT_PER_ASSERTER_TRUST",
    "AsserterTrustStore",
    "ExternalDecoration",
    "decoration_weight",
    "decorations_for_anchor",
    "get_default_trust_store",
    "ingest_external_assertion",
    "reset_default_trust_store",
]
