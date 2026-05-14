"""Tests for DD sub-slice B — current_nodes as a DD view.

Sub-slice B converts `current_nodes` from a `parking_lot::RwLock<HashMap>`
placeholder into a real DD-backed view. It's the first view to use
the `ViewSpec` trait and the operator chain
`flat_map → reduce → arrange_by_key → trace`.

# Test layers

1. **Structural** — read the source file, verify the operator chain,
   the helper functions, the trait impl, and the snapshot mirror are
   all present. Runs unconditionally.

2. **Behavioral** — actually drive a built `mempalace_core` extension,
   feed events, query the view. Skipped when extension isn't built.

# Why two layers

Without a Rust toolchain in this environment, behavioral tests can't
run. The structural tests catch shape mistakes (missing operator,
wrong subscribed_kinds, broken trait impl). When the user builds the
extension, the behavioral tests confirm the DD operators actually
produce the right output.
"""

from __future__ import annotations

import importlib
import os
import re
import unittest


CURRENT_NODES_PATH = (
    "/home/claude/work/mempalace_core/src/dataflow/views/current_nodes.rs"
)
DATAFLOW_MOD_PATH = "/home/claude/work/mempalace_core/src/dataflow/mod.rs"
VIEWS_MOD_PATH = "/home/claude/work/mempalace_core/src/dataflow/views/mod.rs"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# =============================================================================
# Structural tests
# =============================================================================


class TestCurrentNodesViewStructure(unittest.TestCase):
    """The DD-backed current_nodes view has the expected shape."""

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(CURRENT_NODES_PATH):
            raise unittest.SkipTest(
                f"current_nodes DD view missing at {CURRENT_NODES_PATH}"
            )
        cls.src = _read(CURRENT_NODES_PATH)

    def test_implements_view_spec_trait(self) -> None:
        self.assertIn("impl ViewSpec for CurrentNodesView", self.src)

    def test_subscribed_kinds_are_node_events(self) -> None:
        # The subscribed_kinds method must include both node_created
        # and node_property_set
        self.assertRegex(
            self.src,
            r'fn subscribed_kinds\(&self\)\s*->\s*&\[&\'static str\]\s*\{[^}]*"node_created"[^}]*"node_property_set"',
        )

    def test_view_name_is_current_nodes(self) -> None:
        self.assertRegex(
            self.src,
            r'fn name\(&self\)\s*->\s*&\'static str\s*\{\s*"current_nodes"',
        )

    def test_dd_operator_chain_present(self) -> None:
        """The build() method wires flat_map → reduce → arrange_by_key →
        inspect_batch."""
        # flat_map for parsing + keying
        self.assertIn("input.flat_map", self.src)
        # reduce for folding events into NodeState
        self.assertIn(".reduce(", self.src)
        # arrange_by_key for the trace
        self.assertIn("arrange_by_key", self.src)
        # inspect_batch for snapshot mirroring
        self.assertIn("inspect_batch", self.src)

    def test_dd_imports_present(self) -> None:
        """Confirms the source actually pulls in DD operator traits."""
        self.assertIn(
            "use differential_dataflow::operators::Reduce", self.src
        )
        self.assertIn(
            "use differential_dataflow::operators::arrange::ArrangeByKey",
            self.src,
        )
        self.assertIn("use differential_dataflow::Collection", self.src)

    def test_node_state_dd_compatible(self) -> None:
        """NodeState must be Clone+Eq+Hash+Ord for DD's reduce."""
        m = re.search(
            r"#\[derive\(([^)]+)\)\]\s*pub struct NodeState",
            self.src,
        )
        self.assertIsNotNone(m, "NodeState struct not found")
        derives = m.group(1)
        for required in ["Clone", "PartialEq", "Eq", "Hash", "PartialOrd", "Ord"]:
            self.assertIn(
                required, derives,
                f"NodeState missing required derive for DD: {required}",
            )

    def test_importance_stored_as_bits(self) -> None:
        """f64 isn't Eq, so importance is stored as u64 bits."""
        self.assertIn("importance_bits: u64", self.src)
        self.assertIn("f64::from_bits", self.src)
        self.assertIn(".to_bits()", self.src)

    def test_properties_stored_as_json_string(self) -> None:
        """serde_json::Value isn't Hash/Ord either; properties is a string."""
        self.assertIn("properties_json: String", self.src)

    def test_helpers_parse_event_and_fold_events(self) -> None:
        self.assertIn("fn parse_event(evt: &EventTuple)", self.src)
        self.assertIn("fn fold_events(events: Vec<(LogOffset, ParsedEvent)>)", self.src)

    def test_snapshot_trace_query_implemented(self) -> None:
        self.assertIn("impl TraceQuery for SnapshotTraceQuery", self.src)
        # The three required methods
        self.assertIn("fn query_bytes(&self, key_bytes: &[u8])", self.src)
        self.assertIn("fn snapshot_bytes(&self)", self.src)
        self.assertIn("fn frontier_offset(&self)", self.src)

    def test_query_bytes_returns_legacy_compat_shape(self) -> None:
        """The JSON returned by query_bytes must include the same
        fields as the legacy view's NodeState so consumers can switch
        between the two transparently."""
        for field in [
            '"node_id"',
            '"node_kind"',
            '"properties"',
            '"canonical"',
            '"canon_path"',
            '"importance"',
            '"created_at_offset"',
            '"last_modified_at_offset"',
        ]:
            self.assertIn(field, self.src)

    def test_inline_rust_tests_present(self) -> None:
        for test_name in [
            "fn parse_event_picks_node_id",
            "fn parse_event_skips_unrelated_kinds",
            "fn fold_events_built_from_create_only",
            "fn fold_events_applies_property_set_after_create",
            "fn fold_events_orphan_property_set_is_noop",
            "fn fold_events_applies_canonical_field",
            "fn snapshot_query_returns_json_with_expected_fields",
            "fn snapshot_query_returns_none_for_unknown_key",
            "fn snapshot_bytes_returns_all_entries",
        ]:
            self.assertIn(test_name, self.src)

    def test_todo_markers_localized(self) -> None:
        todos = re.findall(r"TODO\(rust-build\)", self.src)
        # The DD operator API has several places that need build-time
        # confirmation (reduce signature, arrange_by_key return type,
        # TraceAgent type name, inspect_batch signature). 4-25 markers.
        self.assertGreaterEqual(len(todos), 4)
        self.assertLessEqual(len(todos), 25)

    def test_legacy_compat_acknowledged(self) -> None:
        """The doc-comment must mention coexistence with the legacy
        view so future readers know this isn't a drop-in replacement
        until sub-slice F."""
        self.assertIn("legacy", self.src.lower())
        self.assertIn("Coexistence with the legacy view", self.src)


