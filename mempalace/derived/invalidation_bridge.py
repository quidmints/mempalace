"""
Invalidation bridge — Track 4A + 6C.

Wires user-tier invalidation events (DrawerInvalidated, NodeInvalidated,
EdgeInvalidated, and their *Revalidated counterparts) to the
DependencyTracker. When a substrate object is invalidated, every
artifact downstream of it gets marked dirty; the RankerOutputCache's
lazy `is_dirty` check evicts those entries on next access.

# Why a separate module

The dependency tracker doesn't know about events; it knows about
DependencyKey edges. The schema doesn't know about the tracker; it
knows about events. This module bridges them.

# Lifecycle

  - `start_invalidation_bridge()` — opt-in subscriber. Production
    daemons call this at startup; tests call it as needed.
  - `stop_invalidation_bridge()` — for tests that need to undo.
  - `tick_invalidation_bridge()` — process pending events. Tests call
    this synchronously after appending invalidation events.

# What it propagates

  - `node_invalidated(node_id)` → `tracker.invalidate(substrate_field(
    node_id, field))` for every field encountered. Since fields are
    open-ended, we use `tracker.invalidate(node_id-keyed coarse key)`
    via SUBSTRATE_NODE_KIND_SET as a side-channel — but that's wrong.
    Cleaner: a NodeInvalidated event invalidates ALL DependencyKeys
    whose first identity element is this node_id. The tracker's
    invalidate() walks the dependents map; we walk it ourselves
    because the kind+identity tuple shape varies.

# What this module does NOT do

  - Cascade. Invalidating a node doesn't recursively invalidate its
    edges' artifacts (per design — the user can invalidate edges
    separately).
  - Persistence. The bridge subscribes from the current log offset
    forward; on restart, the consumer offset checkpoint logic
    handles where to resume.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"DD wiring",
HANDLES_DESIGN.md v2 §"Cluster-pattern caching",
IMPLEMENTATION_ROADMAP.md §"Track 4A".
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..log.subscriber import get_default_registry
from .dependency import (
    DependencyKey,
    DependencyKind,
    DependencyTracker,
    get_dependency_tracker,
    substrate_drawer,
)
from .ranker_cache import RankerOutputCache, get_default_cache

logger = logging.getLogger(__name__)


# =============================================================================
# Bridge state
# =============================================================================


_BRIDGE_STARTED = False
_BRIDGE_LOCK = threading.Lock()
_BRIDGE_CONSUMER_ID = "derived.invalidation_bridge"


# Event kinds the bridge subscribes to
INVALIDATION_KINDS = (
    "drawer_invalidated",
    "drawer_revalidated",
    "node_invalidated",
    "node_revalidated",
    "edge_invalidated",
    "edge_revalidated",
)


# =============================================================================
# Public API
# =============================================================================


def start_invalidation_bridge(
    *,
    tracker: DependencyTracker | None = None,
    cache: RankerOutputCache | None = None,
) -> None:
    """Register the bridge subscriber against the default registry.

    Idempotent: calling twice is a no-op (subsequent calls don't
    re-register).
    """
    global _BRIDGE_STARTED
    with _BRIDGE_LOCK:
        if _BRIDGE_STARTED:
            return
        actual_tracker = tracker or get_dependency_tracker()
        actual_cache = cache or get_default_cache()

        registry = get_default_registry()
        registry.register(
            consumer_id=_BRIDGE_CONSUMER_ID,
            kinds=list(INVALIDATION_KINDS),
            handler=_make_handler(actual_tracker, actual_cache),
            max_batch_size=256,
        )
        _BRIDGE_STARTED = True
        logger.debug("Invalidation bridge started")


def stop_invalidation_bridge() -> None:
    """Test hook: tear down the bridge subscription.

    The default subscriber registry doesn't support unregister, so
    we set a flag the handler checks. Restarting requires
    `reset_module_state()` (the conftest helper).
    """
    global _BRIDGE_STARTED
    with _BRIDGE_LOCK:
        _BRIDGE_STARTED = False


def tick_invalidation_bridge() -> int:
    """Drive pending events through the bridge.

    Returns the number of events processed. Tests call this after
    appending invalidation events to flush them through.
    """
    if not _BRIDGE_STARTED:
        return 0
    registry = get_default_registry()
    return registry.tick_one(_BRIDGE_CONSUMER_ID)


def is_invalidation_bridge_started() -> bool:
    return _BRIDGE_STARTED


# =============================================================================
# Event handler
# =============================================================================


def _make_handler(
    tracker: DependencyTracker,
    cache: RankerOutputCache,
) -> Callable[[int, str, dict], None]:
    """Build the closure-capturing handler.

    Closes over the tracker + cache so callers can inject mocks for
    testing.
    """

    def handler(offset: int, kind: str, payload: dict) -> None:
        if kind == "drawer_invalidated":
            drawer_id = payload.get("drawer_id", "")
            if drawer_id:
                _propagate_drawer_invalidation(tracker, cache, drawer_id)

        elif kind == "drawer_revalidated":
            # Revalidation doesn't actively re-cache anything — just
            # un-marks the dirty flag. Downstream artifacts will
            # re-compute on next access if cleared from cache;
            # otherwise they stay valid.
            drawer_id = payload.get("drawer_id", "")
            if drawer_id:
                _propagate_drawer_revalidation(tracker, drawer_id)

        elif kind == "node_invalidated":
            node_id = payload.get("node_id", "")
            if node_id:
                _propagate_node_invalidation(tracker, cache, node_id)

        elif kind == "node_revalidated":
            node_id = payload.get("node_id", "")
            if node_id:
                _propagate_node_revalidation(tracker, node_id)

        elif kind == "edge_invalidated":
            edge_id = payload.get("edge_id", "")
            if edge_id:
                _propagate_edge_invalidation(tracker, cache, edge_id)

        elif kind == "edge_revalidated":
            edge_id = payload.get("edge_id", "")
            if edge_id:
                _propagate_edge_revalidation(tracker, edge_id)

    return handler


# =============================================================================
# Propagation helpers
# =============================================================================


def _propagate_drawer_invalidation(
    tracker: DependencyTracker,
    cache: RankerOutputCache,
    drawer_id: str,
) -> None:
    """Invalidate every artifact downstream of this drawer.

    SUBSTRATE_DRAWER deps are the canonical hook. Cached entries that
    recorded a drawer dep get evicted; their downstream consumers
    (e.g., signatures, foyer renders) get marked dirty by the
    tracker's transitive walk.
    """
    drawer_key = substrate_drawer(drawer_id)
    report = tracker.invalidate(drawer_key, propagate=True)
    cache.invalidate_for_substrate_change(drawer_key)
    if report.count:
        logger.debug(
            "Drawer %s invalidated → %d downstream artifacts dirtied",
            drawer_id,
            report.count,
        )


def _propagate_drawer_revalidation(
    tracker: DependencyTracker,
    drawer_id: str,
) -> None:
    """Mark all artifacts that depended on this drawer clean again.

    Note: artifacts that were evicted from the cache stay evicted —
    revalidation un-marks dirty but doesn't restore values. The
    next access recomputes from the (now-revalidated) substrate.
    """
    drawer_key = substrate_drawer(drawer_id)
    dependents = tracker.reverse_closure(drawer_key)
    for art in dependents:
        tracker.mark_clean(art)


def _propagate_node_invalidation(
    tracker: DependencyTracker,
    cache: RankerOutputCache,
    node_id: str,
) -> None:
    """Invalidate all known fields of this node + walk downstream."""
    affected = _collect_node_keys(tracker, node_id)
    for key in affected:
        tracker.invalidate(key, propagate=True)
        cache.invalidate_for_substrate_change(key)
    if affected:
        logger.debug(
            "Node %s invalidated → %d substrate keys propagated",
            node_id,
            len(affected),
        )


def _propagate_node_revalidation(
    tracker: DependencyTracker,
    node_id: str,
) -> None:
    """Mark artifacts downstream of this node's substrate keys clean."""
    affected = _collect_node_keys(tracker, node_id)
    for key in affected:
        for art in tracker.reverse_closure(key):
            tracker.mark_clean(art)


