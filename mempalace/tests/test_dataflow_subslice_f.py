"""Tests for DD sub-slice F — delete legacy + reroute to dataflow.

Sub-slice F is the architectural flip: DD becomes the only path.
Concretely:

1. The 14 legacy `parking_lot::RwLock<HashMap>` view files in
   `mempalace_core/src/views/` are deleted.
2. The legacy `View` trait + `ViewBuilder` (in `views/builder.rs`)
   are deleted.
3. `views/mod.rs` shrinks to expose only `frontier` (kept for
   sub-slice G; goes away then).
4. `pyo3/bindings.rs` is rewritten so `PyLogClient.append` feeds
   the DD `DataflowHandle` instead of the legacy `ViewBuilder`.
5. A new `PyDataflowHandle` class is exposed for direct view
   queries (used by sub-slice B/C/D/E behavioral tests and by
   sub-slice H's full Python integration).

# Why this can't be behaviorally tested without a Rust build

The Python side hasn't been switched to use `PyDataflowHandle` yet
— that's sub-slice H. Until then, the Python tests in earlier
suites (test_log, test_views, etc.) continue using the existing
in-Python implementations and don't touch the Rust extension. The
214+ existing tests should stay green because nothing they call
has changed.

So sub-slice F is *fully* a structural change for now. The
structural assertions:

  - Legacy view files no longer exist.
  - `views/mod.rs` doesn't import the deleted modules.
  - `bindings.rs` imports DD views and constructs a `DataflowHandle`.
  - `bindings.rs` exposes `PyDataflowHandle`.
"""

from __future__ import annotations

import os
import re
import unittest


CORE_SRC = "/home/claude/work/mempalace_core/src"
VIEWS_DIR = f"{CORE_SRC}/views"
VIEWS_MOD = f"{VIEWS_DIR}/mod.rs"
BINDINGS = f"{CORE_SRC}/pyo3/bindings.rs"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# =============================================================================
# Legacy view files are gone
# =============================================================================


class TestLegacyViewsDeleted(unittest.TestCase):
    """All 14 legacy views + builder.rs are removed."""

    LEGACY_VIEW_FILES = [
        "active_iams.rs",
        "active_periods.rs",
        "builder.rs",
        "canon_set.rs",
        "current_edges.rs",
        "current_interpretations.rs",
        "current_nodes.rs",
        "current_schemas.rs",
        "heat_field.rs",
        "match_cache.rs",
        "matched_against.rs",
        "open_contradictions.rs",
        "pending_review.rs",
        "recurrence_clusters.rs",
        "velocity_field.rs",
    ]

    def test_legacy_files_removed(self) -> None:
        for f in self.LEGACY_VIEW_FILES:
            path = f"{VIEWS_DIR}/{f}"
            self.assertFalse(
                os.path.exists(path),
                f"Legacy view file should have been deleted: {path}",
            )

    def test_only_frontier_and_mod_remain(self) -> None:
        """The views/ directory now contains only frontier.rs + mod.rs."""
        if not os.path.isdir(VIEWS_DIR):
            self.skipTest(f"{VIEWS_DIR} missing entirely")
        files = sorted(os.listdir(VIEWS_DIR))
        self.assertEqual(
            files, ["frontier.rs", "mod.rs"],
            f"unexpected contents of {VIEWS_DIR}: {files}",
        )


# =============================================================================
# views/mod.rs no longer imports deleted modules
# =============================================================================


