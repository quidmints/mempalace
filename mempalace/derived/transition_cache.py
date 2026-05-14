"""
Pairwise drawer-coherence transition cache.

Per Part 5.4: when the composer (montage builder, claude-thread retrieval)
needs to know "is drawer A coherent with drawer B as a sequence?", that
pairwise check is expensive (semantic similarity + structural overlap +
narrative continuity). Cache it.

The cache key is (source_drawer_id, target_drawer_id, coherence_kind).
Coherence kinds:
  - SEMANTIC      : embedding similarity
  - STRUCTURAL    : period-/event-overlap continuity
  - NARRATIVE     : derivation-chain bridge (do they share an assertion?)
  - FULL          : weighted combination

Population is lazy — the composer asks for a coherence, we compute and
cache. Background refresh re-computes stale entries when the underlying
inputs change (drawer amended, new derived_from edges).

Cache state is held in memory; a future RocksDB-backed variant lives
behind the same query API.

Spec ref: Part 5.4, Part 8.1.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..views.current import _get_store
from .base import DerivedRepresentation


class CoherenceKind(str, Enum):
    SEMANTIC = "semantic"
    STRUCTURAL = "structural"
    NARRATIVE = "narrative"
    FULL = "full"


@dataclass
class CoherenceEntry:
    source_id: str
    target_id: str
    kind: CoherenceKind
    score: float           # in [0, 1]
    computed_at_ms: int
    inputs_hash: str       # changes on amend/edge-mutation; triggers refresh
    valid: bool = True


# =============================================================================
# Cache
# =============================================================================


class TransitionCache(DerivedRepresentation):
    """Lazy-populated, event-invalidated pairwise coherence cache."""

    name = "derived.transition_cache"
    subscribed_kinds = (
        "drawer_amended",
        "edge_created",
        "edge_invalidated",
        "interpretation_assigned",
    )

    def __init__(self, *, max_entries: int = 100_000, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: dict[tuple[str, str, str], CoherenceEntry] = {}
        self._max_entries = max_entries
        self._cache_lock = threading.Lock()

    def reset_state(self) -> None:
        with self._cache_lock:
            self._entries.clear()

    # ---- subscriber side: invalidate on any structural change --------------

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        # Invalidate entries whose source or target match the changed node
        affected_id = (
            payload.get("drawer_id")
            or payload.get("source_node_id")
            or payload.get("target_node_id")
            or payload.get("node_id")
        )
        if not affected_id:
            return
        with self._cache_lock:
            for key in list(self._entries.keys()):
                src, tgt, _ = key
                if src == affected_id or tgt == affected_id:
                    self._entries[key].valid = False

    # ---- query side: compute-or-cache ---------------------------------------

    def get_or_compute(
        self,
        source_id: str,
        target_id: str,
        kind: CoherenceKind = CoherenceKind.FULL,
        compute_fn=None,
    ) -> CoherenceEntry:
        """Return a cached entry or compute and store one.

        compute_fn(source_id, target_id, kind) -> float in [0, 1]
        """
        key = (source_id, target_id, kind.value)
        with self._cache_lock:
            entry = self._entries.get(key)
            if entry is not None and entry.valid:
                return entry

        # Compute
        if compute_fn is None:
            score = self._default_compute(source_id, target_id, kind)
        else:
            score = float(compute_fn(source_id, target_id, kind))

        # Generate an inputs hash for staleness detection
        store = _get_store()
        src_node = store.nodes.get(source_id)
        tgt_node = store.nodes.get(target_id)
        inputs_token = (
            f"{src_node.last_modified_at_offset if src_node else 0}"
            f":{tgt_node.last_modified_at_offset if tgt_node else 0}"
            f":{kind.value}"
        )

        new_entry = CoherenceEntry(
            source_id=source_id,
            target_id=target_id,
            kind=kind,
            score=max(0.0, min(1.0, score)),
            computed_at_ms=int(time.time() * 1000),
            inputs_hash=inputs_token,
            valid=True,
        )

        with self._cache_lock:
            # LRU eviction at max size: drop oldest
            if len(self._entries) >= self._max_entries:
                # Evict the oldest 10% to amortize
                evict_n = max(1, self._max_entries // 10)
                sorted_keys = sorted(
                    self._entries.keys(),
                    key=lambda k: self._entries[k].computed_at_ms,
                )
                for k in sorted_keys[:evict_n]:
                    self._entries.pop(k, None)
            self._entries[key] = new_entry

        return new_entry

    def _default_compute(
        self, source_id: str, target_id: str, kind: CoherenceKind
    ) -> float:
        """Cheap structural-only fallback. Real compute is in the composer."""
        store = _get_store()
        if source_id == target_id:
            return 1.0
        # Structural: do they share a containing period? Approximate via
        # incoming CONTAINS edges' source nodes.
        src_in = [
            store.edges[eid]
            for eid in store.incoming.get(source_id, [])
            if eid in store.edges and store.edges[eid].edge_kind == "contains"
        ]
        tgt_in = [
            store.edges[eid]
            for eid in store.incoming.get(target_id, [])
            if eid in store.edges and store.edges[eid].edge_kind == "contains"
        ]
        src_parents = {e.source_node_id for e in src_in}
        tgt_parents = {e.source_node_id for e in tgt_in}
        if src_parents & tgt_parents:
            return 0.7  # share a parent → moderately coherent
        return 0.2

    # ---- introspection ------------------------------------------------------

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._entries)

    def invalidate_for(self, node_id: str) -> int:
        """Manually invalidate all entries touching a node. Returns count."""
        count = 0
        with self._cache_lock:
            for key, entry in self._entries.items():
                if key[0] == node_id or key[1] == node_id:
                    entry.valid = False
                    count += 1
        return count


__all__ = ["CoherenceEntry", "CoherenceKind", "TransitionCache"]
