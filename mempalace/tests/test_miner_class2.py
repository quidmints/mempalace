"""Tests for Class 2 cross-drawer aggregation miner (Part 10.5)."""

from __future__ import annotations

import unittest

from mempalace.miner import Class2Pass, PassContext
from mempalace.tests.conftest import reset_module_state


class TestClass2(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_emits_period_state_per_run(self) -> None:
        drawers = [
            {"drawer_id": f"d{i}",
             "verbatim": f"Quantum entanglement spooky distance {i}",
             "themes": ["physics"]}
            for i in range(3)
        ]
        pass_ = Class2Pass()
        ctx = PassContext(parameters={"drawers": drawers})
        result = pass_.run(ctx)
        period_states = [p for p in result.proposals if p.proposal_kind == "period_state"]
        self.assertEqual(len(period_states), 1)
        self.assertEqual(
            period_states[0].proposed_value["drawer_count_in_window"], 3
        )

    def test_detects_event_boundary_at_topic_shift(self) -> None:
        drawers = [
            {"drawer_id": "d1", "verbatim": "Sarah loves coffee daily morning",
             "themes": ["caffeine"]},
            # boundary — totally different vocabulary
            {"drawer_id": "d2", "verbatim": "Quantum measurement collapses superposition",
             "themes": ["physics"]},
        ]
        pass_ = Class2Pass(boundary_jaccard=0.15)
        ctx = PassContext(parameters={"drawers": drawers})
        result = pass_.run(ctx)
        boundaries = [p for p in result.proposals if p.proposal_kind == "event_boundary"]
        self.assertEqual(len(boundaries), 1)

    def test_detects_contradiction(self) -> None:
        drawers = [
            {"drawer_id": "d1", "verbatim": "Sarah loves coffee", "themes": []},
        ]
        existing = [{"subject_id": "Sarah", "predicate": "loves", "object_id": "tea"}]
        pass_ = Class2Pass()
        ctx = PassContext(parameters={
            "drawers": drawers,
            "existing_assertions": existing,
        })
        result = pass_.run(ctx)
        contras = [p for p in result.proposals if p.proposal_kind == "contradiction"]
        self.assertGreaterEqual(len(contras), 1)

    def test_emits_velocity_update_per_theme(self) -> None:
        drawers = [
            {"drawer_id": f"d{i}",
             "verbatim": f"text {i}",
             "themes": ["theme_a"] if i < 3 else ["theme_b"]}
            for i in range(5)
        ]
        pass_ = Class2Pass()
        ctx = PassContext(parameters={"drawers": drawers})
        result = pass_.run(ctx)
        vels = [p for p in result.proposals if p.proposal_kind == "velocity_update"]
        # one update per distinct theme
        self.assertEqual(len(vels), 2)


if __name__ == "__main__":
    unittest.main()