class TestViewsModulePruned(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(VIEWS_MOD):
            raise unittest.SkipTest(f"missing {VIEWS_MOD}")
        cls.src = _read(VIEWS_MOD)

    def test_does_not_import_legacy_views(self) -> None:
        """No `pub mod current_nodes;` etc — those moved to
        `dataflow/views/`."""
        for legacy in [
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
            "builder",
        ]:
            # Each legacy module should not appear as a `pub mod` decl
            self.assertNotRegex(
                self.src,
                rf"pub\s+mod\s+{re.escape(legacy)}\s*;",
                f"views/mod.rs still declares pub mod {legacy}",
            )

    def test_still_exposes_frontier(self) -> None:
        """Phase-5 frontier registry stays here until sub-slice G."""
        self.assertIn("pub mod frontier", self.src)
        self.assertIn("FrontierRegistry", self.src)


# =============================================================================
# bindings.rs is rewired to DataflowHandle
# =============================================================================


class TestBindingsRerouted(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(BINDINGS):
            raise unittest.SkipTest(f"missing {BINDINGS}")
        cls.src = _read(BINDINGS)

    def test_imports_dd_views_not_legacy(self) -> None:
        """bindings.rs must import from `crate::dataflow::views`,
        not from `crate::views`."""
        self.assertIn("crate::dataflow::views", self.src)
        # No legacy view imports
        self.assertNotIn("use crate::views::active_iams", self.src)
        self.assertNotIn("use crate::views::current_nodes", self.src)
        self.assertNotIn("use crate::views::builder::ViewBuilder", self.src)
        self.assertNotIn("use crate::views::builder::{", self.src)

    def test_imports_dataflow_handle(self) -> None:
        self.assertIn("DataflowHandle", self.src)
        self.assertIn("ViewSpec", self.src)

    def test_pylog_client_uses_handle_not_builder(self) -> None:
        """PyLogClient must hold a DataflowHandle, not a ViewBuilder."""
        # Locate the PyLogClient struct
        m = re.search(
            r"pub struct PyLogClient\s*\{[^}]+\}",
            self.src,
        )
        self.assertIsNotNone(m, "PyLogClient struct not found")
        body = m.group(0)
        self.assertIn("DataflowHandle", body)
        # Check there's no actual `ViewBuilder` field declaration —
        # docstrings mentioning the legacy name are fine.
        # A field decl would look like `something: ViewBuilder` or
        # `something: Arc<...ViewBuilder...>`.
        self.assertNotRegex(
            body,
            r":\s*[^,/\n]*ViewBuilder",
            "PyLogClient must not have a ViewBuilder-typed field",
        )

    def test_append_calls_feed_and_advance(self) -> None:
        """`append` should feed the DD handle and wait for advance."""
        # Find the append method
        m = re.search(
            r"pub fn append\(.*?fn current_offset",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "append method not found")
        body = m.group(0)
        self.assertIn("self.handle.feed(", body)
        self.assertIn("self.handle.advance_to(", body)
        # No reference to legacy apply_batch
        self.assertNotIn("apply_batch", body)

    def test_standard_views_constructs_all_14(self) -> None:
        """The factory function builds all 14 ViewSpec impls."""
        self.assertIn("fn standard_views()", self.src)
        for view_struct in [
            "CurrentNodesView::new()",
            "CurrentEdgesView::new()",
            "CurrentInterpretationsView::new()",
            "CurrentSchemasView::new()",
            "HeatFieldView::new()",
            "VelocityFieldView::new()",
            "RecurrenceClustersView::new()",
            "ActivePeriodsView::new()",
            "ActiveIamsView::new()",
            "OpenContradictionsView::new()",
            "CanonSetView::new()",
            "PendingReviewView::new()",
            "MatchCacheView::new()",
            "MatchedAgainstView::new()",
        ]:
            self.assertIn(view_struct, self.src,
                          f"standard_views must build {view_struct}")


# =============================================================================
# PyDataflowHandle is exposed
# =============================================================================


class TestPyDataflowHandleExposed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(BINDINGS):
            raise unittest.SkipTest(f"missing {BINDINGS}")
        cls.src = _read(BINDINGS)

    def test_class_defined(self) -> None:
        self.assertIn("pub struct PyDataflowHandle", self.src)
        self.assertIn("impl PyDataflowHandle", self.src)

    def test_has_required_methods(self) -> None:
        """The behavioral tests (sub-slice B/C/D/E) call these."""
        m = re.search(
            r"#\[pymethods\]\s*impl PyDataflowHandle\s*\{(.*?)^\}",
            self.src, re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(m, "PyDataflowHandle impl block not found")
        body = m.group(1)
        for method in [
            "pub fn start",
            "pub fn feed",
            "pub fn advance_to",
            "pub fn frontier_of",
            "pub fn query",
            "pub fn known_views",
            "pub fn shutdown",
        ]:
            self.assertIn(method, body,
                          f"PyDataflowHandle missing {method}")

    def test_start_handles_named_subset_and_all(self) -> None:
        """start() accepts a list; empty list = all 14 views."""
        m = re.search(
            r"pub fn start\(.*?fn feed",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("view_names.is_empty()", body)
        self.assertIn("standard_views()", body)
        # Each named view has a match arm
        for name in [
            "current_nodes", "current_edges", "current_interpretations",
            "heat_field", "active_iams", "match_cache",
        ]:
            self.assertIn(f'"{name}"', body)

    def test_registered_with_module(self) -> None:
        """The Python module exports PyDataflowHandle."""
        self.assertIn("m.add_class::<PyDataflowHandle>", self.src)


# =============================================================================
# PyFrontierRegistry preserved (Phase-5 surface unchanged)
# =============================================================================


class TestPyFrontierRegistryPreserved(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(BINDINGS):
            raise unittest.SkipTest(f"missing {BINDINGS}")
        cls.src = _read(BINDINGS)

    def test_class_still_defined(self) -> None:
        """Phase-5 surface is unchanged in F. Python's rust_bridge.py
        keeps using it. Sub-slice G replaces the underlying
        FrontierRegistry; the API stays the same."""
        self.assertIn("pub struct PyFrontierRegistry", self.src)
        self.assertIn("crate::views::FrontierRegistry", self.src)

    def test_methods_intact(self) -> None:
        for method in [
            "pub fn register",
            "pub fn record_applied",
            "pub fn record_batch_started",
            "pub fn record_batch_closed",
            "pub fn committed_offset",
            "pub fn applied_offset",
            "pub fn meet",
            "pub fn known_views",
            "pub fn open_batch_count",
        ]:
            self.assertIn(method, self.src)


if __name__ == "__main__":
    unittest.main()
