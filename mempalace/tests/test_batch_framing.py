"""Tests for Phase 1 — batch framing, idempotency, recovery."""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.log.recovery import (
    committed_frontier,
    emit_recovery_aborts,
    scan_for_orphans,
)
from mempalace.schema.events import (
    BatchAborted,
    BatchCommitted,
    BatchStarted,
    NodeCreated,
)
from mempalace.schema.identifiers import make_batch_id, make_theme_id
from mempalace.tests.conftest import reset_module_state


class TestBatchHandleHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_clean_batch_emits_started_events_committed(self) -> None:
        with self.log.batch("test.writer", expected_count=2) as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "B"},
            ))

        kinds = [e[1] for e in self.backend.read_range(0, self.backend.current_offset() + 1)]
        self.assertEqual(kinds, [
            "batch_started", "node_created", "node_created", "batch_committed",
        ])

    def test_all_events_in_batch_share_batch_id(self) -> None:
        with self.log.batch("test.writer", expected_count=2) as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "B"},
            ))

        events = list(self.backend.read_range(0, self.backend.current_offset() + 1))
        batch_ids = {e[2].get("batch_id", "") for e in events}
        # All four events should share one batch_id
        self.assertEqual(len(batch_ids), 1)
        self.assertTrue(next(iter(batch_ids)).startswith("bat_"))

    def test_output_index_increments(self) -> None:
        with self.log.batch("test.writer") as bh:
            r0 = bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
            r1 = bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "B"},
            ))
            r2 = bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "C"},
            ))
        self.assertEqual(r0.output_index, 0)
        self.assertEqual(r1.output_index, 1)
        self.assertEqual(r2.output_index, 2)

    def test_committed_event_has_actual_count(self) -> None:
        with self.log.batch("test.writer", expected_count=5) as bh:
            for _ in range(3):
                bh.append(NodeCreated(
                    node_id=make_theme_id(), node_kind="theme",
                    properties={"name": "X"},
                ))
        events = list(self.backend.read_range(0, self.backend.current_offset() + 1))
        commit = events[-1]
        self.assertEqual(commit[1], "batch_committed")
        # actual was 3, even though expected was 5
        self.assertEqual(commit[2]["actual_count"], 3)


class TestBatchHandleCrashPath(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_exception_inside_batch_emits_aborted(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.log.batch("test.crash", expected_count=3) as bh:
                bh.append(NodeCreated(
                    node_id=make_theme_id(), node_kind="theme",
                    properties={"name": "A"},
                ))
                raise RuntimeError("simulated mid-batch crash")

        events = list(self.backend.read_range(0, self.backend.current_offset() + 1))
        kinds = [e[1] for e in events]
        self.assertEqual(kinds, ["batch_started", "node_created", "batch_aborted"])
        abort = events[-1]
        self.assertEqual(abort[2]["reason"], "exception")
        self.assertEqual(abort[2]["partial_count"], 1)

    def test_explicit_abort_closes_cleanly(self) -> None:
        with self.log.batch("test.cancel") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
            bh.abort(reason="user_cancel", detail="user closed dialog")
            # Subsequent __exit__ is a no-op

        events = list(self.backend.read_range(0, self.backend.current_offset() + 1))
        kinds = [e[1] for e in events]
        self.assertEqual(kinds, ["batch_started", "node_created", "batch_aborted"])
        self.assertEqual(events[-1][2]["reason"], "user_cancel")

    def test_use_after_close_raises(self) -> None:
        # Open, close, then try to append → RuntimeError
        bh = self.log.batch("test.misuse")
        bh.__enter__()
        bh.__exit__(None, None, None)
        with self.assertRaises(RuntimeError):
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "X"},
            ))


