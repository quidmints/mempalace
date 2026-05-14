"""Tests for DD sub-slice H — Python integration.

Sub-slice H is the wire from Python to the `PyDataflowHandle`
exposed by sub-slice F. It also activates the sub-slice G
frontier wiring by calling `PyFrontierRegistry.attach_dataflow`
on bridge initialization.

# What landed

  - `mempalace/log/dataflow_bridge.py` — adapter over PyDataflowHandle
  - `PyFrontierRegistry::attach_dataflow` exposed via PyO3
  - `PyFrontierRegistry::is_dataflow_attached` exposed for tests

# Why structural-only

Same reason as sub-slices A through G: no Rust toolchain in this
environment. The bridge code can be exercised in fallback (no-Rust)
mode — and IS, in these tests — but the real Rust path activates
only when the extension is built.
"""

from __future__ import annotations

import os
import re
import unittest


CORE_SRC = "/home/claude/work/mempalace_core/src"
BINDINGS = f"{CORE_SRC}/pyo3/bindings.rs"
DATAFLOW_BRIDGE = "/home/claude/work/mempalace/log/dataflow_bridge.py"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# =============================================================================
# Python-side: dataflow_bridge.py exists and has the right surface
# =============================================================================


class TestDataflowBridgeImportable(unittest.TestCase):
    """The module must import cleanly, even when the Rust extension
    isn't built. (No Rust import = bridge runs in fallback mode.)"""

    def test_module_imports(self) -> None:
        from mempalace.log import dataflow_bridge  # noqa: F401

    def test_exports_expected_names(self) -> None:
        from mempalace.log import dataflow_bridge as db
        for name in [
            "DataflowBridge",
            "STANDARD_VIEW_NAMES",
            "get_dataflow_bridge",
            "is_dataflow_live",
            "reset_dataflow_probe_for_testing",
            "set_dataflow_bridge",
        ]:
            self.assertTrue(hasattr(db, name), f"missing export: {name}")

    def test_standard_view_names_has_all_14(self) -> None:
        from mempalace.log.dataflow_bridge import STANDARD_VIEW_NAMES
        expected = {
            "current_nodes",
            "current_edges",
            "current_interpretations",
            "current_schemas",
            "heat_field",
            "velocity_field",
            "recurrence_clusters",
            "active_periods",
            "active_iams",
            "open_contradictions",
            "canon_set",
            "pending_review",
            "match_cache",
            "matched_against",
        }
        self.assertEqual(set(STANDARD_VIEW_NAMES), expected)
        self.assertEqual(len(STANDARD_VIEW_NAMES), 14)


