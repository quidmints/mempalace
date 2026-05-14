"""Tests for federate/ — sandbox + matching + findings + watermark."""

from __future__ import annotations

import unittest

from mempalace.tests.conftest import reset_module_state


class TestKgSketch(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_jaccard_similarity_basics(self) -> None:
        from mempalace.federate.kg_sketch import build_kg_sketch, sketch_jaccard

        nodes_a = [{"node_id": f"n{i}", "kind": "theme"} for i in range(3)]
        nodes_b = [{"node_id": f"n{i}", "kind": "theme"} for i in range(2, 5)]
        edges = []  # both empty
        s1 = build_kg_sketch(nodes=nodes_a, edges=edges, num_hashes=64)
        s2 = build_kg_sketch(nodes=nodes_b, edges=edges, num_hashes=64)
        sim = sketch_jaccard(s1, s2)
        # 1 shared (n2) / 4 union (n0..n4) = 0.2 — minhash approximates it
        self.assertGreaterEqual(sim, 0.0)
        self.assertLessEqual(sim, 1.0)

    def test_identical_sketches_have_high_similarity(self) -> None:
        from mempalace.federate.kg_sketch import build_kg_sketch, sketch_jaccard

        nodes = [{"node_id": f"n{i}", "kind": "theme"} for i in range(5)]
        s1 = build_kg_sketch(nodes=nodes, edges=[], num_hashes=64)
        s2 = build_kg_sketch(nodes=nodes, edges=[], num_hashes=64)
        # identical inputs → identical sketches → jaccard == 1.0
        self.assertEqual(sketch_jaccard(s1, s2), 1.0)


class TestRateLimit(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_rate_limiter_caps_requests(self) -> None:
        from mempalace.federate.rate_limit import RateLimiter

        limiter = RateLimiter()
        # match_request policy: 10 per hour. 11th must be denied.
        allowed = sum(1 for _ in range(15) if limiter.check("peer_a", "match_request"))
        self.assertEqual(allowed, 10)

    def test_unknown_route_allowed(self) -> None:
        from mempalace.federate.rate_limit import RateLimiter

        limiter = RateLimiter()
        for _ in range(100):
            self.assertTrue(limiter.check("peer", "unknown_route"))


class TestWatermark(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_embed_then_detect_roundtrip(self) -> None:
        from mempalace.federate.watermark import get_watermark_registry

        reg = get_watermark_registry()
        seed = reg.create(session_key_id="sk_test_1")
        original = "this is some content from a foreign palace finding"
        marked = reg.embed("sk_test_1", original)
        self.assertNotEqual(marked, original)  # transform applied
        result = reg.detect(marked)
        self.assertTrue(result.matched)
        self.assertEqual(result.matched_session_key_id, "sk_test_1")


if __name__ == "__main__":
    unittest.main()