class TestRecoveryScan(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_clean_log_has_no_open_batches(self) -> None:
        with self.log.batch("w") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 0)
        self.assertEqual(report.closed_batch_count, 1)

    def test_torn_batch_detected_with_correct_start_offset(self) -> None:
        # Simulate a torn batch by directly appending BatchStarted + events
        # without ever appending a close event.
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="torn.writer", expected_count=3, batch_id=bid,
        ))
        torn_start = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))

        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 1)
        ob = report.open_batches[0]
        self.assertEqual(ob.batch_id, bid)
        self.assertEqual(ob.consumer_id, "torn.writer")
        self.assertEqual(ob.start_offset, torn_start)

    def test_committed_frontier_rolls_back_for_torn_writer(self) -> None:
        # writer.A: 2 clean batches
        with self.log.batch("writer.A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A1"},
            ))
        with self.log.batch("writer.A") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A2"},
            ))
        # writer.B: torn batch
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="writer.B", expected_count=2, batch_id=bid,
        ))
        torn_start = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "B1"}, batch_id=bid,
        ))

        report = scan_for_orphans(self.log)
        # writer.A: no open → frontier = end_offset - 1 (i.e. log tail)
        self.assertEqual(report.committed_frontiers["writer.A"],
                         self.backend.current_offset())
        # writer.B: open at torn_start → frontier = torn_start - 1
        self.assertEqual(report.committed_frontiers["writer.B"], torn_start - 1)
        # Strict: B's frontier < A's frontier
        self.assertLess(report.committed_frontiers["writer.B"],
                        report.committed_frontiers["writer.A"])

    def test_emit_recovery_aborts_makes_log_self_consistent(self) -> None:
        # Create one torn batch
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="recover.me", expected_count=2, batch_id=bid,
        ))
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "X"}, batch_id=bid,
        ))

        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 1)

        n = emit_recovery_aborts(self.log, report)
        self.assertEqual(n, 1)

        # Re-scan: zero open
        report2 = scan_for_orphans(self.log)
        self.assertEqual(len(report2.open_batches), 0)
        # And the recovery abort carries the right reason
        last = list(self.log.read_range(
            self.backend.current_offset(), self.backend.current_offset() + 1,
        ))[0]
        self.assertEqual(last[1], "batch_aborted")
        self.assertEqual(last[2]["reason"], "recovery_orphan")

    def test_consumer_filter_restricts_open_batches(self) -> None:
        # Two torn batches, different consumers
        for consumer in ("alpha", "beta"):
            bid = make_batch_id()
            self.log.append(BatchStarted(
                consumer_id=consumer, expected_count=1, batch_id=bid,
            ))
            self.log.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "x"}, batch_id=bid,
            ))

        report_all = scan_for_orphans(self.log)
        self.assertEqual(len(report_all.open_batches), 2)

        report_alpha = scan_for_orphans(self.log, consumers=["alpha"])
        self.assertEqual(len(report_alpha.open_batches), 1)
        self.assertEqual(report_alpha.open_batches[0].consumer_id, "alpha")
        # And the frontier dict only mentions alpha
        self.assertIn("alpha", report_alpha.committed_frontiers)
        self.assertNotIn("beta", report_alpha.committed_frontiers)


class TestPartialUpdateRecognition(unittest.TestCase):
    """The keystone test: a torn batch is provably recognizable as such,
    distinguishable from a clean one. This is the property the original
    concern asked about."""

    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_clean_and_torn_batches_are_distinguishable(self) -> None:
        # Clean batch
        with self.log.batch("w.clean", expected_count=2) as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "X"},
            ))
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "Y"},
            ))

        # Torn batch (simulated)
        bid_torn = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="w.torn", expected_count=2, batch_id=bid_torn,
        ))
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "Z"}, batch_id=bid_torn,
        ))

        # Recovery scan returns exactly the torn batch
        report = scan_for_orphans(self.log)
        torn_ids = {ob.batch_id for ob in report.open_batches}
        self.assertEqual(len(torn_ids), 1)
        self.assertIn(bid_torn, torn_ids)

        # The clean batch's events do NOT appear as torn
        all_node_events = [
            (offset, kind, payload)
            for offset, kind, payload in self.log.read_range(
                0, self.backend.current_offset() + 1
            )
            if kind == "node_created"
        ]
        self.assertEqual(len(all_node_events), 3)
        # 2 are from the clean batch, 1 is from the torn batch
        clean_count = sum(
            1 for _, _, p in all_node_events
            if p.get("batch_id") not in torn_ids
        )
        torn_count = sum(
            1 for _, _, p in all_node_events
            if p.get("batch_id") in torn_ids
        )
        self.assertEqual(clean_count, 2)
        self.assertEqual(torn_count, 1)

    def test_post_recovery_frontier_distinguishes_consumers(self) -> None:
        """After recovery, a query for committed_frontier returns
        different values for clean vs previously-torn consumers. This
        is what downstream readers use to filter partial state."""
        # Clean writer
        with self.log.batch("clean") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))

        # Torn writer
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="messy", expected_count=1, batch_id=bid,
        ))
        torn_offset = self.backend.current_offset()
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "B"}, batch_id=bid,
        ))

        # Both consumers query their frontier (before recovery)
        clean_frontier = committed_frontier(self.log, "clean")
        messy_frontier = committed_frontier(self.log, "messy")

        # Clean: frontier at log tail
        self.assertEqual(clean_frontier, self.backend.current_offset())
        # Messy: frontier rolled back to before the open batch
        self.assertEqual(messy_frontier, torn_offset - 1)
        # Different
        self.assertNotEqual(clean_frontier, messy_frontier)