def _propagate_edge_invalidation(
    tracker: DependencyTracker,
    cache: RankerOutputCache,
    edge_id: str,
) -> None:
    affected = _collect_edge_keys(tracker, edge_id)
    for key in affected:
        tracker.invalidate(key, propagate=True)
        cache.invalidate_for_substrate_change(key)


def _propagate_edge_revalidation(
    tracker: DependencyTracker,
    edge_id: str,
) -> None:
    affected = _collect_edge_keys(tracker, edge_id)
    for key in affected:
        for art in tracker.reverse_closure(key):
            tracker.mark_clean(art)


def _collect_node_keys(
    tracker: DependencyTracker,
    node_id: str,
) -> list[DependencyKey]:
    """All known DependencyKeys whose identity[0] == node_id and kind
    is node-level (SUBSTRATE_NODE_FIELD, FEATURE, EMBEDDING).
    """
    node_kinds = (
        DependencyKind.SUBSTRATE_NODE_FIELD,
        DependencyKind.FEATURE,
        DependencyKind.EMBEDDING,
    )
    # Iterate the tracker's dependents map (the public API exposes
    # deps_dependents indirectly via reverse_closure but we want a
    # direct enumeration). Use the dependents map's keys — those are
    # all the deps anyone has registered.
    with tracker._lock:
        all_deps = list(tracker._deps_dependents.keys())
    return [
        k for k in all_deps
        if k.kind in node_kinds
        and len(k.identity) >= 1
        and k.identity[0] == node_id
    ]


def _collect_edge_keys(
    tracker: DependencyTracker,
    edge_id: str,
) -> list[DependencyKey]:
    """All known DependencyKeys whose identity[0] == edge_id and kind
    is edge-level (SUBSTRATE_EDGE_FIELD).
    """
    with tracker._lock:
        all_deps = list(tracker._deps_dependents.keys())
    return [
        k for k in all_deps
        if k.kind == DependencyKind.SUBSTRATE_EDGE_FIELD
        and len(k.identity) >= 1
        and k.identity[0] == edge_id
    ]


__all__ = [
    "INVALIDATION_KINDS",
    "is_invalidation_bridge_started",
    "start_invalidation_bridge",
    "stop_invalidation_bridge",
    "tick_invalidation_bridge",
]
