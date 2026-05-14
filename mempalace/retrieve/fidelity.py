"""
Fidelity tiers.

A retrieval handle can be resolved at different fidelity levels. Lower
fidelity returns less data per candidate (cheaper, faster, less exposure);
higher fidelity returns full substrate.

Tiers:
  - LITE      : node id, kind, top features, score. No facets, no edges.
  - STANDARD  : adds outgoing edges, top-K associated nodes, key facets
               (verbatim text excerpt, structural metadata).
  - FULL      : full FacetBundle (verbatim, acoustic, semantic, structural,
               social), full edge neighborhood, derivation chains.
  - SUBSTRATE_VERIFY : FULL + the supporting drawer span pointers and
               substrate-faithfulness scores per assertion (R3 §9.3).

Spec ref: Part 6 (handle protocol fidelity), R3 §9.3 (substrate
verification), R3 §7.7 (privacy framing — fidelity is the granularity
at which exposure-bounded-by-intention is enforced).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..views.current import EdgeState, _get_store
from .gather import Candidate


class Fidelity(str, Enum):
    LITE = "lite"
    STANDARD = "standard"
    FULL = "full"
    SUBSTRATE_VERIFY = "substrate_verify"


# =============================================================================
# Rendered candidate at a given fidelity
# =============================================================================


@dataclass
class RenderedCandidate:
    """The shape returned to consumers at resolve time.

    Fields are populated based on the fidelity tier; lower tiers have
    None for higher-tier-only fields.
    """

    node_id: str
    node_kind: str
    score: float
    features: dict[str, Any] = field(default_factory=dict)

    # STANDARD and above
    edges: list[dict[str, Any]] | None = None
    properties: dict[str, Any] | None = None
    text_excerpt: str | None = None

    # FULL and above
    facet_bundle_ref: str | None = None
    derivation_chain: list[str] | None = None

    # SUBSTRATE_VERIFY only
    substrate_spans: list[dict[str, Any]] | None = None
    faithfulness_score: float | None = None


# =============================================================================
# Renderers per fidelity
# =============================================================================


def _excerpt_from_node(node_id: str, max_chars: int = 240) -> str | None:
    """Pull a verbatim-text excerpt from the node's properties if present."""
    store = _get_store()
    node = store.nodes.get(node_id)
    if node is None:
        return None
    text = node.properties.get("verbatim_text") or node.properties.get("text")
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _edge_to_dict(edge: EdgeState) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "edge_kind": edge.edge_kind,
        "source": edge.source_node_id,
        "target": edge.target_node_id,
        "valid_from": edge.valid_from,
        "valid_to": edge.valid_to,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "active": edge.is_active(),
    }


def render_lite(candidate: Candidate, score: float) -> RenderedCandidate:
    return RenderedCandidate(
        node_id=candidate.node_id,
        node_kind=candidate.node.node_kind,
        score=score,
        features=dict(candidate.features),
    )


def render_standard(candidate: Candidate, score: float) -> RenderedCandidate:
    rendered = render_lite(candidate, score)
    rendered.edges = [_edge_to_dict(e) for e in candidate.outgoing if e.is_active()]
    rendered.properties = dict(candidate.node.properties)
    rendered.text_excerpt = _excerpt_from_node(candidate.node_id)
    return rendered


def render_full(candidate: Candidate, score: float) -> RenderedCandidate:
    rendered = render_standard(candidate, score)
    # facet_bundle_ref: pointer to the facet bundle's storage. In production,
    # this resolves to the drawer's full FacetBundle via embed.client +
    # node properties. Here we store the drawer_id as the ref.
    rendered.facet_bundle_ref = candidate.node_id
    # Derivation chain: walk derived_from edges
    derived_from_targets = [
        e.target_node_id
        for e in candidate.outgoing
        if e.edge_kind == "derived_from" and e.is_active()
    ]
    rendered.derivation_chain = derived_from_targets
    return rendered


def render_substrate_verify(candidate: Candidate, score: float) -> RenderedCandidate:
    rendered = render_full(candidate, score)
    # Substrate spans: pull span pointers from derived_from edge properties
    spans: list[dict[str, Any]] = []
    for e in candidate.outgoing:
        if e.edge_kind != "derived_from" or not e.is_active():
            continue
        span = e.properties.get("span")
        if span is not None:
            spans.append({
                "drawer_id": e.target_node_id,
                "span": span,
            })
    rendered.substrate_spans = spans

    # Faithfulness score: pull from features if present
    rendered.faithfulness_score = candidate.features.get(
        "assertion_substrate_faithfulness"
    )
    return rendered


_RENDERERS = {
    Fidelity.LITE: render_lite,
    Fidelity.STANDARD: render_standard,
    Fidelity.FULL: render_full,
    Fidelity.SUBSTRATE_VERIFY: render_substrate_verify,
}


def render(
    candidate: Candidate, score: float, fidelity: Fidelity
) -> RenderedCandidate:
    """Render one scored candidate at the requested fidelity."""
    return _RENDERERS[fidelity](candidate, score)


def render_all(
    scored_candidates: list[tuple[Candidate, float]],
    fidelity: Fidelity,
) -> list[RenderedCandidate]:
    return [render(c, s, fidelity) for c, s in scored_candidates]


__all__ = [
    "Fidelity",
    "RenderedCandidate",
    "render",
    "render_all",
    "render_full",
    "render_lite",
    "render_standard",
    "render_substrate_verify",
]