class TestModulesRegistered(unittest.TestCase):
    """The new view files are wired into the module tree."""

    def test_views_module_registered_in_dataflow(self) -> None:
        if not os.path.exists(DATAFLOW_MOD_PATH):
            self.skipTest("dataflow/mod.rs missing")
        src = _read(DATAFLOW_MOD_PATH)
        self.assertIn("pub mod views", src)

    def test_current_nodes_registered_in_views_mod(self) -> None:
        if not os.path.exists(VIEWS_MOD_PATH):
            self.skipTest("dataflow/views/mod.rs missing")
        src = _read(VIEWS_MOD_PATH)
        self.assertIn("pub mod current_nodes", src)


# =============================================================================
# Behavioral tests — skipped when extension not built
# =============================================================================


def _has_rust_dataflow() -> bool:
    """Probe for the built mempalace_core extension with the DD
    handle exposed."""
    try:
        mp = importlib.import_module("mempalace_core")
    except ImportError:
        return False
    # The PyDataflowHandle type lands in sub-slice H. For sub-slice B
    # we only need to check that the underlying Rust crate built
    # successfully — which we'd see as the module having any of its
    # known classes. Until sub-slice H wires the dataflow API, we
    # skip behavioral tests by checking whether DataflowHandle is
    # exposed yet.
    return hasattr(mp, "PyDataflowHandle")


@unittest.skipUnless(
    _has_rust_dataflow(),
    "Rust extension with DD handle not built (sub-slice H wires this up)",
)
class TestCurrentNodesBehavioral(unittest.TestCase):
    """End-to-end DD test — exercises real timely worker + real DD
    operators. Skipped until the Rust extension is built and the
    PyDataflowHandle is exposed (sub-slice H).

    These tests document the contract that sub-slice B + H together
    have to fulfill. Running them tomorrow against a built crate is
    how we'll know the conversion actually works.
    """

    def test_create_then_query(self) -> None:
        import mempalace_core as mp  # type: ignore

        handle = mp.PyDataflowHandle.start(["current_nodes"])
        try:
            # Feed a node_created event
            handle.feed(
                offset=1,
                kind="node_created",
                payload=b'{"node_id":"n1","node_kind":"entity","importance":0.7}',
            )
            handle.advance_to(1)
            # Query the view
            result = handle.query("current_nodes", b"n1")
            self.assertIsNotNone(result)
            import json
            v = json.loads(result)
            self.assertEqual(v["node_id"], "n1")
            self.assertEqual(v["node_kind"], "entity")
            self.assertAlmostEqual(v["importance"], 0.7)
        finally:
            handle.shutdown()

    def test_property_set_updates_state(self) -> None:
        import mempalace_core as mp  # type: ignore

        handle = mp.PyDataflowHandle.start(["current_nodes"])
        try:
            handle.feed(
                offset=1,
                kind="node_created",
                payload=b'{"node_id":"n1","node_kind":"entity","importance":0.3}',
            )
            handle.feed(
                offset=2,
                kind="node_property_set",
                payload=b'{"node_id":"n1","field_name":"importance","new_value":0.9}',
            )
            handle.advance_to(2)
            result = handle.query("current_nodes", b"n1")
            import json
            v = json.loads(result)
            self.assertAlmostEqual(v["importance"], 0.9)
            self.assertEqual(v["last_modified_at_offset"], 2)
            # created_at_offset stays at 1
            self.assertEqual(v["created_at_offset"], 1)
        finally:
            handle.shutdown()

    def test_unknown_key_returns_none(self) -> None:
        import mempalace_core as mp  # type: ignore

        handle = mp.PyDataflowHandle.start(["current_nodes"])
        try:
            handle.advance_to(0)
            result = handle.query("current_nodes", b"never_existed")
            self.assertIsNone(result)
        finally:
            handle.shutdown()

    def test_frontier_advances_with_input(self) -> None:
        import mempalace_core as mp  # type: ignore

        handle = mp.PyDataflowHandle.start(["current_nodes"])
        try:
            self.assertEqual(handle.frontier_of("current_nodes"), 0)
            handle.feed(
                offset=5,
                kind="node_created",
                payload=b'{"node_id":"n1","node_kind":"entity"}',
            )
            handle.advance_to(5)
            self.assertGreaterEqual(handle.frontier_of("current_nodes"), 5)
        finally:
            handle.shutdown()


if __name__ == "__main__":
    unittest.main()
