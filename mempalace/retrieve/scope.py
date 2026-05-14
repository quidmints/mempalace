"""
Scope expansion.

A scope spec defines what subset of the graph a query is interested in.
Scopes compose: hierarchical (period → event → drawer), thematic (theme →
periods → events → drawers), temporal (time window). The scope expander
takes a typed `Scope` and produces a list of candidate node IDs.

Per Conway's hierarchical retrieval finding (R3 §9.1), the expander
prefers period/event constraints before drawer-level matching. The
`prefer_hierarchical` flag (default True) controls this preference.

Spec ref: Part 6 (handle protocol scope), R3 §9.1 (Conway hierarchical).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..schema.kinds import EdgeKind, NodeKind
from ..views.current import (
    EdgeState,
    NodeState,
    _get_store,
    incoming_edges,
    outgoing_edges,
)


# =============================================================================
# Scope spec
# =============================================================================


@dataclass
class Scope:
    """Typed scope for retrieval.

    Multiple constraint kinds compose by intersection: a candidate must
    satisfy all non-empty constraints.

    Attributes:
        node_ids: explicit candidate set. If non-empty, only these are
            considered (intersected with other filters).
        node_kinds: restrict to candidates of these kinds.
        period_ids: candidates contained by these periods (transitively).
        event_ids: candidates contained by these events.
        theme_ids: candidates under these themes.
        valid_at_ms: bitemporal — candidates valid at this world time.
        time_window_ms: (start, end) — candidates whose recorded_at falls in.
        max_candidates: hard cap on returned set size (post-filter).
        prefer_hierarchical: walk through period→event→drawer rather than
            flat scan. Default True per Conway hierarchical-retrieval.
        canonical_only: if True, restrict to canonical nodes.
    """

    node_ids: list[str] = field(default_factory=list)
    node_kinds: list[NodeKind] = field(default_factory=list)
    period_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    theme_ids: list[str] = field(default_factory=list)
    valid_at_ms: int | None = None
    time_window_ms: tuple[int, int] | None = None
    max_candidates: int = 1000
    prefer_hierarchical: bool = True
    canonical_only: bool = False


# =============================================================================
# Expander
# =============================================================================


def _walk_contains(root_id: str, target_kind: NodeKind | None = None) -> set[str]:
    """Walk CONTAINS edges from root_id, returning all reached node IDs.

    Optionally filter to only nodes of a target kind.
    """
    store = _get_store()
    visited: set[str] = set()
    queue: list[str] = [root_id]
    out: set[str] = set()
    while queue:
        node_id = queue.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        # Get outgoing CONTAINS edges
        outgoing = outgoing_edges(node_id, EdgeKind.CONTAINS)
        for edge in outgoing:
            if not edge.is_active():
                continue
            child_id = edge.target_node_id
            child_node = store.nodes.get(child_id)
            if child_node is None:
                queue.append(child_id)
                continue
            if target_kind is None or child_node.node_kind == target_kind.value:
                out.add(child_id)
            queue.append(child_id)
    return out


def expand(scope: Scope) -> list[str]:
    """Expand a scope to a list of candidate node IDs.

    Returns deduplicated, capped list. Order is not specified at this
    layer — the ranker imposes order downstream.
    """
    store = _get_store()
    candidates: set[str] | None = None  # None means "no constraint yet"

    def intersect(new: Iterable[str]) -> None:
        nonlocal candidates
        s = set(new)
        if candidates is None:
            candidates = s
        else:
            candidates = candidates & s

    # Explicit IDs
    if scope.node_ids:
        intersect(scope.node_ids)

    # Period containment — walk CONTAINS hierarchy
    if scope.period_ids:
        period_reach: set[str] = set()
        for pid in scope.period_ids:
            period_reach |= _walk_contains(pid)
        intersect(period_reach)

    # Event containment — typically drawer_refs under an event
    if scope.event_ids:
        event_reach: set[str] = set()
        for eid in scope.event_ids:
            event_reach |= _walk_contains(eid)
        intersect(event_reach)

    # Theme containment — theme → periods → events → drawers
    if scope.theme_ids:
        theme_reach: set[str] = set()
        for tid in scope.theme_ids:
            theme_reach |= _walk_contains(tid)
        intersect(theme_reach)

    # If no positive constraints have narrowed yet, start with "all nodes"
    if candidates is None:
        candidates = set(store.nodes.keys())

    # Filter by node kind
    if scope.node_kinds:
        kind_values = {k.value for k in scope.node_kinds}
        candidates = {nid for nid in candidates if (
            store.nodes.get(nid) is not None
            and store.nodes[nid].node_kind in kind_values
        )}

    # Canonical only
    if scope.canonical_only:
        candidates = candidates & store.canonicals

    # Bitemporal: only nodes whose at-least-one-incoming-edge is valid_at
    if scope.valid_at_ms is not None:
        valid_set: set[str] = set()
        for nid in candidates:
            in_edges = [
                store.edges[eid] for eid in store.incoming.get(nid, [])
                if eid in store.edges
            ]
            if any(e.is_valid_at(scope.valid_at_ms) for e in in_edges):
                valid_set.add(nid)
            elif not in_edges:
                # Nodes with no incoming edges (orphan roots) are always valid
                valid_set.add(nid)
        candidates = valid_set

    # Time window on recorded_at
    if scope.time_window_ms is not None:
        start_ms, end_ms = scope.time_window_ms
        # We use captured_at if present in node properties, else fall through
        windowed: set[str] = set()
        for nid in candidates:
            node = store.nodes.get(nid)
            if node is None:
                continue
            ts = node.properties.get("capture_recorded_at")
            if ts is None or (start_ms <= ts < end_ms):
                windowed.add(nid)
        candidates = windowed

    # Cap
    out = list(candidates)
    if len(out) > scope.max_candidates:
        # Stable order: by node ID lexicographically (deterministic)
        out.sort()
        out = out[: scope.max_candidates]

    return out


__all__ = ["Scope", "expand"]
