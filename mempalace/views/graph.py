"""
Typed graph accessor over master views.

The "filing-cabinet API" — provides high-level graph operations like
`create_period`, `add_assertion`, `add_event_to_period` that emit the
appropriate events. Callers don't manipulate nodes and edges directly;
they use the typed methods here.

Spec ref: Part 3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from . import current as views
from ..log.client import LogClient, get_default_client
from ..schema.events import (
    EdgeCreated, EdgeInvalidated, NodeCreated, NodePropertySet,
)

if TYPE_CHECKING:
    from ..log.client import BatchHandle
from ..schema.identifiers import (
    SELF_ENTITY_ID, make_assertion_id, make_edge_id, make_entity_id,
    make_event_id, make_period_id, make_recurrence_cluster_id, make_schema_id,
    make_theme_id,
)
from ..schema.kinds import (
    DerivationType, EdgeKind, EntityType, NodeKind, PeriodState, SchemaKind,
)


# =============================================================================
# AssertionAsserter — who made the claim
#
# Per the matching-layer alignment criterion: cross-palace mentions are the
# strongest alignment signal between two palaces. To make use of them, the
# substrate must distinguish:
#
#   - assertions the operator's own palace produced about its subject
#     (subject = SELF_ENTITY, asserter = self/empty)
#   - assertions another palace produced about this operator
#     (subject = SELF_ENTITY, asserter = other palace_id)
#   - assertions this palace produced about another person
#     (subject = some entity, asserter = self/empty)
#
# The signature/pubkey fields enable verifier-side authentication: when an
# external assertion arrives over federation, the receiver checks the
# signature before accepting the claim into its substrate.
#
# Spec ref: cross-palace alignment criterion + W3C VCDM issuer/subject
# distinction.
# =============================================================================


@dataclass(frozen=True)
class AssertionAsserter:
    """Who made an assertion. Optional — empty palace_id = self."""

    palace_id: str = ""
    """Empty string means "self-asserted by this palace." Otherwise,
    the palace_id of the third party that originated this claim."""

    session_pubkey_hex: str = ""
    """The asserter's session-key public half (hex). For verification
    of the signature. Required when palace_id is non-empty."""

    signature_hex: str = ""
    """Signature over the assertion content. The exact serialization
    is `subject_id||predicate||object_id||valid_from||valid_to`
    (UTF-8 concatenation, matching `_asserter_canonical_bytes`).
    Required when palace_id is non-empty; verified at receive time."""

    @property
    def is_self(self) -> bool:
        """True iff this is a self-assertion (palace_id empty)."""
        return self.palace_id == ""

    @property
    def is_external(self) -> bool:
        return not self.is_self


def _asserter_canonical_bytes(
    *,
    subject_id: str,
    predicate: str,
    object_id: str,
    valid_from_ms: int | None,
    valid_to_ms: int | None,
) -> bytes:
    """Canonical serialization of the assertion content for signing.

    Asserter signs over this byte string; verifier reconstructs the
    same bytes from the assertion fields and checks the signature.

    Order is fixed; null timestamps render as empty.
    """
    parts = [
        subject_id,
        predicate,
        object_id,
        "" if valid_from_ms is None else str(valid_from_ms),
        "" if valid_to_ms is None else str(valid_to_ms),
    ]
    return "\x00".join(parts).encode("utf-8")


# =============================================================================
# DrawerSpan — span-pointer provenance on derived_from edges
#
# Per R3 §9.3: substrate verification + span-pointer provenance.
# When an assertion is derived from a drawer, the `derived_from` edge
# carries a span indicating *which specific portion* of the drawer
# was the source. This unlocks two things:
#
#   1. Faithfulness scoring — compare the assertion text to the
#      actual substrate text at the span, not to the whole drawer.
#      Detects coherence-overwrite (assertions that drift from what
#      the drawer actually said).
#   2. User-review surfaces — show the operator "this assertion came
#      from drawer Y, lines 3-7" so they can audit the inference.
#
# Token offsets are the canonical representation (deterministic);
# line offsets and excerpts are convenience derivatives. All three
# are optional — the substrate gracefully degrades when only some
# are present.
# =============================================================================


@dataclass(frozen=True)
class DrawerSpan:
    """A span within a drawer indicating what content backed an
    inference."""

    start_token: int = 0
    """Token offset (0-indexed) at which the cited content starts."""

    end_token: int = 0
    """Token offset (exclusive) at which the cited content ends.
    end_token > start_token for a non-empty span."""

    start_line: int | None = None
    """Line number (1-indexed) for the span start. Convenience for
    user-review surfaces."""

    end_line: int | None = None

    excerpt: str = ""
    """First ~80 characters of the cited content. For debug, logging,
    and review-surface previews. Not load-bearing — recomputable
    from the drawer + span."""

    @property
    def is_empty(self) -> bool:
        return self.end_token <= self.start_token

    @property
    def token_count(self) -> int:
        return max(0, self.end_token - self.start_token)

    def to_edge_properties(self) -> dict[str, Any]:
        """Render as edge property fields. None values are omitted
        so the edge stays minimal when line info isn't available."""
        out: dict[str, Any] = {
            "span_start_token": self.start_token,
            "span_end_token": self.end_token,
        }
        if self.start_line is not None:
            out["span_start_line"] = self.start_line
        if self.end_line is not None:
            out["span_end_line"] = self.end_line
        if self.excerpt:
            # Cap excerpt length on storage so a malformed call can't
            # bloat the edge properties unbounded.
            out["span_excerpt"] = self.excerpt[:80]
        return out

    @classmethod
    def from_edge_properties(cls, props: dict[str, Any]) -> "DrawerSpan | None":
        """Reverse of `to_edge_properties`. Returns None if the
        properties don't carry a span (no `span_start_token` key)."""
        if "span_start_token" not in props:
            return None
        return cls(
            start_token=int(props.get("span_start_token", 0)),
            end_token=int(props.get("span_end_token", 0)),
            start_line=props.get("span_start_line"),
            end_line=props.get("span_end_line"),
            excerpt=str(props.get("span_excerpt", "")),
        )


