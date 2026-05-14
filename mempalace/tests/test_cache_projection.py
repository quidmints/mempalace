"""Tests for Track 4B — cache projection.

Covers:
  - Equivalence helpers: deep_equality, top_k_node_ids_match.
  - K consistent matches → promotion.
  - Divergence (1 mismatch) blocks promotion.
  - lookup() routes to projected entry when promoted.
  - Divergence after promotion within demotion window → demotion.
  - Cooldown blocks re-promotion within the cooldown window.
  - Stability cap → UNSTABLE state after N cycles within window.
  - Audit events emitted (CacheProjectionPromoted, _Demoted, _Unstable).
  - Lookup falls back to signature-specific key when not promoted.
  - Projection-key writes don't trigger feedback loop.
"""

from __future__ import annotations

import unittest

from mempalace.derived.cache_projection import (
    CacheProjection,
    PROMOTION_THRESHOLD,
    ProjectionStatus,
    deep_equality,
    top_k_node_ids_match,
)
from mempalace.derived.ranker_cache import (
    PROJECTED_CLUSTER_SIGNATURE,
    RankerOutputCache,
    RankerOutputCacheKey,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state


# =============================================================================
# Equivalence helpers
# =============================================================================


class TestEquivalenceHelpers(unittest.TestCase):
    def test_deep_equality_matches(self) -> None:
        self.assertTrue(deep_equality([1, 2, 3], [1, 2, 3]))
        self.assertTrue(deep_equality({"a": 1}, {"a": 1}))

    def test_deep_equality_mismatches(self) -> None:
        self.assertFalse(deep_equality([1, 2, 3], [1, 2, 4]))
        self.assertFalse(deep_equality({"a": 1}, {"a": 2}))

    def test_top_k_matches_when_top_n_node_ids_align(self) -> None:
        check = top_k_node_ids_match(3)

        # Use simple dicts as candidates (the helper supports this shape)
        list_a = [{"node_id": "n1"}, {"node_id": "n2"}, {"node_id": "n3"},
                  {"node_id": "n_extra"}]
        list_b = [{"node_id": "n1"}, {"node_id": "n2"}, {"node_id": "n3"}]
        self.assertTrue(check(list_a, list_b))

    def test_top_k_fails_on_order_mismatch(self) -> None:
        check = top_k_node_ids_match(3)
        list_a = [{"node_id": "n1"}, {"node_id": "n2"}, {"node_id": "n3"}]
        list_b = [{"node_id": "n2"}, {"node_id": "n1"}, {"node_id": "n3"}]
        self.assertFalse(check(list_a, list_b))

    def test_top_k_handles_empty(self) -> None:
        check = top_k_node_ids_match(3)
        self.assertTrue(check([], []))
        self.assertFalse(check([], [{"node_id": "n1"}]))

    def test_top_k_uses_min_k(self) -> None:
        """If both lists are shorter than k, compare what's available."""
        check = top_k_node_ids_match(10)
        list_a = [{"node_id": "n1"}, {"node_id": "n2"}]
        list_b = [{"node_id": "n1"}, {"node_id": "n2"}]
        self.assertTrue(check(list_a, list_b))

    def test_top_k_supports_string_candidates(self) -> None:
        """Test fixtures often use plain strings."""
        check = top_k_node_ids_match(2)
        self.assertTrue(check(["a", "b"], ["a", "b"]))
        self.assertFalse(check(["a", "b"], ["b", "a"]))


# =============================================================================
# Promotion
# =============================================================================


def _put_with_signature(
    cache: RankerOutputCache,
    projection: CacheProjection,
    qh: str,
    rn: str,
    sig: str,
    value: list,
    *,
    now_ms: int = 1_000_000,
) -> None:
    """Helper: cache.put + projection.observe_put.

    Uses a long TTL so test entries don't expire during the test.
    """
    key = RankerOutputCacheKey(qh, rn, sig)
    cache.put(key, value, now_ms=now_ms, ttl_sec=10**9)
    projection.observe_put(key, value, now_ms=now_ms)


class TestPromotion(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.cache = RankerOutputCache(max_entries=1024)
        self.projection = CacheProjection(
            cache=self.cache,
            equivalence_fn=deep_equality,
        )

    def test_starts_not_promoted(self) -> None:
        self.assertEqual(
            self.projection.status("q-h", "r-v1"),
            ProjectionStatus.NOT_PROMOTED,
        )

    def test_k_consistent_matches_promotes(self) -> None:
        """Need K equivalence observations to trigger promotion.
        Each new put compares against ALL existing peers, so to
        accumulate K observations we need either:
          - K peer signatures present + 1 final put = K observations
          - or many separate puts each adding observations
        """
        # Seed with N peers — first one creates the entry, subsequent
        # puts each generate observations against existing peers.
        # The simplest structure: build up K peer entries with the
        # same value, then the K+1th put triggers K observations.
        same_value = ["candidate_a", "candidate_b", "candidate_c"]

        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache,
                self.projection,
                "q-h",
                "r-v1",
                f"cs-{i}",
                same_value,
            )
            # Status should remain NOT_PROMOTED until we hit K
            # *observations* — but each put generates as many
            # observations as there are existing peers, so promotion
            # may happen partway through.

        # After K puts of the same value, projection should be promoted.
        # (Whether mid-loop or at the end depends on the count;
        # for K=10 puts of same value, we generate
        # 0+1+2+...+9 = 45 observations total, so promotion
        # happens early.)
        self.assertEqual(
            self.projection.status("q-h", "r-v1"),
            ProjectionStatus.PROMOTED,
        )

    def test_divergence_blocks_promotion(self) -> None:
        """A divergence between observations resets the match count.

        With PROMOTION_THRESHOLD=10, 5 matching puts produce
        0+1+2+3+4 = 10 matched observations — right at the threshold.
        We need to interleave the divergence early enough that
        promotion hasn't fired before it.
        """
        proj = CacheProjection(
            cache=RankerOutputCache(max_entries=1024),
            equivalence_fn=deep_equality,
        )
        cache2 = proj.cache
        same_value = ["a", "b", "c"]
        diff_value = ["a", "x", "y"]

        # 3 matching puts (0+1+2 = 3 obs, well below threshold)
        for i in range(3):
            _put_with_signature(
                cache2, proj, "q2", "r2", f"cs-{i}", same_value,
            )

        # The divergent put compared against 3 existing peers —
        # 3 mismatches recorded. Plus 3 prior matches.
        # Since recent_diverges > 0, no promotion.
        _put_with_signature(
            cache2, proj, "q2", "r2", "cs-bad", diff_value,
        )

        self.assertEqual(
            proj.status("q2", "r2"),
            ProjectionStatus.NOT_PROMOTED,
        )

        # Even MANY further matching puts shouldn't promote until
        # the divergent observations age out (or get re-observed
        # as matching). Run a few more matching puts.
        for i in range(3, 6):
            _put_with_signature(
                cache2, proj, "q2", "r2", f"cs-{i}", same_value,
            )

        # Still NOT_PROMOTED — divergence remains in the window
        # blocking promotion.
        self.assertEqual(
            proj.status("q2", "r2"),
            ProjectionStatus.NOT_PROMOTED,
        )

    def test_lookup_routes_to_projected_when_promoted(self) -> None:
        """Once promoted, lookup() returns the projected entry."""
        same_value = ["a", "b", "c"]
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, "q-h", "r-v1",
                f"cs-{i}", same_value,
            )
        self.assertEqual(
            self.projection.status("q-h", "r-v1"),
            ProjectionStatus.PROMOTED,
        )

        # lookup with any of the merged signatures returns the value
        key_via_sig0 = RankerOutputCacheKey("q-h", "r-v1", "cs-0")
        result = self.projection.lookup(key_via_sig0, now_ms=1_000_100)
        self.assertEqual(result, same_value)

    def test_lookup_falls_back_when_not_promoted(self) -> None:
        """When NOT_PROMOTED, lookup() goes through to the
        signature-specific key."""
        value = ["x", "y"]
        _put_with_signature(
            self.cache, self.projection, "q-h", "r-v1", "cs-only", value,
        )

        key = RankerOutputCacheKey("q-h", "r-v1", "cs-only")
        self.assertEqual(self.projection.lookup(key, now_ms=1_000_100), value)

    def test_promotion_emits_audit_event(self) -> None:
        """A CacheProjectionPromoted event lands in the log."""
        proj = CacheProjection(
            cache=RankerOutputCache(max_entries=1024),
            equivalence_fn=deep_equality,
            log_client=self.p["log"],
        )
        same_value = ["a", "b", "c"]
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                proj.cache, proj, "q-aud", "r-aud",
                f"cs-{i}", same_value,
            )

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        promoted_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "cache_projection_promoted"
        ]
        self.assertGreater(len(promoted_evts), 0)
        evt = promoted_evts[0]
        self.assertEqual(evt["query_hash"], "q-aud")
        self.assertEqual(evt["ranker_name"], "r-aud")
        self.assertGreater(evt["observation_count"], 0)


