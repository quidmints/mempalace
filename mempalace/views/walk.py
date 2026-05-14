"""
Typed graph traversal.

Walk by edge-kind, max-hops, with stance-aware weighting. Used by handle
resolution (Part 6.4) and by per-dimension pull computation in features
(Part 6.1).

The "per-dimension pull" formulation: weighting walks by edge-kind family
gives different views of structural connectivity. provenance_pull walks
inbound `derived_from`; abstraction_pull walks inbound `instantiates`;
relational_pull walks inbound `asserted_subject` / `asserted_object`; etc.

Spec ref: Part 6.1, Part 6.4
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from . import current as views
from ..schema.kinds import EdgeKind


@dataclass
class WalkResult:
    """Result of a single walk: nodes reached and the cumulative weight."""
    node_ids: list[str]
    weights: dict[str, float]  # node_id → cumulative weight along walks


def walk_outgoing(
    start: str,
    *,
    edge_kinds: list[EdgeKind] | None = None,
    max_hops: int = 3,
    weight_decay: float = 0.7,
) -> WalkResult:
    """Walk outgoing edges from `start`, optionally filtered by edge_kinds.

    Args:
        start: starting node ID.
        edge_kinds: if provided, only edges of these kinds are followed.
        max_hops: maximum hop distance.
        weight_decay: per-hop multiplier on edge weights.

    Returns:
        WalkResult with reached nodes and cumulative weights.
    """
    visited: dict[str, float] = {}
    frontier: list[tuple[str, float, int]] = [(start, 1.0, 0)]
    visited[start] = 1.0

    while frontier:
        node, weight, hops = frontier.pop(0)
        if hops >= max_hops:
            continue
        edges = views.outgoing_edges(node, kind=None)
        for edge in edges:
            if edge_kinds is not None:
                if not any(edge.edge_kind == k.value for k in edge_kinds):
                    continue
            target = edge.target_node_id
            new_weight = weight * edge.weight * weight_decay
            existing = visited.get(target, 0.0)
            if new_weight > existing:
                visited[target] = new_weight
                frontier.append((target, new_weight, hops + 1))

    return WalkResult(
        node_ids=list(visited.keys()),
        weights=visited,
    )


def walk_incoming(
    target: str,
    *,
    edge_kinds: list[EdgeKind] | None = None,
    max_hops: int = 3,
    weight_decay: float = 0.7,
) -> WalkResult:
    """Walk inbound edges to `target`, optionally filtered."""
    visited: dict[str, float] = {target: 1.0}
    frontier: list[tuple[str, float, int]] = [(target, 1.0, 0)]

    while frontier:
        node, weight, hops = frontier.pop(0)
        if hops >= max_hops:
            continue
        edges = views.incoming_edges(node, kind=None)
        for edge in edges:
            if edge_kinds is not None:
                if not any(edge.edge_kind == k.value for k in edge_kinds):
                    continue
            source = edge.source_node_id
            new_weight = weight * edge.weight * weight_decay
            existing = visited.get(source, 0.0)
            if new_weight > existing:
                visited[source] = new_weight
                frontier.append((source, new_weight, hops + 1))

    return WalkResult(node_ids=list(visited.keys()), weights=visited)


# =============================================================================
# Per-dimension pull (Part 6.1)
# =============================================================================

@dataclass
class PerDimensionPull:
    """Per-dimension pull values for a node.

    Each dimension corresponds to a family of inbound edges; the value is the
    summed weight of inbound edges of that kind. Used as ranker features.
    """
    provenance_pull: float = 0.0      # inbound derived_from
    abstraction_pull: float = 0.0     # inbound instantiates
    structural_pull: float = 0.0      # inbound contains
    relational_pull: float = 0.0      # inbound asserted_subject + asserted_object
    tension_pull: float = 0.0         # inbound contradicts


def per_dimension_pull(node_id: str) -> PerDimensionPull:
    """Compute per-dimension pull values for the given node."""
    pull = PerDimensionPull()
    for edge in views.incoming_edges(node_id):
        ek = edge.edge_kind
        if ek == EdgeKind.DERIVED_FROM.value:
            pull.provenance_pull += edge.weight
        elif ek == EdgeKind.INSTANTIATES.value:
            pull.abstraction_pull += edge.weight
        elif ek == EdgeKind.CONTAINS.value:
            pull.structural_pull += edge.weight
        elif ek in (EdgeKind.ASSERTED_SUBJECT.value, EdgeKind.ASSERTED_OBJECT.value):
            pull.relational_pull += edge.weight
        elif ek == EdgeKind.CONTRADICTS.value:
            pull.tension_pull += edge.weight
    return pull