class TestAssertTripleBatching(unittest.TestCase):
    """End-to-end: the keystone multi-event writer (Graph.add_assertion)
    now emits a batch frame, and a simulated mid-write crash leaves a
    detectably-torn batch."""

    def setUp(self) -> None:
        from mempalace.tests.conftest import fresh_palace
        self.p = fresh_palace()

    def test_clean_add_assertion_is_a_committed_batch(self) -> None:
        graph = self.p["graph"]
        a = graph.create_entity(name="Alice")
        c = graph.create_entity(name="coffee")
        graph.add_assertion(
            subject_id=a, predicate="loves", object_id=c,
        )
        # Recovery sees zero open batches
        report = scan_for_orphans(self.p["log"])
        self.assertEqual(len(report.open_batches), 0)
        # The graph.add_assertion consumer has frontier = log tail
        # (no torn batches for that consumer)
        graph_frontier = report.committed_frontiers.get("graph.add_assertion")
        self.assertIsNotNone(graph_frontier)
        self.assertEqual(graph_frontier, self.p["backend"].current_offset())

    def test_add_assertion_events_share_batch_id(self) -> None:
        graph = self.p["graph"]
        a = graph.create_entity(name="A")
        b = graph.create_entity(name="B")
        graph.add_assertion(subject_id=a, predicate="knows", object_id=b)

        # Find the batch_started for graph.add_assertion
        events = list(self.p["log"].read_range(
            0, self.p["backend"].current_offset() + 1,
        ))
        bs_events = [
            (o, k, p) for o, k, p in events
            if k == "batch_started"
            and p.get("consumer_id") == "graph.add_assertion"
        ]
        self.assertEqual(len(bs_events), 1)
        bid = bs_events[0][2]["batch_id"]

        # Three events should carry that batch_id: 1 NodeCreated +
        # 2 EdgeCreated (no derived_from, so no extras)
        in_batch = [
            (o, k, p) for o, k, p in events
            if p.get("batch_id") == bid
            and k in ("node_created", "edge_created")
        ]
        self.assertEqual(len(in_batch), 3)
        kinds = [k for _, k, _ in in_batch]
        self.assertEqual(kinds.count("node_created"), 1)
        self.assertEqual(kinds.count("edge_created"), 2)

    def test_torn_add_assertion_simulation_is_detectable(self) -> None:
        """Simulate a crash inside add_assertion by manually opening a
        batch with the same consumer_id, emitting a partial event set,
        and skipping the close. This is what would happen if the
        process crashed mid-write."""
        from mempalace.schema.identifiers import make_assertion_id
        graph = self.p["graph"]
        a = graph.create_entity(name="Alice")
        c = graph.create_entity(name="coffee")
        # First, a clean add_assertion
        graph.add_assertion(subject_id=a, predicate="loves", object_id=c)

        # Now simulate a mid-write crash for a second assertion. We
        # open a batch but raise before all writes complete.
        torn_assertion_id = make_assertion_id()
        with self.assertRaises(RuntimeError):
            with self.p["log"].batch(
                "graph.add_assertion",
                expected_count=3,
            ) as bh:
                bh.append(NodeCreated(
                    node_id=torn_assertion_id,
                    node_kind="assertion",
                    properties={
                        "predicate": "hates",
                        "predicate_surface": "hates",
                        "confidence": 1.0,
                    },
                ))
                # crash before the edges are written
                raise RuntimeError("simulated mid-add_assertion crash")

        # The torn batch is now visible in the log via BatchAborted
        report = scan_for_orphans(self.p["log"])
        # No open batches because __exit__ emitted BatchAborted
        self.assertEqual(len(report.open_batches), 0)

        # But the partial assertion (1 NodeCreated, no edges) is still
        # in the log under the torn batch_id. Downstream consumers can
        # filter it by walking the log for batch_aborted events and
        # excluding their batch_ids.
        events = list(self.p["log"].read_range(
            0, self.p["backend"].current_offset() + 1,
        ))
        aborted_batch_ids = {
            e[2]["batch_id"] for e in events if e[1] == "batch_aborted"
        }
        self.assertEqual(len(aborted_batch_ids), 1)

        # Count assertion nodes: separated into clean vs torn by
        # checking whether each one's batch_id is in the aborted set.
        # The clean add_assertion call created 1 assertion node;
        # the torn simulation created 1 assertion node that landed
        # before the crash.
        clean_assertions = [
            e for e in events
            if e[1] == "node_created"
            and e[2].get("node_kind") == "assertion"
            and e[2].get("batch_id", "") not in aborted_batch_ids
        ]
        torn_assertions = [
            e for e in events
            if e[1] == "node_created"
            and e[2].get("node_kind") == "assertion"
            and e[2].get("batch_id", "") in aborted_batch_ids
        ]
        # Property: the torn assertion is in the log but recognizable
        # as torn. Downstream readers can drop it.
        self.assertEqual(len(clean_assertions), 1)
        self.assertEqual(len(torn_assertions), 1)
        # And the torn one carries the batch_id of an aborted batch.
        self.assertIn(torn_assertions[0][2]["batch_id"], aborted_batch_ids)


if __name__ == "__main__":
    unittest.main()