# =============================================================================
# Demotion
# =============================================================================


class TestDemotion(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.cache = RankerOutputCache(max_entries=1024)
        self.projection = CacheProjection(
            cache=self.cache,
            equivalence_fn=deep_equality,
            log_client=self.p["log"],
        )

    def _promote_qh_rn(self, qh: str, rn: str, value: list, *,
                        now_ms: int = 1_000_000) -> None:
        """Helper to drive a (qh, rn) into PROMOTED state."""
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, qh, rn,
                f"cs-{i}", value, now_ms=now_ms + i,
            )
        self.assertEqual(
            self.projection.status(qh, rn),
            ProjectionStatus.PROMOTED,
        )

    def test_divergent_put_within_window_demotes(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        # Drive into promoted state
        self._promote_qh_rn("q-d", "r-d", same_value, now_ms=1_000_000)

        # Now put a divergent value under one of the merged signatures
        # within the demotion window
        _put_with_signature(
            self.cache, self.projection, "q-d", "r-d",
            "cs-0", diff_value, now_ms=1_001_000,  # 1 sec later
        )

        self.assertEqual(
            self.projection.status("q-d", "r-d"),
            ProjectionStatus.NOT_PROMOTED,
        )

    def test_demotion_emits_audit_event(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        self._promote_qh_rn("q-evt", "r-evt", same_value, now_ms=2_000_000)
        _put_with_signature(
            self.cache, self.projection, "q-evt", "r-evt",
            "cs-0", diff_value, now_ms=2_001_000,
        )

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        demoted_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "cache_projection_demoted"
        ]
        self.assertEqual(len(demoted_evts), 1)
        evt = demoted_evts[0]
        self.assertEqual(evt["query_hash"], "q-evt")
        self.assertEqual(evt["ranker_name"], "r-evt")
        self.assertNotEqual(evt["divergence_detail"], "")

    def test_demotion_drops_projected_entry(self) -> None:
        """After demotion, the projected key is no longer in the
        cache; lookup falls back to signature-specific."""
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        self._promote_qh_rn("q-drop", "r-drop", same_value, now_ms=3_000_000)
        _put_with_signature(
            self.cache, self.projection, "q-drop", "r-drop",
            "cs-0", diff_value, now_ms=3_001_000,
        )

        projected_key = RankerOutputCacheKey(
            "q-drop", "r-drop", PROJECTED_CLUSTER_SIGNATURE,
        )
        self.assertNotIn(projected_key, self.cache)

    def test_lookup_after_demotion_returns_signature_specific(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        self._promote_qh_rn("q-look", "r-look", same_value, now_ms=4_000_000)
        _put_with_signature(
            self.cache, self.projection, "q-look", "r-look",
            "cs-0", diff_value, now_ms=4_001_000,
        )

        # cs-0 now stores diff_value; cs-1 still has same_value
        key0 = RankerOutputCacheKey("q-look", "r-look", "cs-0")
        key1 = RankerOutputCacheKey("q-look", "r-look", "cs-1")

        self.assertEqual(
            self.projection.lookup(key0, now_ms=4_001_500),
            diff_value,
        )
        self.assertEqual(
            self.projection.lookup(key1, now_ms=4_001_500),
            same_value,
        )


# =============================================================================
# Cooldown
# =============================================================================


class TestCooldown(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.cache = RankerOutputCache(max_entries=1024)
        # Reduce cooldown for testability
        self.projection = CacheProjection(
            cache=self.cache,
            equivalence_fn=deep_equality,
            log_client=self.p["log"],
            promotion_cooldown_ms=10_000,  # 10 seconds
        )

    def test_cooldown_blocks_re_promotion(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        # Promote
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, "q-cool", "r-cool",
                f"cs-{i}", same_value, now_ms=5_000_000 + i,
            )
        self.assertEqual(
            self.projection.status("q-cool", "r-cool"),
            ProjectionStatus.PROMOTED,
        )

        # Demote via divergence
        _put_with_signature(
            self.cache, self.projection, "q-cool", "r-cool",
            "cs-0", diff_value, now_ms=5_001_000,
        )
        self.assertEqual(
            self.projection.status("q-cool", "r-cool"),
            ProjectionStatus.NOT_PROMOTED,
        )

        # Try to re-promote within cooldown — observations should
        # be silently dropped
        for i in range(PROMOTION_THRESHOLD * 2):
            _put_with_signature(
                self.cache, self.projection, "q-cool", "r-cool",
                f"cs-recheck-{i}", same_value,
                now_ms=5_001_500 + i,  # well within cooldown
            )

        self.assertEqual(
            self.projection.status("q-cool", "r-cool"),
            ProjectionStatus.NOT_PROMOTED,
        )

    def test_cooldown_expires_allowing_re_promotion(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]

        # Promote
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, "q-exp", "r-exp",
                f"cs-{i}", same_value, now_ms=6_000_000 + i,
            )

        # Demote
        _put_with_signature(
            self.cache, self.projection, "q-exp", "r-exp",
            "cs-0", diff_value, now_ms=6_001_000,
        )
        self.assertEqual(
            self.projection.status("q-exp", "r-exp"),
            ProjectionStatus.NOT_PROMOTED,
        )

        # Heal cs-0 back to same_value (substrate moved on, the
        # divergent put no longer reflects current ranker output).
        # Without observing — this is just substrate state, not a
        # ranker observation.
        heal_key = RankerOutputCacheKey("q-exp", "r-exp", "cs-0")
        self.cache.put(
            heal_key, same_value, now_ms=6_012_000, ttl_sec=10**9,
        )

        # Wait past cooldown (10 seconds + slack), re-promote with
        # fresh signatures. cs-after-* will compare against the
        # healed cs-0 + cs-1..cs-9 (still same_value) + each other.
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, "q-exp", "r-exp",
                f"cs-after-{i}", same_value,
                now_ms=6_012_000 + i,
            )

        self.assertEqual(
            self.projection.status("q-exp", "r-exp"),
            ProjectionStatus.PROMOTED,
        )


# =============================================================================
# Stability cap
# =============================================================================


class TestStabilityCap(unittest.TestCase):
    """Stability cap tests drive promote ↔ demote cycles. Each cycle
    needs to overwrite the divergent entry back to same_value before
    the next promote — otherwise the residual divergence in the cache
    blocks the next promotion attempt (correct semantic).

    Tests use a tight cap (2 cycles → unstable) for speed.
    """

    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.cache = RankerOutputCache(max_entries=1024)
        self.projection = CacheProjection(
            cache=self.cache,
            equivalence_fn=deep_equality,
            log_client=self.p["log"],
            promotion_cooldown_ms=100,  # very short
            instability_cap=2,  # 2 cycles → unstable
        )

    def _drive_cycle(
        self,
        qh: str,
        rn: str,
        same_value: list,
        diff_value: list,
        cycle: int,
        base_t: int,
    ) -> None:
        """Drive one promote → demote cycle.

        Each cycle uses disjoint signatures (cs-{cycle}-N) AND
        overwrites prior cycle's divergent signature back to
        same_value, so subsequent peer comparisons see consistent
        values.
        """
        t = base_t + cycle * 10_000  # 10s gap per cycle (past cooldown)

        # Step 1: heal any divergent entries from prior cycles by
        # overwriting them with same_value
        if cycle > 0:
            for prev_cycle in range(cycle):
                heal_key = RankerOutputCacheKey(qh, rn, f"cs-{prev_cycle}-0")
                self.cache.put(
                    heal_key, same_value, now_ms=t, ttl_sec=10**9,
                )
                # Don't observe this — we don't want it to count
                # toward promotion observations (it's a healing op)

        # Step 2: K matching puts to drive promotion
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                self.cache, self.projection, qh, rn,
                f"cs-{cycle}-{i}", same_value, now_ms=t + i,
            )

        # Step 3: divergent put under one signature → demotion
        _put_with_signature(
            self.cache, self.projection, qh, rn,
            f"cs-{cycle}-0", diff_value, now_ms=t + 100,
        )

    def test_repeated_cycles_flag_unstable(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]
        base_t = 7_000_000

        # Drive cycles until UNSTABLE or max cycles
        for cycle in range(5):
            self._drive_cycle(
                "q-cap", "r-cap", same_value, diff_value, cycle, base_t,
            )
            status = self.projection.status("q-cap", "r-cap")
            if status == ProjectionStatus.UNSTABLE:
                break

        self.assertEqual(
            self.projection.status("q-cap", "r-cap"),
            ProjectionStatus.UNSTABLE,
            f"After {cycle+1} cycles, should be UNSTABLE",
        )

    def test_unstable_state_blocks_observations(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]
        base_t = 8_000_000

        # Drive into UNSTABLE
        for cycle in range(5):
            self._drive_cycle(
                "q-blk", "r-blk", same_value, diff_value, cycle, base_t,
            )
            if self.projection.status("q-blk", "r-blk") == ProjectionStatus.UNSTABLE:
                break

        self.assertEqual(
            self.projection.status("q-blk", "r-blk"),
            ProjectionStatus.UNSTABLE,
        )

        # Subsequent puts shouldn't change anything
        for i in range(PROMOTION_THRESHOLD * 2):
            _put_with_signature(
                self.cache, self.projection, "q-blk", "r-blk",
                f"cs-future-{i}", same_value,
                now_ms=9_000_000 + i,
            )
        self.assertEqual(
            self.projection.status("q-blk", "r-blk"),
            ProjectionStatus.UNSTABLE,
        )

    def test_unstable_emits_audit_event(self) -> None:
        same_value = ["a", "b"]
        diff_value = ["a", "z"]
        base_t = 10_000_000

        for cycle in range(5):
            self._drive_cycle(
                "q-unst", "r-unst", same_value, diff_value, cycle, base_t,
            )
            if self.projection.status("q-unst", "r-unst") == ProjectionStatus.UNSTABLE:
                break

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        unstable_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "cache_projection_unstable"
        ]
        self.assertEqual(len(unstable_evts), 1)
        evt = unstable_evts[0]
        self.assertEqual(evt["query_hash"], "q-unst")
        self.assertGreaterEqual(evt["cycle_count"], 2)


# =============================================================================
# Stats
# =============================================================================


class TestProjectionStats(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_stats_initial_state(self) -> None:
        proj = CacheProjection(cache=RankerOutputCache(max_entries=1024))
        stats = proj.stats()
        self.assertEqual(stats["tracked_pairs"], 0)
        self.assertEqual(stats["promoted"], 0)
        self.assertEqual(stats["unstable"], 0)

    def test_stats_after_observations(self) -> None:
        proj = CacheProjection(
            cache=RankerOutputCache(max_entries=1024),
            equivalence_fn=deep_equality,
            log_client=self.p["log"],
        )
        # Trigger one promotion
        for i in range(PROMOTION_THRESHOLD):
            _put_with_signature(
                proj.cache, proj, "q-s", "r-s",
                f"cs-{i}", ["v"], now_ms=11_000_000 + i,
            )
        stats = proj.stats()
        self.assertEqual(stats["tracked_pairs"], 1)
        self.assertEqual(stats["promoted"], 1)


if __name__ == "__main__":
    unittest.main()
