"""Tests for Track 4A — default-distinct ranker output cache.

Covers:
  - Cache key equality semantics (same triple == same key, any
    dimension different == different key).
  - Get/put roundtrip.
  - Distinct cluster_signatures cache distinctly (the central
    correctness property).
  - Hit counts, miss counts, stats.
  - LRU eviction at max_entries.
  - TTL expiry returns miss.
  - Substrate-change invalidation.
  - Wires into the dependency tracker for transitive invalidation.
  - Default cache singleton behavior.
"""

from __future__ import annotations

import time
import unittest

from mempalace.derived.dependency import (
    DependencyKey,
    DependencyKind,
    edge_field,
    get_dependency_tracker,
    substrate_field,
)
from mempalace.derived.ranker_cache import (
    DEFAULT_MAX_ENTRIES,
    PROJECTED_CLUSTER_SIGNATURE,
    RankerOutputCache,
    RankerOutputCacheKey,
    get_default_cache,
    reset_default_cache,
    set_default_cache,
)


# =============================================================================
# Cache key
# =============================================================================


class TestCacheKey(unittest.TestCase):
    def test_equality_requires_all_three_fields(self) -> None:
        a = RankerOutputCacheKey("q1", "factored", "sig_a")
        b = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_different_query_hash_distinct(self) -> None:
        a = RankerOutputCacheKey("q1", "factored", "sig_a")
        b = RankerOutputCacheKey("q2", "factored", "sig_a")
        self.assertNotEqual(a, b)

    def test_different_ranker_distinct(self) -> None:
        a = RankerOutputCacheKey("q1", "factored", "sig_a")
        b = RankerOutputCacheKey("q1", "neural", "sig_a")
        self.assertNotEqual(a, b)

    def test_different_cluster_signature_distinct(self) -> None:
        """The Track 4A central property: distinct cluster signatures
        are distinct cache keys."""
        a = RankerOutputCacheKey("q1", "factored", "sig_a")
        b = RankerOutputCacheKey("q1", "factored", "sig_b")
        self.assertNotEqual(a, b)

    def test_to_dependency_key(self) -> None:
        k = RankerOutputCacheKey("q1", "factored", "sig_a")
        dep = k.to_dependency_key()
        self.assertEqual(dep.kind, DependencyKind.RANKER_OUTPUT)
        self.assertEqual(dep.identity, ("q1", "factored", "sig_a"))


# =============================================================================
# Get / put basics
# =============================================================================


