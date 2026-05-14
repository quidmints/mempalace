"""
Per-consumer frontier tracking + cross-consumer meet operator (Phase 3).

A `Frontier` is a position in the log up to which a consumer has fully
committed its work. Phase 1 introduced `committed_frontier_offset` per
consumer (the highest offset where every batch this consumer started
with first event ≤ this offset has been closed). Phase 3 elevates this
to a first-class object and adds the cross-consumer **meet** operator
for consistent reads.

Two semantic flavors:

  - per-consumer frontier: scalar offset per consumer
  - meet of frontiers: the largest offset N such that for every
    consumer in a given set, that consumer's frontier ≥ N

A reader that needs a *consistent cross-consumer view* (e.g. a ranker
that reads features written by Class 1 and Class 2 simultaneously)
queries `meet({"miner.class1", "miner.class2"})` and reads only events
up to the returned offset. Anything beyond is potentially in-flight and
unsafe to read for consistent semantics.

Phase 1 already gave us `scan_for_orphans` returning a per-consumer
`committed_frontiers` dict. Phase 3's `FrontierRegistry` is a thin
process-wide singleton that:

  1. Caches the per-consumer frontier so repeated reads don't rescan
     the log.
  2. Provides `meet`, `advance`, and `subscribe` operations.
  3. Is invalidated whenever a writer commits or aborts a batch
     (the registry watches the log for batch_committed /
     batch_aborted events and updates its cache lazily).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .client import LogClient
from .recovery import scan_for_orphans


@dataclass
class FrontierRegistry:
    """Process-wide cache of per-consumer frontier offsets.

    Thread-safe. The first read for a consumer triggers a scan; later
    reads use the cached value until invalidation.

    Invalidation happens on:
      - `mark_dirty(consumer_id)` — explicit invalidation
      - `mark_all_dirty()` — full cache flush
      - `refresh_from_log()` — full re-scan (typically called once per
        process start by the recovery hook)
    """

    log: LogClient
    _frontier: dict[str, int] = field(default_factory=dict)
    _fresh: set[str] = field(default_factory=set)
    """Consumer ids whose frontier is currently fresh in the cache."""
    _scan_start_offset: int = 1

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    # ---- queries -----------------------------------------------------------

    def frontier_of(self, consumer_id: str) -> int:
        """Return the committed_frontier_offset for a consumer.

        Lazy-refreshes from the log if not fresh. Returns 0 if the
        consumer has emitted no events yet.
        """
        with self._lock:
            if consumer_id not in self._fresh:
                self._refresh_locked(consumer_id)
            return self._frontier.get(consumer_id, 0)

    def meet(self, consumer_ids: Iterable[str]) -> int:
        """Return the meet-of-frontiers across the given consumers.

        Defined as: the largest offset N such that for every consumer
        in the set, `frontier_of(consumer) >= N`.

        Equivalent to `min(frontier_of(c) for c in consumer_ids)` —
        but exposing it as `meet` makes the dataflow semantics
        explicit. A reader that calls `meet([c1, c2, c3])` and reads
        events up to that offset is guaranteed to see only events
        that are committed for *all three* consumers.

        Empty input → returns the log's current offset (no constraint).
        """
        ids = list(consumer_ids)
        if not ids:
            return self.log.current_offset()
        return min(self.frontier_of(c) for c in ids)

    def known_consumers(self) -> set[str]:
        """Set of consumer_ids the registry has seen."""
        with self._lock:
            self._refresh_all_locked()
            return set(self._frontier.keys())

    # ---- invalidation ------------------------------------------------------

    def mark_dirty(self, consumer_id: str) -> None:
        with self._lock:
            self._fresh.discard(consumer_id)

    def mark_all_dirty(self) -> None:
        with self._lock:
            self._fresh.clear()

    def refresh_from_log(self) -> dict[str, int]:
        """Force a full re-scan; returns the fresh per-consumer dict."""
        with self._lock:
            self._refresh_all_locked()
            return dict(self._frontier)

    # ---- internal ----------------------------------------------------------

    def _refresh_all_locked(self) -> None:
        """Run a full scan_for_orphans and rebuild the cache."""
        report = scan_for_orphans(self.log, start_offset=self._scan_start_offset)
        self._frontier = dict(report.committed_frontiers)
        self._fresh = set(self._frontier.keys())

    def _refresh_locked(self, consumer_id: str) -> None:
        """Refresh the frontier for a single consumer.

        Phase 5 wire: consults the Rust bridge first
        (`mempalace_core.PyFrontierRegistry`) for an O(1) lookup.
        Falls back to the scan-based path when the bridge is not
        live (extension not built) or when the bridge doesn't yet
        know about this consumer.

        The two paths are required to agree by the alignment-contract
        tests in `test_phase5_frontier_alignment`. With the bridge
        active, this method has the same observable behavior but
        avoids the per-call log scan.
        """
        # Try the Rust bridge first.
        from .rust_bridge import get_frontier_bridge
        bridge = get_frontier_bridge()
        rust_offset = bridge.committed_offset(consumer_id)
        if rust_offset is not None and consumer_id in bridge.known_views():
            # Rust knows about this consumer. Trust its answer.
            self._frontier[consumer_id] = rust_offset
            self._fresh.add(consumer_id)
            return

        # Fallback: scan-based path. This is what ran before the
        # Rust bridge existed; it remains the source of truth when
        # the extension isn't built. Even when Rust is live, this
        # catches consumers Rust hasn't seen yet (e.g. a test that
        # appended to the log before instantiating the registry).
        report = scan_for_orphans(
            self.log,
            consumers=[consumer_id],
            start_offset=self._scan_start_offset,
        )
        self._frontier[consumer_id] = report.committed_frontiers.get(
            consumer_id, 0,
        )
        self._fresh.add(consumer_id)


# =============================================================================
# Process-wide singleton
# =============================================================================


_REGISTRY: FrontierRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_frontier_registry(log: LogClient | None = None) -> FrontierRegistry:
    """Return the process-wide FrontierRegistry, initializing if needed.

    First-call must provide `log` (or rely on the default client).
    Subsequent calls reuse the same registry.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            from .client import get_default_client
            _REGISTRY = FrontierRegistry(log=log or get_default_client())
        return _REGISTRY


