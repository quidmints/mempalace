"""Tests for DD sub-slice A — dataflow module scaffolding.

The Rust-side `mempalace_core::dataflow` module stands up the
infrastructure that all 14 views will plug into:
  - `DataflowHandle` — application-side handle to a worker thread
  - `ViewSpec` trait — what each view implements (replaces `View`)
  - `EventTuple` — the unit pushed into the input session
  - `WorkerCommand` — control messages to the worker

This sub-slice is pure scaffolding. It does NOT convert any view yet
(sub-slice B converts `current_nodes` first). The worker main loop is
a stub that acks readiness and discards events.

These tests run unconditionally (they read source) so structural
issues are caught even without a Rust build. Real DD-backed
behavioral tests land in sub-slice B when `current_nodes` becomes a
DD view.
"""

from __future__ import annotations

import os
import re
import unittest


DATAFLOW_PATH = "/home/claude/work/mempalace_core/src/dataflow/mod.rs"
LIB_PATH = "/home/claude/work/mempalace_core/src/lib.rs"
CARGO_PATH = "/home/claude/work/mempalace_core/Cargo.toml"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class TestDataflowModuleStructure(unittest.TestCase):
    """The dataflow module exposes the expected types and methods."""

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(DATAFLOW_PATH):
            raise unittest.SkipTest(f"dataflow module missing at {DATAFLOW_PATH}")
        cls.src = _read(DATAFLOW_PATH)

    def test_imports_dd_and_timely(self) -> None:
        """The module imports the actual DD/timely surface — not just
        listing them in Cargo.toml."""
        self.assertIn("use differential_dataflow::input::InputSession", self.src)
        self.assertIn("use differential_dataflow::Collection", self.src)
        self.assertIn("use timely::dataflow::Scope", self.src)

    def test_event_tuple_is_dataflow_data(self) -> None:
        """EventTuple needs the trait bounds DD requires: Clone +
        Debug + PartialEq + Eq + Hash + PartialOrd + Ord."""
        # Locate the EventTuple struct
        m = re.search(
            r"#\[derive\(([^)]+)\)\]\s*pub struct EventTuple",
            self.src,
        )
        self.assertIsNotNone(m, "EventTuple struct not found")
        derives = m.group(1)
        for required in ["Clone", "Debug", "PartialEq", "Eq", "Hash", "PartialOrd", "Ord"]:
            self.assertIn(
                required, derives,
                f"EventTuple missing required derive: {required}",
            )

    def test_view_spec_trait_present(self) -> None:
        self.assertIn("pub trait ViewSpec", self.src)
        self.assertIn("fn name(&self) -> &'static str", self.src)
        self.assertIn("fn subscribed_kinds(&self) -> &[&'static str]", self.src)
        self.assertIn("fn build", self.src)

    def test_dataflow_handle_methods(self) -> None:
        for method in [
            "pub fn start",
            "pub fn feed",
            "pub fn feed_batch",
            "pub fn advance_to",
            "pub fn frontier_of",
            "pub fn query",
            "pub fn snapshot",
            "pub fn known_views",
            "pub fn shutdown",
        ]:
            self.assertIn(
                method, self.src,
                f"DataflowHandle missing method: {method}",
            )

    def test_worker_runs_on_dedicated_thread(self) -> None:
        """The worker uses thread::spawn and joins on shutdown."""
        self.assertIn("thread::spawn", self.src)
        self.assertIn("JoinHandle", self.src)

    def test_command_channel_used(self) -> None:
        """Application <-> worker uses crossbeam-channel."""
        self.assertIn("crossbeam_channel", self.src)
        # Both bounded and unbounded are used (unbounded for cmd_tx,
        # bounded for replies)
        self.assertIn("unbounded", self.src)
        self.assertIn("bounded", self.src)

    def test_timestamp_is_log_offset(self) -> None:
        """The dataflow's logical time is the log offset."""
        self.assertIn("DataflowTimestamp = LogOffset", self.src)

    def test_inline_tests_present(self) -> None:
        self.assertIn("#[cfg(test)]", self.src)
        self.assertIn("fn handle_starts_and_lists_known_views", self.src)
        self.assertIn("fn feed_does_not_error_with_stub", self.src)
        self.assertIn("fn advance_to_returns_with_stub", self.src)
        self.assertIn("fn query_unknown_view_errors", self.src)

    def test_todo_markers_localized_to_uncertain_spots(self) -> None:
        """Every guess at the DD/timely API surface is marked
        TODO(rust-build) so it can be confirmed on first build."""
        todos = re.findall(r"TODO\(rust-build\)", self.src)
        # Expect at least 5 — the module is full of API guesses.
        # An upper bound catches accidental TODO sprawl beyond the
        # intentional uncertainty markers.
        self.assertGreaterEqual(len(todos), 5)
        self.assertLessEqual(len(todos), 30)


class TestModuleRegistered(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(LIB_PATH):
            raise unittest.SkipTest("lib.rs missing")
        cls.src = _read(LIB_PATH)

    def test_dataflow_module_in_lib(self) -> None:
        self.assertIn("pub mod dataflow", self.src)


class TestCargoToml(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(CARGO_PATH):
            raise unittest.SkipTest("Cargo.toml missing")
        cls.src = _read(CARGO_PATH)

    def test_dd_and_timely_listed(self) -> None:
        self.assertIn("differential-dataflow", self.src)
        self.assertIn("timely", self.src)

    def test_required_concurrency_deps(self) -> None:
        self.assertIn("crossbeam-channel", self.src)
        self.assertIn("parking_lot", self.src)
        self.assertIn("thiserror", self.src)


class TestStubBehaviorContract(unittest.TestCase):
    """Sub-slice A is intentionally a stub. These tests document that
    contract so sub-slice B knows what it's replacing."""

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(DATAFLOW_PATH):
            raise unittest.SkipTest("dataflow module missing")
        cls.src = _read(DATAFLOW_PATH)

    def test_stub_trace_returns_none(self) -> None:
        """StubTrace::query_bytes returns None until sub-slice B
        replaces it."""
        # Find StubTrace impl and assert it has the documented stub
        # behavior
        self.assertIn("struct StubTrace", self.src)
        self.assertIn("impl TraceQuery for StubTrace", self.src)
        # query_bytes returns None
        self.assertIn("fn query_bytes(&self, _key_bytes: &[u8]) -> Option<Vec<u8>> {\n        None\n    }", self.src)

    def test_main_loop_acknowledges_readiness(self) -> None:
        """The stub worker still has to ack readiness so the
        DataflowHandle::start path doesn't time out."""
        self.assertIn("ready_tx.send(Ok(()))", self.src)

    def test_stub_marked_for_replacement(self) -> None:
        """The stub paths are clearly marked with TODO(rust-build)
        and the comment indicates which sub-slice replaces them."""
        self.assertIn("Sub-slice B replaces this", self.src)


if __name__ == "__main__":
    unittest.main()
