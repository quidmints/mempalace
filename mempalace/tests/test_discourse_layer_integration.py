"""
Tests for R3 §9.5 — discourse-pattern integration into the layer 2b
DerivationGraphSimilarity step.

Coverage:
  - Without discourse patterns in the context, layer 2b falls back to
    pure CCGraph signal (backwards-compat).
  - With discourse patterns, the score blends ccgraph_score *
    DEFAULT_CCGRAPH_WEIGHT + discourse_score * DEFAULT_DISCOURSE_WEIGHT.
  - Identical discourse patterns push the layer 2b score up.
  - Disjoint discourse patterns push it down.
"""

from __future__ import annotations

import unittest

from mempalace.federate.discourse import DiscoursePattern
from mempalace.federate.layers.derivation import (
    CCGraphSketch,
    DerivationGraphSimilarity,
)
from mempalace.stack.context import StackContext


def _stub_ccgraph_sketch() -> CCGraphSketch:
    """Minimal CCGraph sketch for the existing _ccgraph_jaccard helper."""
    return CCGraphSketch(
        nodes=("n1", "n2"),
        edges=(("n1", "n2"),),
        fork_components=(),
    )


class TestDerivationLayerWithDiscourse(unittest.TestCase):
    def setUp(self) -> None:
        self.step = DerivationGraphSimilarity()

    def _make_ctx(self, **overrides) -> StackContext:
        inputs = {
            "local_ccgraph_sketch": _stub_ccgraph_sketch(),
            "remote_ccgraph_sketch": _stub_ccgraph_sketch(),
            "layer2a_gate_passed": True,
            **overrides,
        }
        return StackContext(inputs=inputs)

    def test_no_discourse_falls_back_to_ccgraph(self) -> None:
        ctx = self._make_ctx()
        result = self.step._run_sync(ctx)
        self.assertTrue(result.success)
        breakdown = result.outputs["layer2b_breakdown"]
        # No "discourse" key when not provided
        self.assertNotIn("discourse", breakdown)

    def test_identical_discourse_appears_in_breakdown(self) -> None:
        pattern = DiscoursePattern(
            node_id="x",
            refinement_chain_count=2,
            refinement_chain_lengths=(2, 4),
            contradiction_count=3,
            supersedes_count=2,
            contradiction_resolution_ratio=2 / 3,
            support_vs_oppose_balance=0.5,
            total_discourse_edges=10,
        )
        ctx = self._make_ctx(
            local_discourse_pattern=pattern,
            remote_discourse_pattern=pattern,
        )
        result = self.step._run_sync(ctx)
        self.assertTrue(result.success)
        breakdown = result.outputs["layer2b_breakdown"]
        self.assertIn("discourse", breakdown)
        # Identical patterns → discourse aggregate = 1.0
        self.assertEqual(breakdown["discourse"]["aggregate"], 1.0)

    def test_disjoint_discourse_lowers_score(self) -> None:
        local = DiscoursePattern(
            node_id="a",
            support_vs_oppose_balance=1.0,  # all support
            inhibits_count=0,
            supports_count=10,
            total_discourse_edges=10,
        )
        remote = DiscoursePattern(
            node_id="b",
            support_vs_oppose_balance=-1.0,  # all opposition
            inhibits_count=10,
            supports_count=0,
            total_discourse_edges=10,
        )

        # Run with discourse patterns
        ctx_with = self._make_ctx(
            local_discourse_pattern=local,
            remote_discourse_pattern=remote,
        )
        result_with = self.step._run_sync(ctx_with)

        # Run without
        ctx_without = self._make_ctx()
        result_without = self.step._run_sync(ctx_without)

        # Disjoint discourse should pull the score below the
        # discourse-less score (since identical CCGraph stub is the same
        # in both runs, the only diff is the discourse blend).
        self.assertLess(
            result_with.outputs["layer2b_score"],
            result_without.outputs["layer2b_score"],
        )


class TestGateBlocking(unittest.TestCase):
    """Layer 2a gate blocks layer 2b regardless of discourse signal."""

    def test_blocked_when_layer2a_gate_failed(self) -> None:
        step = DerivationGraphSimilarity()
        ctx = StackContext(inputs={
            "local_ccgraph_sketch": _stub_ccgraph_sketch(),
            "remote_ccgraph_sketch": _stub_ccgraph_sketch(),
            "layer2a_gate_passed": False,
            "local_discourse_pattern": DiscoursePattern(node_id="x"),
            "remote_discourse_pattern": DiscoursePattern(node_id="x"),
        })
        result = step._run_sync(ctx)
        self.assertEqual(result.outputs["layer2b_score"], 0.0)
        self.assertFalse(result.outputs["layer2b_gate_passed"])


if __name__ == "__main__":
    unittest.main()