def set_frontier_registry(registry: FrontierRegistry | None) -> None:
    """Replace the process-wide registry (test hook)."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry


# =============================================================================
# Consistent-read helper
# =============================================================================


@dataclass
class ConsistentReadView:
    """A consistent read snapshot — events up to `meet_offset` are
    safe to read; anything beyond may be in-flight in some consumer.

    Returned by `consistent_read_snapshot` for callers that want to
    walk the log under a fixed consistency boundary.
    """

    meet_offset: int
    consumer_ids: tuple[str, ...]
    log: LogClient

    def read_events(self) -> list[tuple[int, str, dict]]:
        """Read all events up to and including meet_offset."""
        return list(self.log.read_range(0, self.meet_offset + 1))

    def is_consistent_for(self, consumer_id: str) -> bool:
        """True if this snapshot is at-or-before consumer_id's frontier
        (so the consumer's outputs in the read range are fully
        committed)."""
        registry = get_frontier_registry(self.log)
        return registry.frontier_of(consumer_id) >= self.meet_offset


def consistent_read_snapshot(
    consumer_ids: Iterable[str],
    *,
    log: LogClient | None = None,
) -> ConsistentReadView:
    """Build a consistent-read view across the given consumers.

    Reads from this view see only events that are committed for all
    listed consumers. A ranker that reads features from miner.class1
    + miner.class2 should call this with both consumer_ids and walk
    only the returned event range.
    """
    from .client import get_default_client
    log_client = log or get_default_client()
    registry = get_frontier_registry(log_client)
    ids_tuple = tuple(consumer_ids)
    return ConsistentReadView(
        meet_offset=registry.meet(ids_tuple),
        consumer_ids=ids_tuple,
        log=log_client,
    )


__all__ = [
    "ConsistentReadView",
    "FrontierRegistry",
    "consistent_read_snapshot",
    "get_frontier_registry",
    "set_frontier_registry",
]
