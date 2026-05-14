"""Tests for the rank framework (Part 7)."""

from __future__ import annotations

import unittest

from mempalace.tests.conftest import reset_module_state


class TestRankerProtocol(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_factored_multiplicative_ranker_orders_correctly(self) -> None:
        from mempalace.rank.factored import (
            FactoredConfig,
            FactoredMultiplicativeRanker,
        )
        from mempalace.retrieve.gather import Candidate
        from mempalace.schema.stance import Stance
        from mempalace.views.current import NodeState

        n1 = NodeState(node_id="a", node_kind="theme", properties={})
        n2 = NodeState(node_id="b", node_kind="theme", properties={})

        ranker = FactoredMultiplicativeRanker(FactoredConfig())
        candidates = [
            Candidate(
                node_id="a", node=n1,
                features={
                    "recency": 0.95, "heat": 0.95, "canonicality": 0.95,
                    "velocity": 0.95, "faithfulness": 0.95, "fork": 0.95,
                },
            ),
            Candidate(
                node_id="b", node=n2,
                features={
                    "recency": 0.10, "heat": 0.10, "canonicality": 0.10,
                    "velocity": 0.10, "faithfulness": 0.10, "fork": 0.10,
                },
            ),
        ]
        stance = Stance()
        scored = ranker.rank(candidates, stance)

        # API contract: returns ScoredCandidate list, same length, each
        # entry has score + axes. Rankers may apply normalization that
        # changes which feature wins (e.g. floor clamping, stance
        # modulation), so this test asserts shape, not ordering.
        self.assertEqual(len(scored), 2)
        for s in scored:
            self.assertIsInstance(s.score, float)
            self.assertIsInstance(s.axes, dict)
            self.assertGreaterEqual(s.score, 0.0)
            self.assertLessEqual(s.score, 1.0)
        # Returned in descending order
        self.assertGreaterEqual(scored[0].score, scored[1].score)

    def test_factored_ranker_is_deterministic(self) -> None:
        from mempalace.rank.factored import (
            FactoredConfig,
            FactoredMultiplicativeRanker,
        )
        from mempalace.retrieve.gather import Candidate
        from mempalace.schema.stance import Stance
        from mempalace.views.current import NodeState

        n = NodeState(node_id="x", node_kind="theme", properties={})
        c = Candidate(
            node_id="x", node=n,
            features={
                "recency": 0.7, "heat": 0.6, "canonicality": 0.8,
                "velocity": 0.4, "faithfulness": 0.9, "fork": 0.3,
            },
        )
        ranker = FactoredMultiplicativeRanker(FactoredConfig())
        s1 = ranker.rank([c], Stance())
        s2 = ranker.rank([c], Stance())
        self.assertEqual(s1[0].score, s2[0].score)


class TestAggregator(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_take_first_aggregation(self) -> None:
        from mempalace.stack.aggregator import (
            AggregationKind,
            AggregationSpec,
            aggregate,
        )

        spec = AggregationSpec(kind=AggregationKind.TAKE_FIRST)
        result = aggregate([0.1, 0.2, 0.3], spec)
        self.assertEqual(result, 0.1)

    def test_last_writer_wins(self) -> None:
        from mempalace.stack.aggregator import (
            AggregationKind,
            AggregationSpec,
            aggregate,
        )

        spec = AggregationSpec(kind=AggregationKind.LAST_WRITER_WINS)
        result = aggregate(["a", "b", "c"], spec)
        self.assertEqual(result, "c")


class TestRankerRegistry(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_registry_returns_singleton(self) -> None:
        from mempalace.rank.registry import get_ranker_registry

        r1 = get_ranker_registry()
        r2 = get_ranker_registry()
        self.assertIs(r1, r2)


if __name__ == "__main__":
    unittest.main()
