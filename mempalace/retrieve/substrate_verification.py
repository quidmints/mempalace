"""
Substrate verification — R3 §9.3.

# What this addresses

Schema-driven gap-filling is a confabulation risk. The miner can
assemble an assertion from multiple drawers in ways that drift from
what the drawers actually said — producing claims that look
well-formed but aren't grounded in the substrate. R3 §9.3 calls
this "coherence-overwrite."

The mitigation: at retrieval time, the consumer can request
**substrate verification**. Retrieved assertions come with their
supporting drawer references AND a **substrate-faithfulness score**
indicating how closely the assertion text matches the substrate
text at the cited spans.

  - Low scores (< 0.3): the assertion has drifted from the drawers
    it claims to derive from. Possible coherence-overwrite. The
    consumer should treat the claim with more skepticism, and the
    user-review surface should flag it.
  - High scores (> 0.7): the assertion is well-grounded; the words
    used in the assertion mirror the substrate at the cited span.

This module is the scorer and the per-assertion query helper.

# Where this fits

  - **`Graph.add_assertion(..., derived_from_spans=...)`** is where
    spans get persisted on `derived_from` edges. See
    `mempalace.views.graph.DrawerSpan`.
  - **`HandleState.substrate_verification: bool`** is the
    consumer-side flag (set to True to opt in).
  - This module's `verify_assertion(...)` is what the retrieval
    pipeline calls when the flag is set, returning a
    `SubstrateFaithfulness` envelope per assertion.

# Faithfulness scoring

The default scorer is token-set Jaccard between the
predicate-rendering of the assertion and the spanned substrate
text. This is cheap, deterministic, and order-invariant. It works
because the test we want is "does the substrate contain the words
the assertion claims" rather than "is the substrate semantically
similar to the assertion." For the latter, callers can supply a
custom `text_similarity` function that uses an embedding model.

When the assertion has multiple `derived_from` edges, scores per
span are aggregated by max-of-supporting-spans (a single
well-grounded span is enough; we don't punish for irrelevant
co-cited drawers).

When `derived_from` edges have no span — drawer-level provenance
only — faithfulness is computed against the whole drawer text
with a discount factor (0.7×) reflecting the loss of precision.

Spec ref: integration_appendix_r3.md §9.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..schema.kinds import EdgeKind, NodeKind
from ..views import current as views
from ..views.graph import DrawerSpan


# =============================================================================
# Tunables
# =============================================================================


DEFAULT_LOW_FAITHFULNESS_THRESHOLD = 0.3
"""Below this, surface to the user-review surface as possibly
confabulated."""

DEFAULT_HIGH_FAITHFULNESS_THRESHOLD = 0.7
"""Above this, treat as well-grounded."""

DRAWER_LEVEL_DISCOUNT = 0.7
"""Multiplier applied when the supporting edge has no span (whole-
drawer provenance). Reflects the precision loss vs a span-pinned
edge."""


# =============================================================================
# Output envelope
# =============================================================================


@dataclass
class SpanFaithfulness:
    """Per-supporting-span scoring detail."""

    drawer_id: str
    span: DrawerSpan | None
    """None when the edge has no span — drawer-level provenance."""

    substrate_text: str
    """The text used as the substrate reference for scoring. For
    spanned edges, the slice; for unspanned, the whole drawer."""

    faithfulness: float
    """Token-set Jaccard between assertion text and substrate text,
    in [0, 1]. Discounted for unspanned edges."""


@dataclass
class SubstrateFaithfulness:
    """The verification result for one assertion."""

    assertion_id: str
    assertion_text: str
    """The renderable text used for scoring — typically the
    predicate name. Override-able via `assertion_text_renderer`."""

    per_span: list[SpanFaithfulness] = field(default_factory=list)

    aggregate_score: float = 0.0
    """Max of per-span scores. A single well-grounded span is
    enough; we don't penalize for irrelevant co-cited drawers."""

    has_any_spans: bool = False
    """False when none of the derived_from edges carry span
    properties. Indicates the assertion was written under the older
    drawer-level-only provenance regime."""

    @property
    def is_low_faithfulness(self) -> bool:
        return self.aggregate_score < DEFAULT_LOW_FAITHFULNESS_THRESHOLD

    @property
    def is_high_faithfulness(self) -> bool:
        return self.aggregate_score >= DEFAULT_HIGH_FAITHFULNESS_THRESHOLD


# =============================================================================
# Scoring primitives
# =============================================================================


