"""Tests for the canonicalizer (R3 §4)."""

from __future__ import annotations

import unittest

from mempalace.canonicalizer import (
    CanonDomain,
    Canonicalizer,
    DEFAULT_PROMOTION_MIN_MEMBERS,
    DEFAULT_THRESHOLDS,
)
from mempalace.tests.conftest import reset_module_state


def stub_embed(s: str) -> list[float]:
    """Bigram-bucket embedder; deterministic + similar surfaces ~ similar vectors."""
    s = s.lower().strip()
    bigrams = [s[i:i + 2] for i in range(len(s) - 1)] if len(s) >= 2 else [s]
    v = [0.0] * 32
    for bg in bigrams:
        v[hash(bg) % 32] += 1.0
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n > 0 else v


class TestCanonicalizerSeed(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.can = Canonicalizer(embedder=stub_embed)
        self.can.seed(CanonDomain.PREDICATES, [
            ("pred_loves", "loves", stub_embed("loves")),
            ("pred_owns", "owns", stub_embed("owns")),
        ])

    def test_seed_resolves_exact_match(self) -> None:
        res = self.can.resolve(CanonDomain.PREDICATES, "loves")
        self.assertTrue(res.matched_existing)
        self.assertEqual(res.canonical_id, "pred_loves")
        self.assertEqual(res.similarity, 1.0)

    def test_seed_resolves_case_insensitive(self) -> None:
        res = self.can.resolve(CanonDomain.PREDICATES, "OWNS")
        self.assertEqual(res.canonical_id, "pred_owns")


class TestCanonicalizerCandidatePool(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.can = Canonicalizer(embedder=stub_embed)

    def test_novel_surface_enters_candidate_pool(self) -> None:
        res = self.can.resolve(CanonDomain.THEME_NAMES, "deep work", pass_id="p1")
        self.assertFalse(res.matched_existing)
        self.assertIsNotNone(res.queued_in_cluster_id)
        clusters = self.can.candidate_clusters(CanonDomain.THEME_NAMES)
        self.assertEqual(len(clusters), 1)


class TestCanonicalizerPromotion(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_cluster_promotes_at_threshold(self) -> None:
        # Use a deterministic embedder where surfaces sharing a common
        # prefix produce highly-similar vectors so the cluster forms
        # reliably.
        def deterministic_embed(s: str) -> list[float]:
            v = [0.0] * 16
            for i, ch in enumerate(s.lower()[:16]):
                v[i] = (ord(ch) % 17) / 17.0
            n = sum(x * x for x in v) ** 0.5
            return [x / n for x in v] if n > 0 else v

        can = Canonicalizer(
            embedder=deterministic_embed,
            promotion_min_members=DEFAULT_PROMOTION_MIN_MEMBERS,
            promotion_min_passes=2,
        )
        # Surfaces that all share the prefix "weekend cooking" — their
        # first 16 chars are identical, so the bucket-vector embedder
        # produces identical embeddings for that prefix region.
        for surface, pid in [
            ("weekend cooking", "p1"),
            ("weekend cooking time", "p1"),
            ("weekend cooking session", "p2"),
            ("weekend cooking pasta", "p2"),
        ]:
            can.resolve(CanonDomain.THEME_NAMES, surface, pass_id=pid)
        promoted = can.check_promotions(CanonDomain.THEME_NAMES)
        self.assertGreaterEqual(len(promoted), 1)
        # Resolving the promoted surface should now match
        for p in promoted:
            res = can.resolve(CanonDomain.THEME_NAMES, p.surface)
            self.assertTrue(res.matched_existing)
            self.assertEqual(res.canonical_id, p.canonical_id)


class TestCanonicalizerRevertReject(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_reject_cluster_removes_it(self) -> None:
        can = Canonicalizer(embedder=stub_embed)
        can.resolve(CanonDomain.THEME_NAMES, "obscure", pass_id="p1")
        clusters = can.candidate_clusters(CanonDomain.THEME_NAMES)
        cid = clusters[0].cluster_id
        ok = can.reject_cluster(CanonDomain.THEME_NAMES, cid, reason="user_no")
        self.assertTrue(ok)
        self.assertEqual(can.candidate_clusters(CanonDomain.THEME_NAMES), [])

    def test_revert_canonical_removes_from_resolves(self) -> None:
        can = Canonicalizer(embedder=stub_embed)
        can.seed(CanonDomain.PREDICATES, [("pred_x", "x", stub_embed("x"))])
        ok = can.revert("pred_x", reason="bad_choice")
        self.assertTrue(ok)
        # canonicals() filters out reverted entries
        remaining = can.canonicals(CanonDomain.PREDICATES)
        self.assertEqual(len(remaining), 0)


class TestCanonicalizerThresholds(unittest.TestCase):
    def test_thresholds_match_spec(self) -> None:
        # R3 §4.2 thresholds.
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.PREDICATES], 0.85)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.MEMORY_TYPES], 0.90)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.SCHEMA_NAMES], 0.78)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.ENTITY_ALIASES], 0.92)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.PERIOD_NAMES], 0.80)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.THEME_NAMES], 0.85)
        self.assertEqual(DEFAULT_THRESHOLDS[CanonDomain.GOAL_MARKERS], 0.75)


if __name__ == "__main__":
    unittest.main()