class TestCacheBasics(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = RankerOutputCache(max_entries=100, ttl_sec=60.0)

    def test_miss_returns_none(self) -> None:
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.assertIsNone(self.cache.get(key))

    def test_put_then_get_returns_cached(self) -> None:
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        candidates = ["cand_a", "cand_b", "cand_c"]
        self.cache.put(key, candidates)
        result = self.cache.get(key)
        self.assertEqual(result, candidates)

    def test_get_returns_copy_not_internal_reference(self) -> None:
        """Defensive: callers mutating the returned list shouldn't
        corrupt the cache."""
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.cache.put(key, ["a", "b"])
        first = self.cache.get(key)
        first.append("x")
        second = self.cache.get(key)
        self.assertEqual(second, ["a", "b"])

    def test_overwrite_same_key(self) -> None:
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.cache.put(key, ["v1"])
        self.cache.put(key, ["v2", "v3"])
        result = self.cache.get(key)
        self.assertEqual(result, ["v2", "v3"])

    def test_contains_check(self) -> None:
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.assertNotIn(key, self.cache)
        self.cache.put(key, ["x"])
        self.assertIn(key, self.cache)

    def test_len(self) -> None:
        self.assertEqual(len(self.cache), 0)
        self.cache.put(RankerOutputCacheKey("q1", "factored", "sig_a"), ["x"])
        self.assertEqual(len(self.cache), 1)
        self.cache.put(RankerOutputCacheKey("q2", "factored", "sig_a"), ["y"])
        self.assertEqual(len(self.cache), 2)


# =============================================================================
# Distinct cluster signatures cache distinctly (Track 4A central correctness)
# =============================================================================


class TestDistinctClusterSignatures(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = RankerOutputCache()

    def test_distinct_clusters_cache_distinctly(self) -> None:
        """The same (query, ranker) under two different cluster
        signatures must produce two distinct cache entries."""
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q1", "factored", "sig_b")

        # Same node ranks differently under the two patterns
        self.cache.put(ka, ["nde_x_high"])
        self.cache.put(kb, ["nde_x_low"])

        self.assertEqual(self.cache.get(ka), ["nde_x_high"])
        self.assertEqual(self.cache.get(kb), ["nde_x_low"])
        self.assertEqual(len(self.cache), 2)

    def test_invalidating_one_does_not_affect_the_other(self) -> None:
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q1", "factored", "sig_b")
        self.cache.put(ka, ["a"])
        self.cache.put(kb, ["b"])

        self.cache.invalidate(ka)
        self.assertIsNone(self.cache.get(ka))
        self.assertEqual(self.cache.get(kb), ["b"])


# =============================================================================
# Stats
# =============================================================================


class TestCacheStats(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = RankerOutputCache()

    def test_hit_miss_counters(self) -> None:
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        self.cache.get(key)  # miss 1
        self.cache.get(key)  # miss 2
        self.cache.put(key, ["a"])
        self.cache.get(key)  # hit 1
        self.cache.get(key)  # hit 2
        self.cache.get(key)  # hit 3
        stats = self.cache.stats()
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats["hits"], 3)
        self.assertEqual(stats["entries"], 1)


# =============================================================================
# LRU eviction
# =============================================================================


class TestLRUEviction(unittest.TestCase):
    def test_lru_evicts_oldest_when_at_capacity(self) -> None:
        cache = RankerOutputCache(max_entries=3)
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q2", "factored", "sig_a")
        kc = RankerOutputCacheKey("q3", "factored", "sig_a")
        kd = RankerOutputCacheKey("q4", "factored", "sig_a")

        cache.put(ka, ["a"])
        cache.put(kb, ["b"])
        cache.put(kc, ["c"])
        # Access ka so it's most-recently-used
        cache.get(ka)
        # Insert kd, should evict kb (LRU)
        cache.put(kd, ["d"])

        self.assertEqual(len(cache), 3)
        self.assertIsNotNone(cache.get(ka))
        self.assertIsNone(cache.get(kb))  # evicted
        self.assertIsNotNone(cache.get(kc))
        self.assertIsNotNone(cache.get(kd))
        self.assertGreaterEqual(cache.stats()["evictions"], 1)

    def test_eviction_cleans_reverse_index(self) -> None:
        """When LRU evicts an entry, its substrate_deps reverse-index
        entries should be cleaned up (otherwise we leak memory and
        can mistakenly invalidate a later, unrelated put)."""
        cache = RankerOutputCache(max_entries=2)
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q2", "factored", "sig_a")
        kc = RankerOutputCacheKey("q3", "factored", "sig_a")

        dep_x = substrate_field("nde_x", "name")
        cache.put(ka, ["a"], substrate_deps=[dep_x])
        cache.put(kb, ["b"], substrate_deps=[dep_x])
        cache.put(kc, ["c"], substrate_deps=[dep_x])
        # ka should be evicted (LRU, since kb and kc are newer)

        # Internal: reverse-index should still contain dep_x but only for kb, kc
        # Public API: invalidate_for_substrate_change reports correct count
        n = cache.invalidate_for_substrate_change(dep_x)
        self.assertEqual(n, 2)


# =============================================================================
# TTL expiry
# =============================================================================


class TestTTLExpiry(unittest.TestCase):
    def test_expired_entry_returns_miss(self) -> None:
        cache = RankerOutputCache(ttl_sec=10.0)
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        cache.put(key, ["a"], now_ms=1000)
        # Same time = hit
        self.assertEqual(cache.get(key, now_ms=1000), ["a"])
        # Within TTL = hit
        self.assertEqual(cache.get(key, now_ms=5000), ["a"])
        # After TTL = miss, entry removed
        self.assertIsNone(cache.get(key, now_ms=11_001))
        self.assertNotIn(key, cache)

    def test_per_put_ttl_overrides_default(self) -> None:
        cache = RankerOutputCache(ttl_sec=60.0)
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        cache.put(key, ["a"], now_ms=1000, ttl_sec=2.0)
        self.assertEqual(cache.get(key, now_ms=2999), ["a"])
        self.assertIsNone(cache.get(key, now_ms=3001))


# =============================================================================
# Substrate-change invalidation
# =============================================================================


class TestSubstrateInvalidation(unittest.TestCase):
    def test_invalidate_for_substrate_change_drops_dependent_entries(self) -> None:
        cache = RankerOutputCache()
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q2", "factored", "sig_a")
        kc = RankerOutputCacheKey("q3", "factored", "sig_a")
        dep_x = substrate_field("nde_x", "name")
        dep_y = substrate_field("nde_y", "name")

        cache.put(ka, ["a"], substrate_deps=[dep_x])
        cache.put(kb, ["b"], substrate_deps=[dep_x, dep_y])
        cache.put(kc, ["c"], substrate_deps=[dep_y])

        # Substrate change at nde_x.name → ka and kb invalidate
        n = cache.invalidate_for_substrate_change(dep_x)
        self.assertEqual(n, 2)
        self.assertIsNone(cache.get(ka))
        self.assertIsNone(cache.get(kb))
        self.assertEqual(cache.get(kc), ["c"])

    def test_invalidate_idempotent(self) -> None:
        cache = RankerOutputCache()
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        dep = substrate_field("nde_x", "name")
        cache.put(key, ["a"], substrate_deps=[dep])

        n1 = cache.invalidate_for_substrate_change(dep)
        n2 = cache.invalidate_for_substrate_change(dep)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)


# =============================================================================
# Dependency tracker integration
# =============================================================================


class TestDependencyTrackerIntegration(unittest.TestCase):
    """When `put` is called with substrate_deps, the entry registers
    in the dependency tracker. Tracker invalidation can then be
    propagated to the cache by the caller."""

    def setUp(self) -> None:
        # Fresh tracker per test by clearing the singleton's state
        from mempalace.derived.dependency import _TRACKER  # noqa
        # Reset by instantiating a new tracker and swapping
        from mempalace.derived import dependency as dep_mod
        dep_mod._TRACKER = None

    def test_put_records_deps_in_tracker(self) -> None:
        cache = RankerOutputCache()
        key = RankerOutputCacheKey("q1", "factored", "sig_a")
        dep = substrate_field("nde_x", "name")
        cache.put(key, ["a"], substrate_deps=[dep])

        tracker = get_dependency_tracker()
        artifact = key.to_dependency_key()
        deps = tracker.dependencies_of(artifact)
        self.assertIn(dep, deps)

    def test_distinct_cluster_artifacts_track_independently(self) -> None:
        """Two cache entries with the same (query, ranker) but different
        cluster signatures should appear as DIFFERENT artifacts in the
        tracker — i.e. the tracker can invalidate one without dirtying
        the other."""
        cache = RankerOutputCache()
        ka = RankerOutputCacheKey("q1", "factored", "sig_a")
        kb = RankerOutputCacheKey("q1", "factored", "sig_b")
        dep_a = substrate_field("nde_x", "name")
        dep_b = substrate_field("nde_y", "name")
        cache.put(ka, ["a"], substrate_deps=[dep_a])
        cache.put(kb, ["b"], substrate_deps=[dep_b])

        tracker = get_dependency_tracker()
        # Distinct DependencyKeys
        self.assertNotEqual(ka.to_dependency_key(), kb.to_dependency_key())

        # Invalidating dep_a marks ka but not kb
        report = tracker.invalidate(dep_a)
        dirty_artifacts = set(report.invalidated_keys)
        self.assertIn(ka.to_dependency_key(), dirty_artifacts)
        self.assertNotIn(kb.to_dependency_key(), dirty_artifacts)


# =============================================================================
# Default singleton
# =============================================================================


class TestDefaultCache(unittest.TestCase):
    def setUp(self) -> None:
        reset_default_cache()

    def tearDown(self) -> None:
        reset_default_cache()

    def test_get_default_cache_returns_singleton(self) -> None:
        a = get_default_cache()
        b = get_default_cache()
        self.assertIs(a, b)

    def test_set_default_cache_replaces(self) -> None:
        custom = RankerOutputCache(max_entries=7)
        set_default_cache(custom)
        self.assertIs(get_default_cache(), custom)

    def test_reset_creates_fresh_on_next_get(self) -> None:
        first = get_default_cache()
        first.put(RankerOutputCacheKey("q1", "f", "s"), ["x"])
        reset_default_cache()
        second = get_default_cache()
        self.assertIsNot(first, second)
        self.assertEqual(len(second), 0)


if __name__ == "__main__":
    unittest.main()
