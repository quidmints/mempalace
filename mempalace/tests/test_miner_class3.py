"""Tests for Class 3 schema-induction miner (Part 10.5)."""

from __future__ import annotations

import hashlib
import unittest

from mempalace.miner import Class3Pass, PassContext
from mempalace.tests.conftest import reset_module_state


def _shape_fp(predicate: str, sk: str, ok: str) -> str:
    return hashlib.blake2b(
        f"{predicate}|{sk}|{ok}".encode(), digest_size=8,
    ).hexdigest()


class TestClass3(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_emits_schemas_meeting_min_instances(self) -> None:
        # 3 instances of "loves/person/thing" → schema; below threshold "knows" filtered
        assertions = [
            {"subject_id": "S1", "predicate": "loves", "object_id": "x",
             "subject_kind": "person", "object_kind": "thing"},
            {"subject_id": "S2", "predicate": "loves", "object_id": "y",
             "subject_kind": "person", "object_kind": "thing"},
            {"subject_id": "S3", "predicate": "loves", "object_id": "z",
             "subject_kind": "person", "object_kind": "thing"},
            {"subject_id": "S1", "predicate": "knows", "object_id": "S2",
             "subject_kind": "person", "object_kind": "person"},
        ]
        pass_ = Class3Pass(min_instances=3)
        ctx = PassContext(parameters={"assertions": assertions})
        result = pass_.run(ctx)
        kinds = {p.proposed_value["shape"][0] for p in result.proposals}
        self.assertIn("loves", kinds)
        self.assertNotIn("knows", kinds)

    def test_classifies_status_against_previous_snapshot(self) -> None:
        # Previous snapshot had a "hates"/person/thing shape with 4 instances;
        # current run has zero → broken
        previous = {_shape_fp("hates", "person", "thing"): 4}
        assertions = [
            {"subject_id": f"S{i}", "predicate": "loves", "object_id": "x",
             "subject_kind": "person", "object_kind": "thing"}
            for i in range(3)
        ]
        pass_ = Class3Pass(min_instances=3)
        ctx = PassContext(parameters={
            "assertions": assertions,
            "previous_schema_snapshot": previous,
        })
        result = pass_.run(ctx)
        statuses = [p.proposed_value["status"] for p in result.proposals]
        # New schema "loves" + broken schema "hates"
        self.assertIn("new", statuses)
        self.assertIn("broken", statuses)

    def test_min_instances_below_threshold_returns_no_proposals(self) -> None:
        assertions = [
            {"subject_id": "S1", "predicate": "knows", "object_id": "S2",
             "subject_kind": "person", "object_kind": "person"},
        ]
        pass_ = Class3Pass(min_instances=3)
        ctx = PassContext(parameters={"assertions": assertions})
        result = pass_.run(ctx)
        self.assertEqual(len(result.proposals), 0)


if __name__ == "__main__":
    unittest.main()
