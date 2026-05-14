"""Tests for Phase 3 — frontier tracking + cross-consumer meet."""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.log.frontier import (
    ConsistentReadView,
    FrontierRegistry,
    consistent_read_snapshot,
    get_frontier_registry,
    set_frontier_registry,
)
from mempalace.schema.events import (
    BatchStarted,
    NodeCreated,
)
from mempalace.schema.identifiers import make_batch_id, make_theme_id
from mempalace.tests.conftest import reset_module_state


class TestFrontierRegistry(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.registry = FrontierRegistry(log=self.log)

    def test_unknown_consumer_returns_zero(self) -> None:
        self.assertEqual(self.registry.frontier_of("nobody"), 0)

    def test_clean_writer_has_frontier_at_log_tail(self) -> None:
        with self.log.batch("writer.A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "X"},
            ))
        self.registry.refresh_from_log()
        self.assertEqual(
            self.registry.frontier_of("writer.A"),
            self.backend.current_offset(),
        )

    def test_torn_writer_frontier_rolls_back(self) -> None:
        # Open a batch but don't close
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="writer.B", expected_count=1, batch_id=bid,
        ))
        torn_offset = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))

        self.registry.refresh_from_log()
        self.assertEqual(self.registry.frontier_of("writer.B"), torn_offset - 1)

    def test_caching_avoids_re_scan(self) -> None:
        with self.log.batch("writer.C") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "X"},
            ))
        # First read: triggers scan
        f1 = self.registry.frontier_of("writer.C")
        # Add more events not from writer.C
        with self.log.batch("writer.D") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "Y"},
            ))
        # Without invalidation, writer.C's frontier is stale
        f2 = self.registry.frontier_of("writer.C")
        self.assertEqual(f1, f2)  # cached
        # After invalidation, frontier advances (more events committed
        # before/after writer.C means writer.C's clean frontier moves up)
        self.registry.mark_dirty("writer.C")
        f3 = self.registry.frontier_of("writer.C")
        self.assertGreaterEqual(f3, f2)


class TestMeetOperator(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.registry = FrontierRegistry(log=self.log)

    def test_meet_of_two_clean_consumers_returns_min(self) -> None:
        with self.log.batch("A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        midpoint = self.backend.current_offset()
        with self.log.batch("B") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "2"},
            ))
        self.registry.refresh_from_log()

        # B is later → min is A's frontier
        # But A's frontier extends to log tail (it has no torn batches),
        # same for B. So meet equals max-of-clean = log tail.
        meet = self.registry.meet(["A", "B"])
        self.assertEqual(meet, self.backend.current_offset())

    def test_meet_constrained_by_torn_consumer(self) -> None:
        # A clean
        with self.log.batch("A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        # B torn
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="B", expected_count=1, batch_id=bid,
        ))
        torn_offset = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))

        self.registry.refresh_from_log()

        meet = self.registry.meet(["A", "B"])
        # Meet is constrained by B's rollback
        self.assertEqual(meet, torn_offset - 1)
        # Strictly less than A's frontier
        self.assertLess(meet, self.registry.frontier_of("A"))

    def test_empty_consumer_set_returns_log_tail(self) -> None:
        # Conventional: empty constraint = no constraint = log tail
        with self.log.batch("X") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        self.registry.refresh_from_log()
        self.assertEqual(self.registry.meet([]), self.backend.current_offset())


class TestConsistentReadSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_snapshot_filters_in_flight_events(self) -> None:
        # Writer A: clean, 2 events
        with self.log.batch("A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "1"},
            ))
        a_clean_end = self.backend.current_offset()

        # Writer B: torn — opens batch, partial events, no close
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="B", expected_count=2, batch_id=bid,
        ))
        b_torn_start = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "2"}, batch_id=bid,
        ))

        # A reader needing a consistent view across A + B
        view = consistent_read_snapshot(
            ["A", "B"], log=self.log,
        )

        # Meet should be at or before B's torn batch start
        self.assertLessEqual(view.meet_offset, b_torn_start - 1)
        # And the events in the read range exclude B's in-flight events
        events = view.read_events()
        b_torn_events = [
            e for e in events if e[2].get("batch_id") == bid
            and e[1] != "batch_started"
        ]
        self.assertEqual(len(b_torn_events), 0)

    def test_snapshot_is_consistent_for_each_consumer(self) -> None:
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

        view = consistent_read_snapshot(["A", "B"], log=self.log)
        # Both consumers' frontiers >= meet_offset
        self.assertTrue(view.is_consistent_for("A"))
        self.assertTrue(view.is_consistent_for("B"))


class TestRegistrySingleton(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        set_frontier_registry(None)

    def test_singleton_returns_same_instance(self) -> None:
        log = LogClient(backend=MockBackend())
        r1 = get_frontier_registry(log)
        r2 = get_frontier_registry(log)
        self.assertIs(r1, r2)

    def test_set_replaces_singleton(self) -> None:
        log = LogClient(backend=MockBackend())
        custom = FrontierRegistry(log=log)
        set_frontier_registry(custom)
        r = get_frontier_registry(log)
        self.assertIs(r, custom)


if __name__ == "__main__":
    unittest.main()
