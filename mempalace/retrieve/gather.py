"""
Candidate gathering.

Given a scope, gather candidates and their associated context (features,
edges, properties) so that downstream ranking and fidelity-tier rendering
have everything they need without re-traversing views.

This sits between scope expansion (`retrieve.scope.expand`) and ranking
(`rank/`). It's the boundary where the master views are read; everything
downstream operates on the gathered structure.

Spec ref: Part 6 (handle protocol), Part 7 (rankers consume features).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..features.compute import compute as compute_feature
from ..features.persist import get_feature_store
from ..features.registry import get_registry
from ..schema.kinds import NodeKind
from ..schema.stance import Stance
from ..views.current import (
    EdgeState,
    NodeState,
    _get_store,
    current_node,
)
from .scope import Scope, expand


# =============================================================================
# Candidate
# =============================================================================


@dataclass
class Candidate:
    """One retrieval candidate with attached context.

    Ranking and fidelity-tier rendering both consume this structure.
    """

    node_id: str
    node: NodeState
    features: dict[str, Any] = field(default_factory=dict)
    outgoing: list[EdgeState] = field(default_factory=list)
    incoming: list[EdgeState] = field(default_factory=list)
    # Reserved for substrate-verification flag at fidelity stage
    derivation_chain: list[str] = field(default_factory=list)


@dataclass
class GatherResult:
    """Output of `gather()`.

    `candidates` is the populated list. `scope_count` is the candidate
    count before any cap was applied (so callers can detect truncation).
    """

    candidates: list[Candidate]
    scope_count: int
    truncated: bool = False


# =============================================================================
# gather()
# =============================================================================


def gather(
    scope: Scope,
    *,
    feature_names: list[str] | None = None,
    stance: Stance | None = None,
    include_edges: bool = True,
    use_persisted_features: bool = True,
    now_ms: int | None = None,
) -> GatherResult:
    """Gather candidates for a scope, attaching features and adjacency.

    Args:
        scope: scope spec (see retrieve.scope.Scope).
        feature_names: features to compute/load per candidate. If None,
            uses the kind-appropriate defaults from the registry.
        stance: optional stance for stance-aware features.
        include_edges: whether to load outgoing/incoming edges.
        use_persisted_features: if True, prefer persisted feature values
            from FeatureStore; recompute when missing. If False, always
            recompute fresh.
        now_ms: optional time anchor for time-decay features.
    """
    store = _get_store()
    feature_store = get_feature_store()
    registry = get_registry()

    candidate_ids = expand(scope)
    scope_count = len(candidate_ids)
    truncated = scope_count >= scope.max_candidates

    candidates: list[Candidate] = []
    for nid in candidate_ids:
        node = store.nodes.get(nid)
        if node is None:
            continue

        # Feature defaults: pick the registry's features matching the node kind
        if feature_names is None:
            try:
                kind_enum = NodeKind(node.node_kind)
                kind_features = registry.list_features(subject_kind=kind_enum)
                # Add stance-aware globals
                global_features = registry.list_features(subject_kind="global")
                effective_features = [f.name for f in kind_features] + [f.name for f in global_features]
            except ValueError:
                effective_features = []
        else:
            effective_features = list(feature_names)

        # Load / compute features
        feature_values: dict[str, Any] = {}
        for fname in effective_features:
            if use_persisted_features:
                fv = feature_store.get(fname, nid)
                if fv is not None:
                    feature_values[fname] = fv.value
                    continue
            try:
                feature_values[fname] = compute_feature(
                    fname, nid, stance=stance, now_ms=now_ms
                )
            except (KeyError, RuntimeError):
                # Feature not registered or no compute fn — skip silently;
                # rankers must handle missing features gracefully.
                continue

        # Load edges
        outgoing: list[EdgeState] = []
        incoming: list[EdgeState] = []
        if include_edges:
            outgoing = [
                store.edges[eid]
                for eid in store.outgoing.get(nid, [])
                if eid in store.edges
            ]
            incoming = [
                store.edges[eid]
                for eid in store.incoming.get(nid, [])
                if eid in store.edges
            ]

        candidates.append(Candidate(
            node_id=nid,
            node=node,
            features=feature_values,
            outgoing=outgoing,
            incoming=incoming,
        ))

    return GatherResult(
        candidates=candidates,
        scope_count=scope_count,
        truncated=truncated,
    )


__all__ = ["Candidate", "GatherResult", "gather"]