def _tokens(text: str) -> set[str]:
    """Lowercased token set. No stemming, no punctuation handling
    beyond whitespace split — keeps the scorer fast and stable."""
    return {t for t in text.lower().split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def default_text_similarity(assertion: str, substrate: str) -> float:
    """Token-set Jaccard. The default scorer."""
    return _jaccard(_tokens(assertion), _tokens(substrate))


# =============================================================================
# Drawer text extraction
# =============================================================================


def _drawer_text(drawer_id: str) -> str:
    """Extract verbatim text from a drawer. Empty when the drawer
    doesn't exist OR is encrypted (v2+) — encrypted drawers go
    through `mempalace.drawer.secure_read` instead."""
    return views.drawer_text(drawer_id)


def _slice_text(full_text: str, span: DrawerSpan) -> str:
    """Token-level slice. Pure split-on-whitespace; the indexing
    matches what `_tokens` produces, so a span-pinned token range
    in the substrate is the same set the assertion is compared
    against."""
    if span.is_empty:
        return ""
    tokens = full_text.split()
    start = max(0, span.start_token)
    end = min(len(tokens), span.end_token)
    if start >= end:
        return ""
    return " ".join(tokens[start:end])


# =============================================================================
# Verification
# =============================================================================


def _default_assertion_text(assertion_node: Any) -> str:
    """Renderable assertion text. Default: the predicate, plus the
    object node's name if available. Callers can supply a custom
    renderer for richer texts."""
    pred = str(assertion_node.properties.get("predicate", ""))
    # Walk to the object node (the asserted_object edge target)
    obj_edges = views.outgoing_edges(
        assertion_node.node_id, kind=EdgeKind.ASSERTED_OBJECT,
    )
    obj_text = ""
    if obj_edges:
        obj_node = views.current_node(obj_edges[0].target_node_id)
        if obj_node is not None:
            obj_text = str(obj_node.properties.get("name", ""))
    parts = [p for p in (pred, obj_text) if p]
    return " ".join(parts)


def verify_assertion(
    assertion_id: str,
    *,
    text_similarity: Callable[[str, str], float] | None = None,
    assertion_text_renderer: Callable[[Any], str] | None = None,
) -> SubstrateFaithfulness | None:
    """Compute substrate faithfulness for one assertion.

    Args:
      assertion_id: The assertion node to verify.
      text_similarity: Override the default token-set Jaccard.
        Signature: (assertion_text, substrate_text) -> float in [0, 1].
        Use this to plug in an embedding-based similarity for
        higher-quality scoring.
      assertion_text_renderer: Override how the assertion is
        rendered as text for comparison. Default: predicate + object
        name. Use this to render richer assertion text (e.g.,
        including the subject, valid_from time, etc.).

    Returns:
      SubstrateFaithfulness, or None if `assertion_id` isn't an
      assertion node or doesn't exist.
    """
    similarity = text_similarity or default_text_similarity
    renderer = assertion_text_renderer or _default_assertion_text

    node = views.current_node(assertion_id)
    if node is None or node.node_kind != NodeKind.ASSERTION.value:
        return None

    assertion_text = renderer(node)

    derived_edges = views.outgoing_edges(
        assertion_id, kind=EdgeKind.DERIVED_FROM,
    )
    per_span: list[SpanFaithfulness] = []
    has_any_spans = False

    for edge in derived_edges:
        drawer_id = edge.target_node_id
        full_text = _drawer_text(drawer_id)
        span = DrawerSpan.from_edge_properties(edge.properties)
        if span is not None:
            has_any_spans = True
            substrate_text = _slice_text(full_text, span)
            score = similarity(assertion_text, substrate_text)
        else:
            # Drawer-level provenance: score against full drawer
            # with the precision discount.
            substrate_text = full_text
            score = (
                similarity(assertion_text, substrate_text)
                * DRAWER_LEVEL_DISCOUNT
            )
        per_span.append(SpanFaithfulness(
            drawer_id=drawer_id,
            span=span,
            substrate_text=substrate_text,
            faithfulness=score,
        ))

    aggregate = max((s.faithfulness for s in per_span), default=0.0)
    return SubstrateFaithfulness(
        assertion_id=assertion_id,
        assertion_text=assertion_text,
        per_span=per_span,
        aggregate_score=aggregate,
        has_any_spans=has_any_spans,
    )


def verify_assertions(
    assertion_ids: list[str],
    *,
    text_similarity: Callable[[str, str], float] | None = None,
    assertion_text_renderer: Callable[[Any], str] | None = None,
) -> list[SubstrateFaithfulness]:
    """Bulk version of `verify_assertion`. None entries are dropped
    so the returned list is a subset of valid assertion verifications."""
    out: list[SubstrateFaithfulness] = []
    for aid in assertion_ids:
        v = verify_assertion(
            aid,
            text_similarity=text_similarity,
            assertion_text_renderer=assertion_text_renderer,
        )
        if v is not None:
            out.append(v)
    return out


def filter_low_faithfulness(
    verifications: list[SubstrateFaithfulness],
    *,
    threshold: float = DEFAULT_LOW_FAITHFULNESS_THRESHOLD,
) -> list[SubstrateFaithfulness]:
    """The user-review-surface input: assertions that should be
    flagged for possible coherence-overwrite."""
    return [v for v in verifications if v.aggregate_score < threshold]


__all__ = [
    "DEFAULT_HIGH_FAITHFULNESS_THRESHOLD",
    "DEFAULT_LOW_FAITHFULNESS_THRESHOLD",
    "DRAWER_LEVEL_DISCOUNT",
    "SpanFaithfulness",
    "SubstrateFaithfulness",
    "default_text_similarity",
    "filter_low_faithfulness",
    "verify_assertion",
    "verify_assertions",
]
