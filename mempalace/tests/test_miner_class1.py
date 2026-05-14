"""Tests for Class 1 streaming miner (Part 10.5)."""

from __future__ import annotations

import unittest

from mempalace.miner import Class1Pass, PassContext
from mempalace.tests.conftest import reset_module_state


class TestClass1(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.pass_ = Class1Pass()

    def test_emits_five_kinds_per_drawer(self) -> None:
        drawers = [
            {"drawer_id": "d1",
             "verbatim": "I was happy after my run today. I plan to do it again."},
        ]
        ctx = PassContext(parameters={"drawers": drawers})
        result = self.pass_.run(ctx)
        self.assertTrue(result.success)
        kinds = {p.proposal_kind for p in result.proposals}
        self.assertEqual(kinds, {
            "memory_type", "interactional", "self_other_world",
            "goal_marker", "affect_derivatives",
        })

    def test_detects_autobiographical_memory(self) -> None:
        drawers = [{
            "drawer_id": "d1",
            "verbatim": "When I was a kid, growing up I always wanted to be an astronaut.",
        }]
        ctx = PassContext(parameters={"drawers": drawers})
        result = self.pass_.run(ctx)
        memtypes = [p for p in result.proposals if p.proposal_kind == "memory_type"]
        self.assertEqual(memtypes[0].proposed_value["memory_type"], "autobiographical")

    def test_detects_negative_affect(self) -> None:
        drawers = [{
            "drawer_id": "d1",
            "verbatim": "I'm frustrated, anxious, and angry today. The world is awful.",
        }]
        ctx = PassContext(parameters={"drawers": drawers})
        result = self.pass_.run(ctx)
        affect = next(p for p in result.proposals if p.proposal_kind == "affect_derivatives")
        self.assertLess(affect.proposed_value["valence"], 0)

    def test_max_drawers_limit(self) -> None:
        drawers = [{"drawer_id": f"d{i}", "verbatim": f"text {i}"} for i in range(20)]
        pass_ = Class1Pass(max_drawers_per_run=5)
        ctx = PassContext(parameters={"drawers": drawers})
        result = pass_.run(ctx)
        # 5 drawers × 5 proposal kinds = 25 proposals
        self.assertEqual(result.inputs_consumed, 5)
        self.assertEqual(len(result.proposals), 25)


if __name__ == "__main__":
    unittest.main()