class Graph:
    """High-level typed graph accessor.

    A thin wrapper that emits events through the LogClient and reads through
    the views module. All mutations go through this class so the event
    taxonomy is uniform.
    """

    def __init__(self, client: LogClient | None = None) -> None:
        self._client = client or get_default_client()

    # =========================================================================
    # Themes
    # =========================================================================

    def create_theme(
        self,
        name: str,
        parent_theme_id: str | None = None,
        importance: float = 0.5,
    ) -> str:
        theme_id = make_theme_id()
        ev = NodeCreated(
            node_id=theme_id,
            node_kind=NodeKind.THEME.value,
            properties={"name": name, "parent_theme": parent_theme_id},
            importance=importance,
            created_by="user",
        )
        self._client.append(ev)
        return theme_id

    # =========================================================================
    # Periods
    # =========================================================================

    def create_period(
        self,
        theme_id: str,
        name: str,
        started_at_ms: int,
        ended_at_ms: int | None = None,
        state: PeriodState = PeriodState.OPEN,
        precedence: int = 0,
        summary: str = "",
    ) -> str:
        period_id = make_period_id()
        ev = NodeCreated(
            node_id=period_id,
            node_kind=NodeKind.PERIOD.value,
            properties={
                "theme_id": theme_id,
                "name": name,
                "started_at": started_at_ms,
                "ended_at": ended_at_ms,
                "state": state.value,
                "precedence": precedence,
                "summary": summary,
            },
            importance=0.5,
            created_by="user",
        )
        self._client.append(ev)
        # Theme contains period
        self._add_edge(
            EdgeKind.CONTAINS, theme_id, period_id, derivation=DerivationType.OBSERVATION
        )
        return period_id

    def close_period(self, period_id: str, ended_at_ms: int) -> None:
        self._client.append(NodePropertySet(
            node_id=period_id, field_name="state", new_value=PeriodState.CLOSED.value,
        ))
        self._client.append(NodePropertySet(
            node_id=period_id, field_name="ended_at", new_value=ended_at_ms,
        ))

    def seal_period(self, period_id: str) -> None:
        self._client.append(NodePropertySet(
            node_id=period_id, field_name="state", new_value=PeriodState.SEALED.value,
        ))

    # =========================================================================
    # Events (the node kind, not log events)
    # =========================================================================

    def create_event(
        self,
        period_id: str,
        name: str,
        gist: str = "",
        occurred_at_ms: int | None = None,
        occurred_to_ms: int | None = None,
        importance: float = 0.5,
    ) -> str:
        event_node_id = make_event_id()
        ev = NodeCreated(
            node_id=event_node_id,
            node_kind=NodeKind.EVENT.value,
            properties={
                "period_id": period_id,
                "name": name,
                "gist": gist,
                "occurred_at": occurred_at_ms,
                "occurred_to": occurred_to_ms,
            },
            importance=importance,
            created_by="user",
        )
        self._client.append(ev)
        # Period contains event
        self._add_edge(EdgeKind.CONTAINS, period_id, event_node_id)
        return event_node_id

    def add_drawer_to_event(self, event_node_id: str, drawer_id: str) -> str:
        return self._add_edge(EdgeKind.CONTAINS, event_node_id, drawer_id)

    # =========================================================================
    # Entities
    # =========================================================================

    def create_entity(
        self,
        name: str,
        entity_type: EntityType | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        entity_id = make_entity_id()
        props = {"name": name, "attributes": attributes or {}}
        if entity_type is not None:
            props["entity_type"] = entity_type.value
        self._client.append(NodeCreated(
            node_id=entity_id,
            node_kind=NodeKind.ENTITY.value,
            properties=props,
            created_by="user",
        ))
        return entity_id

    def self(self) -> str:
        """The designated self-entity ID."""
        return SELF_ENTITY_ID

    # =========================================================================
    # Schemas
    # =========================================================================

    def create_schema(
        self,
        name: str,
        kind: SchemaKind,
        description: str = "",
        canonical: bool = False,
        canon_path: str | None = None,
    ) -> str:
        schema_id = make_schema_id()
        if canonical and not canon_path:
            raise ValueError("canonical schemas must have a canon_path")
        self._client.append(NodeCreated(
            node_id=schema_id,
            node_kind=NodeKind.SCHEMA.value,
            properties={
                "name": name,
                "schema_kind": kind.value,
                "description": description,
                "stability_score": 0.0,
                "coverage_score": 0.0,
            },
            canonical=canonical,
            canon_path=canon_path,
            created_by="user",
        ))
        return schema_id

    # =========================================================================
    # Assertions (the 8-part frame from R3 §1.3 — historically called
    # "triples", but the data model is richer than a triple)
    # =========================================================================

    def add_assertion(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        derived_from_drawers: list[str] | None = None,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        derivation: DerivationType = DerivationType.OBSERVATION,
        confidence: float = 1.0,
        *,
        asserter: AssertionAsserter | None = None,
        derived_from_spans: dict[str, DrawerSpan] | None = None,
    ) -> str:
        """Add an assertion to the graph.

        Creates an assertion node + asserted_subject/asserted_object
        edges + N derived_from edges (one per source drawer). All
        emitted events are framed as a single batch (consumer
        "graph.add_assertion") so a crash mid-write is recognizable
        as a torn batch and the partial assertion can be discarded on
        recovery.

        Per R3 §1.3, an assertion is the 8-part frame
        (subject, predicate, object, time, source, stance, confidence,
        provenance). Time is captured in `valid_from_ms`/`valid_to_ms`;
        provenance is the `derived_from_drawers` list; source/stance
        live on the assertion node's properties; confidence is the
        confidence parameter.

        Asserter (cross-palace mention support):
          When `asserter` is None or `asserter.is_self`, the assertion
          is treated as self-asserted by this palace (default).
          When `asserter` is non-empty, the assertion records that
          a third party (`asserter.palace_id`) made this claim. Used
          for cross-palace mentions; the matching layer's strongest
          alignment criterion. Verification of the asserter signature
          is the caller's responsibility (typically done at federation
          ingress, before calling add_assertion).

        Span-pointer provenance (R3 §9.3):
          When `derived_from_spans` is provided, each entry maps a
          drawer_id (which must also appear in `derived_from_drawers`)
          to a DrawerSpan indicating the precise token range used.
          Spans are persisted as properties on the corresponding
          `derived_from` edges. Drawers without spans get the standard
          drawer-level derived_from (no span properties); drawers with
          spans get the precise-range derived_from. This unlocks
          substrate-faithfulness scoring in `mempalace.retrieve.
          substrate_verification`.

        Returns the assertion node ID.
        """
        assertion_id = make_assertion_id()
        derived_drawers = list(derived_from_drawers or [])
        # 1 NodeCreated + 2 fixed edges + N derived_from edges
        expected = 3 + len(derived_drawers)

        # Asserter properties: live on the assertion node so any caller
        # reading the node sees provenance without an extra lookup.
        # asserted_at_ms is recorded for both self and external
        # assertions so decoration-weight decay works for both
        # (relevant for the anchor-boundary module).
        import time as _time
        _now_ms = int(_time.time() * 1000)
        asserter_properties: dict[str, Any] = {}
        if asserter is not None and asserter.is_external:
            asserter_properties = {
                "asserter_palace_id": asserter.palace_id,
                "asserter_session_pubkey_hex": asserter.session_pubkey_hex,
                "asserter_signature_hex": asserter.signature_hex,
                "asserter_is_external": True,
                "subject_is_self": subject_id == SELF_ENTITY_ID,
                "asserted_at_ms": _now_ms,
            }
        else:
            # Self-assertion (or unset asserter — treated as self).
            # Still mark `subject_is_self` so query helpers can
            # distinguish self-mentions cheaply.
            asserter_properties = {
                "asserter_is_external": False,
                "subject_is_self": subject_id == SELF_ENTITY_ID,
                "asserted_at_ms": _now_ms,
            }

        with self._client.batch(
            "graph.add_assertion",
            expected_count=expected,
            input_summary={
                "subject_id": subject_id,
                "predicate": predicate,
                "object_id": object_id,
                "asserter_external": asserter is not None and asserter.is_external,
            },
        ) as bh:
            properties = {
                "predicate": predicate,
                "predicate_surface": predicate,
                "confidence": confidence,
                **asserter_properties,
            }
            bh.append(NodeCreated(
                node_id=assertion_id,
                node_kind=NodeKind.ASSERTION.value,
                properties=properties,
                importance=0.5,
                created_by=(
                    f"asserter:{asserter.palace_id}"
                    if asserter is not None and asserter.is_external
                    else "user"
                ),
            ))
            self._add_edge(
                EdgeKind.ASSERTED_SUBJECT, assertion_id, subject_id,
                valid_from=valid_from_ms, valid_to=valid_to_ms,
                confidence=confidence, derivation=derivation,
                _batch_handle=bh,
            )
            self._add_edge(
                EdgeKind.ASSERTED_OBJECT, assertion_id, object_id,
                valid_from=valid_from_ms, valid_to=valid_to_ms,
                confidence=confidence, derivation=derivation,
                _batch_handle=bh,
            )
            spans_by_drawer = derived_from_spans or {}
            for drawer_id in derived_drawers:
                edge_props: dict[str, Any] | None = None
                if drawer_id in spans_by_drawer:
                    edge_props = spans_by_drawer[drawer_id].to_edge_properties()
                self._add_edge(
                    EdgeKind.DERIVED_FROM, assertion_id, drawer_id,
                    derivation=derivation,
                    weight=1.0 / max(len(derived_drawers), 1),
                    properties=edge_props,
                    _batch_handle=bh,
                )
        return assertion_id

    def assert_triple(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        derived_from_drawers: list[str] | None = None,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        derivation: DerivationType = DerivationType.OBSERVATION,
        confidence: float = 1.0,
    ) -> str:
        """Deprecated alias for `add_assertion`.

        The data model is the 8-part assertion frame from R3 §1.3,
        not a triple. Kept as a compatibility shim for callers that
        haven't migrated. Will be removed in a future cleanup.
        """
        import warnings
        warnings.warn(
            "Graph.assert_triple is deprecated; use Graph.add_assertion instead. "
            "The data model is the 8-part assertion frame, not a triple.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.add_assertion(
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            derived_from_drawers=derived_from_drawers,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            derivation=derivation,
            confidence=confidence,
        )

    # =========================================================================
    # Goals (as edges)
    # =========================================================================

    def create_pursues(
        self,
        source_id: str,
        target_id: str,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
    ) -> str:
        return self._add_edge(
            EdgeKind.PURSUES, source_id, target_id,
            valid_from=valid_from_ms, valid_to=valid_to_ms,
        )

    # =========================================================================
    # I-am bindings
    # =========================================================================

    def bind_iam(
        self,
        target_node_id: str,
        role: str,
        period_id: str | None = None,
        valid_from_ms: int | None = None,
    ) -> str:
        """Bind a role on the self-entity."""
        return self._add_edge(
            EdgeKind.ROLE_IN_PERIOD,
            SELF_ENTITY_ID,
            target_node_id,
            valid_from=valid_from_ms,
            properties={"role": role, "period_id": period_id} if period_id else {"role": role},
        )

    # =========================================================================
    # Edge invalidation
    # =========================================================================

    def invalidate_edge(self, edge_id: str, reason: str | None = None) -> None:
        self._client.append(EdgeInvalidated(edge_id=edge_id, reason=reason))

    # =========================================================================
    # Internal: edge emission helper
    # =========================================================================

    def _add_edge(
        self,
        kind: EdgeKind,
        source_id: str,
        target_id: str,
        valid_from: int | None = None,
        valid_to: int | None = None,
        weight: float = 1.0,
        confidence: float = 1.0,
        derivation: DerivationType = DerivationType.OBSERVATION,
        properties: dict[str, Any] | None = None,
        _batch_handle: "BatchHandle | None" = None,
    ) -> str:
        """Append an EdgeCreated event.

        If `_batch_handle` is provided, appends through the batch (so
        the event carries the batch's batch_id). Otherwise appends
        directly as an implicit single-event batch (the legacy path).
        """
        edge_id = make_edge_id()
        ev = EdgeCreated(
            edge_id=edge_id,
            edge_kind=kind.value,
            source_node_id=source_id,
            target_node_id=target_id,
            valid_from=valid_from,
            valid_to=valid_to,
            weight=weight,
            confidence=confidence,
            derivation=derivation.value,
            properties=properties or {},
        )
        if _batch_handle is not None:
            _batch_handle.append(ev)
        else:
            self._client.append(ev)
        return edge_id


# =============================================================================
# Cross-palace mention query helpers
#
# These pull back assertions filtered by their asserter, supporting the
# matching-layer use cases:
#
#   - "what others said about me" — assertions where subject = SELF and
#     asserter is external
#   - "what I observed about myself" — assertions where subject = SELF
#     and asserter is self
#   - "what palace_X said about me" — assertions where subject = SELF
#     and asserter.palace_id = X
#
# These are convenience composers over the existing edge accessors.
# Production callers can reach the same data via raw incoming_edges +
# node lookups; the helpers are for legibility.
# =============================================================================


def assertions_about(
    subject_id: str,
    *,
    include_self_asserted: bool = True,
    include_external_asserted: bool = True,
    asserter_palace_id: str | None = None,
) -> list:
    """Return assertions with `subject_id` as their subject.

    Args:
      subject_id: The node whose subject-edge to follow. Pass
        SELF_ENTITY_ID for "claims about me."
      include_self_asserted: If False, skip assertions this palace
        produced.
      include_external_asserted: If False, skip cross-palace mentions.
      asserter_palace_id: If non-None, return only assertions made
        by exactly that palace_id.

    Returns:
      List of NodeState for matching assertion nodes. Each carries
      asserter info in its properties.
    """
    # Find every ASSERTED_SUBJECT edge pointing at subject_id; the
    # source_node_id of each is an assertion node.
    edges = views.incoming_edges(subject_id, kind=EdgeKind.ASSERTED_SUBJECT)
    out = []
    for edge in edges:
        node = views.current_node(edge.source_node_id)
        if node is None or node.node_kind != NodeKind.ASSERTION.value:
            continue
        is_external = bool(node.properties.get("asserter_is_external"))
        if is_external and not include_external_asserted:
            continue
        if (not is_external) and not include_self_asserted:
            continue
        if asserter_palace_id is not None:
            if node.properties.get("asserter_palace_id") != asserter_palace_id:
                continue
        out.append(node)
    return out


def assertions_about_self(
    *,
    include_self_asserted: bool = True,
    include_external_asserted: bool = True,
) -> list:
    """Convenience: assertions about SELF_ENTITY.

    The matching-layer's primary read: "what is on record about me,
    and from whom?"
    """
    return assertions_about(
        SELF_ENTITY_ID,
        include_self_asserted=include_self_asserted,
        include_external_asserted=include_external_asserted,
    )


def external_mentions_of_self() -> list:
    """All cross-palace mentions of self. The matching-layer's
    strongest alignment input."""
    return assertions_about_self(
        include_self_asserted=False,
        include_external_asserted=True,
    )


# =============================================================================
# Module-level singleton
# =============================================================================

_default_graph: Graph | None = None


def get_default_graph() -> Graph:
    global _default_graph
    if _default_graph is None:
        _default_graph = Graph()
    return _default_graph
