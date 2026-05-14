"""
Ranker-output cache (Track 4A).

Per HANDLES_DESIGN.md v2 §"Cluster-pattern caching": the cache is
default-distinct, keyed by `(query_hash, ranker_name, cluster_signature)`.
Two ranker calls with different cluster signatures produce independent
cache entries — same node can be the right answer under one pattern
and the wrong answer under another, and the cache must not collapse
those.

Track 4A scope: just the default-distinct layer. The projection
mechanism (Track 4B) — auto-promotion to a shared key after observed
equivalence, demotion on divergence — is deferred. The data shape
here leaves room for 4B (the cluster_signature can be substituted
for a "projected" sentinel; the cache key tuple stays fixed).

# What's in scope (4A)

  - `RankerOutputCache` in-memory store.
  - Cache keys include cluster_signature (not just (query_hash,
    ranker_name) like the existing `ranker_output_key`).
  - On cache miss, the caller computes; on hit, returns cached.
  - Wires into the existing `DependencyTracker`: cache entries
    record their substrate-field reads so that substrate changes
    invalidate the right cached entries.
  - Eviction: bounded LRU + expiration on TTL.

# What's deferred (4B+)

  - The projection layer that auto-promotes equivalence-observed
    keys.
  - The `cache_projection_demoted` event emission.
  - Stability caps for promotion-thrashing prevention.

# Why a separate module

The existing `derived/dependency.py` provides `ranker_output_key`
but not a cache for the actual output bytes. The cache lives
alongside the dependency tracker (uses it for invalidation) but
needs its own concerns: TTL, LRU, hit/miss observability. Mixing
into dependency.py would clutter that module.

# Why "cluster_signature: str" not a typed object

The cluster signature is currently produced by
`HandleContext.cluster_pattern.cluster_signature()` (per
HANDLES_DESIGN.md v2 §"Outer skeleton"); it's a stable hash. Pass
the hash in directly; we don't need the typed object here.

Spec ref: HANDLES_DESIGN.md v2 §"Cluster-pattern caching",
IMPLEMENTATION_ROADMAP.md §"Track 4A".
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from .dependency import (
    DependencyKey,
    DependencyKind,
    get_dependency_tracker,
)

if TYPE_CHECKING:
    from ..rank.protocol import ScoredCandidate

logger = logging.getLogger(__name__)


# =============================================================================
# Cache key
# =============================================================================


# Sentinel used when a cache entry has been "projected" to ignore the
# cluster_signature dimension (Track 4B). For Track 4A this is unused;
# the constant lives here so 4B can compose without changing the key
# tuple shape.
PROJECTED_CLUSTER_SIGNATURE = "__projected__"


@dataclass(frozen=True)
class RankerOutputCacheKey:
    """Cache-key tuple for a ranker output.

    Two keys are equal iff all three fields match. The `cluster_signature`
    is what makes two ranker calls under different cluster patterns
    cache distinctly — same `(query_hash, ranker_name)` but different
    cluster signature → different cached entry.
    """

    query_hash: str
    ranker_name: str
    cluster_signature: str
    """Stable hash of the HandleContext.cluster_pattern. Empty string
    for ranker calls that don't carry a HandleContext (legacy callers).
    `PROJECTED_CLUSTER_SIGNATURE` is reserved for Track 4B."""

    def to_dependency_key(self) -> DependencyKey:
        """Convert to a `DependencyKey` so the cache participates in
        the existing dependency tracker.

        Uses `RANKER_OUTPUT` kind. Identity tuple is the full triple
        — the existing helper `ranker_output_key(query_hash,
        ranker_name)` produces a 2-tuple; we produce a 3-tuple so the
        two namespaces don't collide and the dependency tracker can
        invalidate per-cluster-signature.
        """
        return DependencyKey(
            DependencyKind.RANKER_OUTPUT,
            (self.query_hash, self.ranker_name, self.cluster_signature),
        )


# =============================================================================
# Cache entry
# =============================================================================


@dataclass
class RankerOutputCacheEntry:
    """One cached ranker-output blob."""

    key: RankerOutputCacheKey
    scored_candidates: list  # list[ScoredCandidate]; type elided to avoid import cycle

    cached_at_ms: int
    last_accessed_ms: int
    expires_at_ms: int

    hit_count: int = 0
    """How many times this entry has been served from cache."""

    substrate_deps: list[DependencyKey] = field(default_factory=list)
    """Substrate dependencies recorded at compute time. Used by the
    dependency tracker for selective invalidation."""

    def is_expired(self, now_ms: int) -> bool:
        return now_ms >= self.expires_at_ms


# =============================================================================
# Cache
# =============================================================================


DEFAULT_TTL_SEC = 300.0  # 5 minutes
DEFAULT_MAX_ENTRIES = 4096


class RankerOutputCache:
    """In-memory cache of ranker outputs.

    Default-distinct keying: `(query_hash, ranker_name, cluster_signature)`.

    LRU eviction at `max_entries`; entries expire after `ttl_sec` seconds.
    Wires into `DependencyTracker` so that substrate changes invalidate
    matching cache entries automatically.

    Thread-safe.

    # Use

        cache = RankerOutputCache()
        key = RankerOutputCacheKey("q1_hash", "factored", "cluster_sig_a")
        hit = cache.get(key)
        if hit is None:
            # Caller computes ranked candidates...
            cache.put(
                key,
                ranked,
                substrate_deps=[substrate_field("nde_x", "name"), ...],
            )

    # Why list, not dict on `scored_candidates`

    Output of a ranker is an ordered list (highest-score first). Cache
    preserves order.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._ttl_sec = ttl_sec
        # OrderedDict so we get LRU semantics with O(1) ops
        self._entries: "OrderedDict[RankerOutputCacheKey, RankerOutputCacheEntry]" = (
            OrderedDict()
        )

        # Reverse index: substrate dep → set of cache keys that depend
        # on it. Lets `invalidate_for_substrate_change` find affected
        # entries without iterating the whole cache.
        self._dep_to_keys: dict[DependencyKey, set[RankerOutputCacheKey]] = {}

        # Diagnostic counters
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0

    # -------- core API -------------------------------------------------------

    def get(
        self,
        key: RankerOutputCacheKey,
        *,
        now_ms: int | None = None,
    ) -> list | None:
        """Return cached ScoredCandidates or None on miss/expiry.

        On hit, marks the entry as most-recently-used (LRU bump) and
        increments hit_count.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None

            if entry.is_expired(now_ms):
                # Lazy expiry — clear the stale entry
                self._remove_entry_locked(key)
                self._misses += 1
                return None

            # Cache hit: bump LRU + counters
            self._entries.move_to_end(key)
            entry.last_accessed_ms = now_ms
            entry.hit_count += 1
            self._hits += 1
            return list(entry.scored_candidates)

    def put(
        self,
        key: RankerOutputCacheKey,
        scored_candidates: list,
        *,
        substrate_deps: list[DependencyKey] | None = None,
        now_ms: int | None = None,
        ttl_sec: float | None = None,
    ) -> None:
        """Cache a ranker output.

        `substrate_deps` is the list of dependencies the compute read.
        These are wired through the dependency tracker so future
        invalidations propagate. Pass an empty list (or None) for
        "no dependencies" (e.g. a synthetic test fixture).
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        ttl = ttl_sec if ttl_sec is not None else self._ttl_sec
        expires = now_ms + int(ttl * 1000)

        deps = list(substrate_deps or [])
        with self._lock:
            # If overwriting, clean up the old reverse-index first
            if key in self._entries:
                self._remove_entry_locked(key)

            entry = RankerOutputCacheEntry(
                key=key,
                scored_candidates=list(scored_candidates),
                cached_at_ms=now_ms,
                last_accessed_ms=now_ms,
                expires_at_ms=expires,
                substrate_deps=deps,
            )
            self._entries[key] = entry
            self._entries.move_to_end(key)

            # Reverse-index for fast invalidation lookups
            for dep in deps:
                self._dep_to_keys.setdefault(dep, set()).add(key)

            # Wire into the dependency tracker
            tracker = get_dependency_tracker()
            artifact_key = key.to_dependency_key()
            for dep in deps:
                tracker.record_dependency(artifact_key, dep)

            # LRU eviction if over budget
            self._evict_if_needed_locked()

    def invalidate(self, key: RankerOutputCacheKey) -> bool:
        """Remove one specific cached entry.

        Returns True if the entry was present and removed.
        """
        with self._lock:
            if key not in self._entries:
                return False
            self._remove_entry_locked(key)
            self._invalidations += 1
            return True

    def invalidate_for_substrate_change(
        self,
        substrate_dep: DependencyKey,
    ) -> int:
        """Invalidate every cached entry that recorded a dependency on
        `substrate_dep`.

        Returns the number of entries invalidated.

        Production callers wire this through the existing
        dependency-tracker invalidation flow: when a substrate change
        fires, the tracker computes the closure; matching ranker-output
        keys are reported back here for cache eviction.
        """
        with self._lock:
            keys = self._dep_to_keys.get(substrate_dep, set()).copy()
            count = 0
            for k in keys:
                if k in self._entries:
                    self._remove_entry_locked(k)
                    count += 1
            self._invalidations += count
            return count

    def clear(self) -> None:
        """Drop all cached entries. Diagnostic / test-helper."""
        with self._lock:
            self._entries.clear()
            self._dep_to_keys.clear()

    # -------- introspection --------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: RankerOutputCacheKey) -> bool:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            return not entry.is_expired(int(time.time() * 1000))

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "invalidations": self._invalidations,
            }

    def keys(self) -> Iterator[RankerOutputCacheKey]:
        """Iterate cached keys (snapshot). Test helper."""
        with self._lock:
            return iter(list(self._entries.keys()))

    # -------- internals ------------------------------------------------------

    def _remove_entry_locked(self, key: RankerOutputCacheKey) -> None:
        """Drop one entry + clean up the reverse index. Caller holds lock."""
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        for dep in entry.substrate_deps:
            owners = self._dep_to_keys.get(dep)
            if owners is None:
                continue
            owners.discard(key)
            if not owners:
                del self._dep_to_keys[dep]

    def _evict_if_needed_locked(self) -> None:
        """LRU eviction down to max_entries. Caller holds lock."""
        while len(self._entries) > self._max_entries:
            # popitem(last=False) removes the LRU entry (oldest)
            oldest_key, _oldest_entry = self._entries.popitem(last=False)
            # Clean up reverse index for the evicted entry
            for dep, owners in list(self._dep_to_keys.items()):
                owners.discard(oldest_key)
                if not owners:
                    del self._dep_to_keys[dep]
            self._evictions += 1


# =============================================================================
# Process-wide singleton
# =============================================================================


_DEFAULT_CACHE: RankerOutputCache | None = None
_DEFAULT_CACHE_LOCK = threading.Lock()


def get_default_cache() -> RankerOutputCache:
    """Return the process-wide default cache, creating one if needed."""
    global _DEFAULT_CACHE
    with _DEFAULT_CACHE_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = RankerOutputCache()
        return _DEFAULT_CACHE


def set_default_cache(cache: RankerOutputCache | None) -> None:
    """Replace the default cache. `None` clears it (next get_default_cache
    creates a fresh one). Test helper; production rarely calls this."""
    global _DEFAULT_CACHE
    with _DEFAULT_CACHE_LOCK:
        _DEFAULT_CACHE = cache


def reset_default_cache() -> None:
    """Drop the default cache so the next `get_default_cache()` makes
    a fresh one. Test cleanup; idempotent."""
    set_default_cache(None)


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SEC",
    "PROJECTED_CLUSTER_SIGNATURE",
    "RankerOutputCache",
    "RankerOutputCacheEntry",
    "RankerOutputCacheKey",
    "get_default_cache",
    "reset_default_cache",
    "set_default_cache",
]
