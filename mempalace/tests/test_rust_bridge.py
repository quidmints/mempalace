"""Tests for the Python ↔ Rust frontier bridge.

Two test classes:

  - `TestBridgeFallbackPath`: runs whether or not the Rust extension
    is built. Confirms the bridge is no-ops-correct when Rust is
    absent, and that all public APIs continue to work via the
    scan-based fallback.

  - `TestBridgeRustPath`: runs only when `mempalace_core` is
    importable. Confirms the Rust-backed path produces the same
    answers as the scan-based path. Skipped on environments without
    the extension.

The structural alignment between Python and Rust is already covered
by `test_phase5_frontier_alignment.TestRustFrontierStructure` and
`TestPyO3BindingsExposure`. This file tests the *runtime wire*.
"""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.log.frontier import FrontierRegistry, set_frontier_registry
from mempalace.log.rust_bridge import (
    FrontierBridge,
    get_frontier_bridge,
    is_rust_available,
    reset_probe_for_testing,
    set_frontier_bridge,
)
from mempalace.schema.events import BatchStarted, NodeCreated
from mempalace.schema.identifiers import make_batch_id, make_theme_id
from mempalace.tests.conftest import reset_module_state


# =============================================================================
# Fallback-path tests — run unconditionally
# =============================================================================


class TestBridgeFallbackPath(unittest.TestCase):
    """The bridge is no-ops when Rust isn't present. Public APIs of
    `FrontierRegistry` continue to work via the scan-based fallback."""

    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)
        # Force a fresh bridge that probes again
        set_frontier_bridge(None)
        reset_probe_for_testing()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.registry = FrontierRegistry(log=self.log)

    def test_bridge_committed_offset_returns_none_when_rust_absent(self) -> None:
        bridge = get_frontier_bridge()
        # If Rust isn't built in this environment, is_live should be False
        # and committed_offset should return None.
        if not is_rust_available():
            self.assertFalse(bridge.is_live)
            self.assertIsNone(bridge.committed_offset("anyone"))
            self.assertIsNone(bridge.meet(["a", "b"]))
            self.assertEqual(bridge.known_views(), set())

    def test_bridge_writes_are_safe_no_ops_when_rust_absent(self) -> None:
        """notify_* methods must not raise even when Rust isn't there."""
        bridge = get_frontier_bridge()
        # These should all silently no-op
        bridge.notify_applied("c1", 5)
        bridge.notify_batch_opened("c1", "bat_x", 10)
        bridge.notify_batch_closed("c1", "bat_x")

    def test_frontier_registry_works_without_rust(self) -> None:
        """The keystone test: even without Rust, FrontierRegistry
        produces correct frontier_of / meet answers."""
        with self.log.batch("A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        with self.log.batch("B") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "2"},
            ))

        # Frontier reads via fallback path
        f_a = self.registry.frontier_of("A")
        f_b = self.registry.frontier_of("B")
        self.assertGreater(f_a, 0)
        self.assertGreater(f_b, 0)
        # Meet works via fallback
        meet = self.registry.meet(["A", "B"])
        self.assertEqual(meet, self.backend.current_offset())

    def test_torn_batch_visible_via_fallback(self) -> None:
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="torn_writer", expected_count=1, batch_id=bid,
        ))
        torn_offset = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))

        # Fallback path computes the rolled-back frontier
        f = self.registry.frontier_of("torn_writer")
        self.assertEqual(f, torn_offset - 1)

    def test_logclient_append_does_not_raise_when_bridge_absent(self) -> None:
        """The wire in LogClient._notify_rust_frontier must never
        propagate exceptions back to the caller."""
        # Append a batch_started — the bridge call is invoked
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="any", expected_count=1, batch_id=bid,
        ))
        # And in-batch event
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))
        # If we got here, append did not propagate any error.

    def test_implicit_single_event_batches_skip_bridge(self) -> None:
        """Events with batch_id="" are not part of any consumer's
        frontier; the bridge should not be called for them.

        We can't directly observe "the bridge wasn't called" without
        a mock, but we CAN observe that this append doesn't raise
        and that the registry isn't polluted with phantom consumers.
        """
        # Append an event with no batch_id (implicit single-event batch)
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "lone"},
        ))
        # No consumer was registered
        self.assertNotIn("", self.registry._frontier)


# =============================================================================
# Rust-path tests — only run when the extension is built
# =============================================================================


@unittest.skipUnless(
    is_rust_available(),
    "mempalace_core extension not built; skipping Rust-path tests. "
    "Build with `cargo build --release -p mempalace_core` to enable.",
)
class TestBridgeRustPath(unittest.TestCase):
    """When the extension is built, the bridge forwards lifecycle
    and applied events to Rust, and committed_offset / meet come
    from Rust.

    These tests run only if `import mempalace_core` succeeds AND
    the class `PyFrontierRegistry` is exposed on it.
    """

    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)
        set_frontier_bridge(None)
        reset_probe_for_testing()
        # Force probe to populate
        is_rust_available()

        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.registry = FrontierRegistry(log=self.log)
        self.bridge = get_frontier_bridge()

    def test_bridge_is_live(self) -> None:
        self.assertTrue(self.bridge.is_live)

    def test_clean_batch_advances_rust_committed_offset(self) -> None:
        with self.log.batch("R1") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "rust"},
            ))
        # Rust knows R1 now
        self.assertIn("R1", self.bridge.known_views())
        rust_committed = self.bridge.committed_offset("R1")
        self.assertIsNotNone(rust_committed)
        self.assertEqual(rust_committed, self.backend.current_offset())

    def test_open_batch_caps_rust_committed_offset(self) -> None:
        with self.log.batch("R1") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "first"},
            ))
        clean_offset = self.backend.current_offset()

        # Open a torn batch on R2
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="R2", expected_count=2, batch_id=bid,
        ))
        torn_start = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "torn"}, batch_id=bid,
        ))

        # Rust says R2's committed is capped at torn_start - 1
        self.assertEqual(
            self.bridge.committed_offset("R2"),
            torn_start - 1,
        )

    def test_rust_and_fallback_paths_agree(self) -> None:
        """Same scenario, both paths produce the same frontier_of
        and meet answers."""
        with self.log.batch("A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        with self.log.batch("B") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "2"},
            ))

        # Get answers via the registry (which uses Rust because it's live)
        rust_fa = self.registry.frontier_of("A")
        rust_fb = self.registry.frontier_of("B")
        rust_meet = self.registry.meet(["A", "B"])

        # Now disable the bridge and reset the registry to force the
        # fallback path
        self.registry.mark_all_dirty()
        set_frontier_bridge(FrontierBridge())  # fresh, but still live
        # Hack: simulate the fallback by directly instantiating a
        # bridge with rust_registry forced to None
        fb = FrontierBridge()
        fb._state.rust_registry = None  # type: ignore[attr-defined]
        set_frontier_bridge(fb)
        self.registry.mark_all_dirty()

        fallback_fa = self.registry.frontier_of("A")
        fallback_fb = self.registry.frontier_of("B")
        fallback_meet = self.registry.meet(["A", "B"])

        self.assertEqual(rust_fa, fallback_fa)
        self.assertEqual(rust_fb, fallback_fb)
        self.assertEqual(rust_meet, fallback_meet)


if __name__ == "__main__":
    unittest.main()