class TestDataflowBridgeFallback(unittest.TestCase):
    """When the Rust extension isn't loaded, every method is a safe
    no-op or returns a falsy default. Test the fallback semantics."""

    def setUp(self) -> None:
        from mempalace.log import dataflow_bridge as db
        # Reset any prior probe state
        db.reset_dataflow_probe_for_testing()
        db.set_dataflow_bridge(None)

    def test_is_dataflow_live_returns_false_when_no_extension(self) -> None:
        from mempalace.log.dataflow_bridge import is_dataflow_live
        # In this environment the extension isn't built; should be False.
        self.assertFalse(is_dataflow_live())

    def test_bridge_reports_not_live(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        self.assertFalse(b.is_live)
        self.assertIsNone(b.handle)

    def test_feed_is_noop(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        # Should not raise
        b.feed(1, "node_created", b'{"node_id": "n1"}')

    def test_advance_to_is_noop(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        b.advance_to(100)

    def test_query_returns_none(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        self.assertIsNone(b.query("current_nodes", b"n1"))

    def test_frontier_of_returns_none(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        self.assertIsNone(b.frontier_of("current_nodes"))

    def test_known_views_empty(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        self.assertEqual(b.known_views(), set())

    def test_shutdown_is_noop(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        b.shutdown()  # idempotent

    def test_singleton_initializes_and_replaces(self) -> None:
        from mempalace.log.dataflow_bridge import (
            DataflowBridge,
            get_dataflow_bridge,
            set_dataflow_bridge,
        )
        b1 = get_dataflow_bridge()
        self.assertIsInstance(b1, DataflowBridge)
        # Same instance on subsequent calls
        b2 = get_dataflow_bridge()
        self.assertIs(b1, b2)
        # Replace
        b3 = DataflowBridge()
        set_dataflow_bridge(b3)
        self.assertIs(get_dataflow_bridge(), b3)
        # Reset
        set_dataflow_bridge(None)
        b4 = get_dataflow_bridge()
        self.assertIsNot(b4, b3)


# =============================================================================
# Bridge module structure
# =============================================================================


class TestDataflowBridgeStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(DATAFLOW_BRIDGE):
            raise unittest.SkipTest(f"missing {DATAFLOW_BRIDGE}")
        cls.src = _read(DATAFLOW_BRIDGE)

    def test_attaches_frontier_on_init(self) -> None:
        """When both bridges are live, DataflowBridge.__init__ must
        call PyFrontierRegistry.attach_dataflow so the sub-slice G
        wiring activates."""
        self.assertIn("attach_dataflow", self.src)
        self.assertIn("get_frontier_bridge", self.src)
        # The attach call site must be in _init_if_available
        m = re.search(
            r"def _init_if_available.*?(?=\n    def |\nclass )",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_init_if_available not found")
        body = m.group(0)
        self.assertIn("attach_dataflow", body)

    def test_handle_property_exposes_raw_handle(self) -> None:
        """Behavioral tests want raw access to the PyDataflowHandle
        so they can call methods directly without going through the
        bridge wrappers."""
        self.assertIn("def handle", self.src)
        self.assertIn("@property", self.src)


# =============================================================================
# Rust-side: PyFrontierRegistry.attach_dataflow exposed
# =============================================================================


class TestAttachDataflowExposed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(BINDINGS):
            raise unittest.SkipTest(f"missing {BINDINGS}")
        cls.src = _read(BINDINGS)

    def test_attach_dataflow_method_on_pyfrontier_registry(self) -> None:
        """The PyO3 binding must expose attach_dataflow so the Python
        bridge can call it."""
        # Locate the PyFrontierRegistry impl block
        m = re.search(
            r"#\[pymethods\]\s*impl PyFrontierRegistry\s*\{(.*?)^\}",
            self.src, re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(m, "PyFrontierRegistry impl block not found")
        body = m.group(1)
        self.assertIn("pub fn attach_dataflow", body)
        # Takes a PyDataflowHandle reference
        self.assertIn("PyDataflowHandle", body)

    def test_is_dataflow_attached_helper_exposed(self) -> None:
        m = re.search(
            r"#\[pymethods\]\s*impl PyFrontierRegistry\s*\{(.*?)^\}",
            self.src, re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("pub fn is_dataflow_attached", body)


# =============================================================================
# Behavioral tests — skipped when the extension isn't built
# =============================================================================


def _has_dataflow() -> bool:
    try:
        from mempalace.log.dataflow_bridge import is_dataflow_live
        return is_dataflow_live()
    except Exception:
        return False


@unittest.skipUnless(_has_dataflow(), "PyDataflowHandle not built")
class TestDataflowBridgeLiveIntegration(unittest.TestCase):
    """When the extension IS built, verify end-to-end:
       - Bridge starts and is_live = True.
       - Feed/advance/query against current_nodes works.
       - FrontierBridge sees attach_dataflow having taken effect."""

    def setUp(self) -> None:
        from mempalace.log import dataflow_bridge as db
        from mempalace.log import rust_bridge as rb
        db.reset_dataflow_probe_for_testing()
        db.set_dataflow_bridge(None)
        rb.reset_probe_for_testing()
        rb.set_frontier_bridge(None)

    def test_bridge_initializes_live(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        b = DataflowBridge()
        try:
            self.assertTrue(b.is_live)
            self.assertIsNotNone(b.handle)
        finally:
            b.shutdown()

    def test_feed_then_query_current_nodes(self) -> None:
        from mempalace.log.dataflow_bridge import DataflowBridge
        import json
        b = DataflowBridge(view_names=["current_nodes"])
        try:
            b.feed(
                offset=1,
                kind="node_created",
                payload=json.dumps({
                    "node_id": "n_test",
                    "node_kind": "concept",
                    "properties": {},
                }).encode(),
            )
            b.advance_to(1)
            result = b.query("current_nodes", b"n_test")
            self.assertIsNotNone(result, "expected node to be in view")
            v = json.loads(result)
            self.assertEqual(v["node_id"], "n_test")
        finally:
            b.shutdown()

    def test_frontier_bridge_sees_dataflow_attached(self) -> None:
        from mempalace.log.dataflow_bridge import get_dataflow_bridge
        from mempalace.log.rust_bridge import get_frontier_bridge
        # Just instantiating the dataflow bridge should attach
        df_bridge = get_dataflow_bridge()
        try:
            self.assertTrue(df_bridge.is_live)
            fb = get_frontier_bridge()
            if fb.is_live:
                rust_registry = fb._state.rust_registry  # noqa: SLF001
                self.assertTrue(rust_registry.is_dataflow_attached())
        finally:
            df_bridge.shutdown()


if __name__ == "__main__":
    unittest.main()
