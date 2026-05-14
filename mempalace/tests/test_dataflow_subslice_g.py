"""Tests for DD sub-slice G — timely-driven frontier.

Sub-slice G replaces the parking_lot-shadow `applied_offset` in
`views/frontier.rs` with a read against the DD `DataflowHandle`.
The Phase-5 frontier registry surface stays the same, but its
*source of truth* changes when a `DataflowHandle` is attached:

  - Pre-G:  `applied_offset` was stored locally and advanced via
            `record_applied()` calls from the application.
  - Post-G: `applied_offset` is read from `DataflowHandle::frontier_of`
            after `FrontierRegistry::attach_dataflow(handle)`.

The batch-coordination layer (open_batches, lowest_open_batch_start)
is unchanged — that's an application concern DD doesn't know about.

# Why this is structural-only

The behavioral confirmation that `applied_offset` reads the live
frontier requires actually running a DD worker, which requires the
Rust extension to be built. We can verify the *structure* of the
change here:

  - `AppliedSource` enum has both Local and Dataflow variants.
  - `FrontierTracker` has an `attach_dataflow` method.
  - `FrontierRegistry` has `attach_dataflow` and `is_dataflow_attached`.
  - `record_applied` is a no-op when the source is Dataflow.
  - The legacy unit tests (record_applied without attach) are
    preserved unchanged.
"""

from __future__ import annotations

import os
import re
import unittest


FRONTIER = "/home/claude/work/mempalace_core/src/views/frontier.rs"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class TestAppliedSourceEnum(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_enum_defined(self) -> None:
        """AppliedSource enum has Local + Dataflow variants."""
        self.assertRegex(self.src, r"enum AppliedSource\s*\{")
        # Both variants present
        self.assertRegex(self.src, r"AppliedSource::Local\b|^\s*Local,")
        self.assertIn("Dataflow {", self.src)

    def test_dataflow_variant_holds_handle_and_view_name(self) -> None:
        """The Dataflow variant must carry an Arc<DataflowHandle>
        and the view name (the registry → frontier_of bridge needs
        both)."""
        m = re.search(
            r"Dataflow\s*\{[^}]+\}",
            self.src,
        )
        self.assertIsNotNone(m, "Dataflow variant body not found")
        body = m.group(0)
        self.assertIn("handle:", body)
        self.assertIn("view_name", body)
        self.assertIn("DataflowHandle", body)


class TestFrontierTrackerWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_tracker_holds_source(self) -> None:
        """FrontierTracker must have a source field of type AppliedSource."""
        m = re.search(
            r"pub struct FrontierTracker\s*\{[^}]+\}",
            self.src,
        )
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("source:", body)
        self.assertIn("AppliedSource", body)

    def test_tracker_has_attach_dataflow(self) -> None:
        """The tracker exposes a method to switch into Dataflow mode."""
        self.assertRegex(
            self.src,
            r"fn attach_dataflow\(\s*&self,\s*handle:.*?DataflowHandle.*?,\s*view_name:",
        )

    def test_applied_offset_reads_from_source(self) -> None:
        """applied_offset() must dispatch on AppliedSource."""
        m = re.search(
            r"pub fn applied_offset\(.*?\n    \}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "applied_offset method not found")
        body = m.group(0)
        # Reads the source enum
        self.assertIn("AppliedSource::Local", body)
        self.assertIn("AppliedSource::Dataflow", body)
        # Calls frontier_of in Dataflow branch
        self.assertIn("frontier_of", body)

    def test_record_applied_is_noop_in_dataflow_mode(self) -> None:
        """record_applied must early-return when source is Dataflow.
        The DD frontier is the source of truth — duplicate writes
        to the local applied_offset would be confusing."""
        m = re.search(
            r"pub fn record_applied\(.*?\n    \}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "record_applied method not found")
        body = m.group(0)
        # The noop early-return references the source check
        self.assertIn("AppliedSource::Local", body)
        # Some form of early-return when not Local. We accept either
        # an explicit `return;` after a guard or a `match` that does
        # nothing in the Dataflow arm.
        has_early_return = "return" in body
        self.assertTrue(
            has_early_return,
            "record_applied must short-circuit when in Dataflow mode",
        )


class TestFrontierRegistryAttach(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_registry_has_dataflow_field(self) -> None:
        """FrontierRegistry must hold an Option<Arc<DataflowHandle>>."""
        m = re.search(
            r"pub struct FrontierRegistry\s*\{[^}]+\}",
            self.src,
        )
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("dataflow:", body)
        self.assertIn("Option", body)
        self.assertIn("DataflowHandle", body)

    def test_registry_has_attach_dataflow(self) -> None:
        self.assertRegex(
            self.src,
            r"pub fn attach_dataflow\(\s*&self,\s*handle:.*?DataflowHandle",
        )

    def test_attach_repoints_existing_trackers(self) -> None:
        """When attach is called after some trackers are already
        registered, those existing trackers must be switched to
        Dataflow mode."""
        m = re.search(
            r"pub fn attach_dataflow\(.*?\n    \}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "attach_dataflow method not found")
        body = m.group(0)
        # Iterate trackers and call attach on each
        self.assertIn("self.views.read()", body)
        self.assertIn("attach_dataflow", body)

    def test_register_after_attach_uses_dataflow(self) -> None:
        """register() must check whether a dataflow is attached and
        immediately put new trackers in Dataflow mode."""
        m = re.search(
            r"pub fn register\(.*?\n    \}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "register method not found")
        body = m.group(0)
        # Reads the dataflow Option
        self.assertIn("self.dataflow.read()", body)
        # Conditionally attaches the new tracker
        self.assertIn("attach_dataflow", body)

    def test_is_dataflow_attached_helper(self) -> None:
        self.assertRegex(self.src, r"pub fn is_dataflow_attached\(&self\)")


class TestCommittedOffsetSemantics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_committed_offset_is_min_of_applied_and_batch_cap(self) -> None:
        """committed_offset = min(applied, lowest_open_batch_start - 1)
        when a batch is open. This is the mechanism that gives readers
        consistent cross-view snapshots — independent of where
        applied_offset comes from."""
        m = re.search(
            r"pub fn committed_offset\(.*?\n    \}",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "committed_offset method not found")
        body = m.group(0)
        # Reads applied (live)
        self.assertIn("self.applied_offset()", body)
        # Inspects lowest_open_batch_start
        self.assertIn("lowest_open_batch_start", body)


class TestLegacyTestsPreserved(unittest.TestCase):
    """Ensure the pre-G unit tests (which use Local mode) are still
    in the module — guards against accidental regression."""

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_legacy_unit_tests_present(self) -> None:
        for t in [
            "fn fresh_tracker_has_zero_offsets",
            "fn record_applied_advances_both_offsets_when_no_open_batch",
            "fn open_batch_caps_committed_offset",
            "fn close_batch_lifts_committed_offset",
            "fn registry_meet_returns_min_committed",
            "fn registry_open_batch_pulls_all_views_back",
        ]:
            self.assertIn(t, self.src,
                          f"legacy test removed: {t}")


class TestNewGModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(FRONTIER):
            raise unittest.SkipTest(f"missing {FRONTIER}")
        cls.src = _read(FRONTIER)

    def test_g_mode_tests_present(self) -> None:
        for t in [
            "fn registry_starts_unattached",
            "fn attach_dataflow_switches_applied_to_handle",
            "fn register_after_attach_uses_dataflow_mode",
        ]:
            self.assertIn(t, self.src,
                          f"new G-mode test missing: {t}")


if __name__ == "__main__":
    unittest.main()
