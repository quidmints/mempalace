"""Tests for DD sub-slice C — current_edges and current_interpretations.

Sub-slice C converts the remaining two foundational master-state
views to DD. Same operator chain as `current_nodes` (sub-slice B):
filter → flat_map → reduce → arrange_by_key, with snapshot mirror via
inspect_batch.

# Per-view notes

- **current_edges**: fold-events pattern (create + 0-or-more
  invalidate). Tests the fold logic, the bitemporal validity
  helpers (`is_active`, `is_valid_at`), and the legacy-compatible
  JSON shape.

- **current_interpretations**: simpler — single event kind, each
  event fully replaces the prior state. Tests pick-latest-by-offset
  semantics with compound key `node_id::field`.

Both follow the structural-then-behavioral test split from sub-slice
B. Behavioral tests skip until the Rust extension is built and
sub-slice H wires up the PyDataflowHandle.
"""

from __future__ import annotations

import importlib
import os
import re
import unittest


EDGES_PATH = (
    "/home/claude/work/mempalace_core/src/dataflow/views/current_edges.rs"
)
INTERPRETATIONS_PATH = (
    "/home/claude/work/mempalace_core/src/dataflow/views/current_interpretations.rs"
)
VIEWS_MOD_PATH = "/home/claude/work/mempalace_core/src/dataflow/views/mod.rs"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# =============================================================================
# current_edges structural tests
# =============================================================================


class TestCurrentEdgesViewStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(EDGES_PATH):
            raise unittest.SkipTest(f"missing {EDGES_PATH}")
        cls.src = _read(EDGES_PATH)

    def test_implements_view_spec(self) -> None:
        self.assertIn("impl ViewSpec for CurrentEdgesView", self.src)

    def test_view_name(self) -> None:
        self.assertRegex(
            self.src,
            r'fn name\(&self\)\s*->\s*&\'static str\s*\{\s*"current_edges"',
        )

    def test_subscribed_kinds(self) -> None:
        # Multi-line check: the subscribed_kinds method must include
        # both edge_created and edge_invalidated as static-string
        # entries.
        m = re.search(
            r'fn subscribed_kinds.*?\}',
            self.src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "subscribed_kinds method not found")
        body = m.group(0)
        self.assertIn('"edge_created"', body)
        self.assertIn('"edge_invalidated"', body)

    def test_dd_operator_chain(self) -> None:
        self.assertIn("input.flat_map", self.src)
        self.assertIn(".reduce(", self.src)
        self.assertIn("arrange_by_key", self.src)
        self.assertIn("inspect_batch", self.src)

    def test_edge_state_dd_compatible(self) -> None:
        m = re.search(
            r"#\[derive\(([^)]+)\)\]\s*pub struct EdgeState",
            self.src,
        )
        self.assertIsNotNone(m, "EdgeState struct not found")
        derives = m.group(1)
        for required in ["Clone", "Eq", "Hash", "Ord"]:
            self.assertIn(required, derives,
                          f"EdgeState missing derive: {required}")

    def test_floats_stored_as_bits(self) -> None:
        self.assertIn("weight_bits: u64", self.src)
        self.assertIn("confidence_bits: u64", self.src)

    def test_bitemporal_helpers_preserved(self) -> None:
        """`is_active()` and `is_valid_at()` are part of the legacy
        view's API; the DD version must keep them."""
        self.assertIn("pub fn is_active(&self) -> bool", self.src)
        self.assertIn("pub fn is_valid_at(&self, world_time_ms: u64) -> bool", self.src)

    def test_inline_tests_present(self) -> None:
        for t in [
            "fn fold_create_then_invalidate",
            "fn parse_event_creates_with_timestamp_default",
            "fn parse_event_uses_explicit_timestamp_ms",
            "fn is_valid_at_respects_window",
            "fn is_valid_at_returns_false_when_invalidated",
            "fn snapshot_query_returns_legacy_compatible_json",
        ]:
            self.assertIn(t, self.src)

    def test_query_bytes_returns_legacy_shape(self) -> None:
        for field in [
            '"edge_id"', '"edge_kind"', '"source_node_id"', '"target_node_id"',
            '"valid_from"', '"valid_to"', '"recorded_at"', '"invalidated_at"',
            '"weight"', '"confidence"', '"derivation"', '"properties"',
            '"created_at_offset"', '"is_active"',
        ]:
            self.assertIn(field, self.src)

    def test_todo_markers_localized(self) -> None:
        todos = re.findall(r"TODO\(rust-build\)", self.src)
        self.assertGreaterEqual(len(todos), 2)
        self.assertLessEqual(len(todos), 15)


# =============================================================================
# current_interpretations structural tests
# =============================================================================


class TestCurrentInterpretationsViewStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(INTERPRETATIONS_PATH):
            raise unittest.SkipTest(f"missing {INTERPRETATIONS_PATH}")
        cls.src = _read(INTERPRETATIONS_PATH)

    def test_implements_view_spec(self) -> None:
        self.assertIn("impl ViewSpec for CurrentInterpretationsView", self.src)

    def test_view_name(self) -> None:
        self.assertRegex(
            self.src,
            r'fn name\(&self\)\s*->\s*&\'static str\s*\{\s*"current_interpretations"',
        )

    def test_subscribed_kinds(self) -> None:
        self.assertIn('"interpretation_assigned"', self.src)

    def test_dd_operator_chain(self) -> None:
        self.assertIn("input.flat_map", self.src)
        self.assertIn(".reduce(", self.src)
        self.assertIn("arrange_by_key", self.src)
        self.assertIn("inspect_batch", self.src)

    def test_compound_key_uses_double_colon(self) -> None:
        """Legacy keying is `node_id::field_name`; DD version must
        match so query bytes are interchangeable."""
        self.assertIn('format!("{}::{}", p.node_id, p.field_name)', self.src)

    def test_state_dd_compatible(self) -> None:
        m = re.search(
            r"#\[derive\(([^)]+)\)\]\s*pub struct InterpretationState",
            self.src,
        )
        self.assertIsNotNone(m, "InterpretationState struct not found")
        derives = m.group(1)
        for required in ["Clone", "Eq", "Hash", "Ord"]:
            self.assertIn(required, derives)

    def test_value_stored_as_json_string(self) -> None:
        self.assertIn("value_json: String", self.src)

    def test_confidence_stored_as_bits(self) -> None:
        self.assertIn("confidence_bits: u64", self.src)

    def test_inline_tests_present(self) -> None:
        for t in [
            "fn parse_keys_compose_node_id_and_field",
            "fn parse_event_skips_unrelated_kinds",
            "fn pick_latest_chooses_highest_offset",
            "fn pick_latest_returns_none_for_empty",
            "fn snapshot_query_legacy_shape",
        ]:
            self.assertIn(t, self.src)

    def test_query_bytes_returns_legacy_shape(self) -> None:
        for field in [
            '"node_id"', '"field_name"', '"value"', '"miner_pass_version"',
            '"confidence"', '"assigned_at_offset"', '"supersedes_event_id"',
        ]:
            self.assertIn(field, self.src)


class TestSubsliceCRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(VIEWS_MOD_PATH):
            raise unittest.SkipTest("views/mod.rs missing")
        cls.src = _read(VIEWS_MOD_PATH)

    def test_current_edges_registered(self) -> None:
        self.assertIn("pub mod current_edges", self.src)

    def test_current_interpretations_registered(self) -> None:
        self.assertIn("pub mod current_interpretations", self.src)


# =============================================================================
# Behavioral tests — skipped until extension is built
# =============================================================================


def _has_rust_dataflow() -> bool:
    try:
        mp = importlib.import_module("mempalace_core")
    except ImportError:
        return False
    return hasattr(mp, "PyDataflowHandle")


@unittest.skipUnless(
    _has_rust_dataflow(),
    "Rust extension with DD handle not built (sub-slice H wires this up)",
)
class TestCurrentEdgesBehavioral(unittest.TestCase):
    def test_create_then_invalidate(self) -> None:
        import mempalace_core as mp  # type: ignore
        import json
        handle = mp.PyDataflowHandle.start(["current_edges"])
        try:
            handle.feed(
                offset=1,
                kind="edge_created",
                payload=json.dumps({
                    "edge_id": "e1",
                    "edge_kind": "contains",
                    "source_node_id": "p1",
                    "target_node_id": "ev1",
                    "_timestamp_ms": 1000,
                }).encode(),
            )
            handle.feed(
                offset=2,
                kind="edge_invalidated",
                payload=json.dumps({
                    "edge_id": "e1",
                    "_timestamp_ms": 2000,
                }).encode(),
            )
            handle.advance_to(2)
            v = json.loads(handle.query("current_edges", b"e1"))
            self.assertFalse(v["is_active"])
            self.assertEqual(v["invalidated_at"], 2000)
        finally:
            handle.shutdown()


@unittest.skipUnless(
    _has_rust_dataflow(),
    "Rust extension with DD handle not built (sub-slice H wires this up)",
)
class TestCurrentInterpretationsBehavioral(unittest.TestCase):
    def test_supersession_picks_latest(self) -> None:
        import mempalace_core as mp  # type: ignore
        import json
        handle = mp.PyDataflowHandle.start(["current_interpretations"])
        try:
            for offset, val, conf in [(1, "v1", 0.5), (3, "v3", 0.9), (2, "v2", 0.7)]:
                handle.feed(
                    offset=offset,
                    kind="interpretation_assigned",
                    payload=json.dumps({
                        "node_id": "n1",
                        "field_name": "memory_type",
                        "new_value": val,
                        "confidence": conf,
                    }).encode(),
                )
            handle.advance_to(3)
            v = json.loads(handle.query("current_interpretations", b"n1::memory_type"))
            self.assertEqual(v["value"], "v3")
            self.assertEqual(v["assigned_at_offset"], 3)
        finally:
            handle.shutdown()


if __name__ == "__main__":
    unittest.main()
