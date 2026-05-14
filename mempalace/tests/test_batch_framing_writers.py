"""Tests for Phase 1 sub-slice 2 — every multi-event writer is
batch-framed and torn-batch-detectable.

Covers:
  - federate.sandbox lifecycle (provision → load → tear_down)
  - multiplex.jobs lifecycle (submit → started → completed/failed)
  - retrieve.handles lifecycle (allocate → refine → resolve → close)
  - migrate.converter run() as a batch
  - canonicalizer.check_promotions as a batch
"""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.log.recovery import scan_for_orphans
from mempalace.tests.conftest import fresh_palace, reset_module_state


def _events_with_batch_id(log: LogClient, backend: MockBackend, batch_id: str):
    """Return all events that carry the given batch_id."""
    return [
        (offset, kind, payload)
        for offset, kind, payload in log.read_range(0, backend.current_offset() + 1)
        if payload.get("batch_id") == batch_id
    ]


def _has_close_for(log: LogClient, backend: MockBackend, batch_id: str) -> tuple[bool, str]:
    """Return (closed, close_kind) where close_kind is 'committed', 'aborted', or ''."""
    for _, kind, payload in log.read_range(0, backend.current_offset() + 1):
        if payload.get("batch_id") == batch_id and kind in ("batch_committed", "batch_aborted"):
            return True, kind
    return False, ""


# =============================================================================
# Sandbox lifecycle
# =============================================================================


class TestSandboxBatchFraming(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        from mempalace.federate.sandbox import SandboxManager
        from mempalace.log.client import LogClient, MockBackend
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.mgr = SandboxManager(log_client=self.log)

    def test_provision_load_teardown_clean_lifecycle(self) -> None:
        state = self.mgr.provision(
            foreign_palace_pubkey="peer_alpha", match_request_id="m_1",
        )
        sbx_id = state.sandbox_id
        # All sandbox events should carry batch_id == sandbox_id
        provisioned_events = _events_with_batch_id(self.log, self.backend, sbx_id)
        self.assertGreaterEqual(len(provisioned_events), 1)
        kinds = [e[1] for e in provisioned_events]
        self.assertIn("batch_started", kinds)
        self.assertIn("sandbox_provisioned", kinds)

        # Load slice
        self.mgr.load_foreign_slice(
            sbx_id,
            slice_blob=b"x" * 100,
            slice_signature=b"sig",
            slice_summary={"layer": 1},
        )
        # Tear down (clean → BatchCommitted)
        ok = self.mgr.tear_down(sbx_id, reason="completed")
        self.assertTrue(ok)
        closed, close_kind = _has_close_for(self.log, self.backend, sbx_id)
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_committed")

        # Recovery sees no orphans
        report = scan_for_orphans(self.log)
        self.assertEqual(report.open_batches, [])

    def test_teardown_with_failure_reason_emits_aborted(self) -> None:
        state = self.mgr.provision(foreign_palace_pubkey="peer_x")
        sbx_id = state.sandbox_id
        self.mgr.tear_down(sbx_id, reason="match_failed")
        _, close_kind = _has_close_for(self.log, self.backend, sbx_id)
        self.assertEqual(close_kind, "batch_aborted")

    def test_torn_sandbox_recovery_detection(self) -> None:
        # Provision two sandboxes; tear down one cleanly, leave the other open
        state_clean = self.mgr.provision(foreign_palace_pubkey="peer_a")
        state_torn = self.mgr.provision(foreign_palace_pubkey="peer_b")
        self.mgr.tear_down(state_clean.sandbox_id, reason="completed")
        # state_torn deliberately not torn down — simulate a crash

        report = scan_for_orphans(self.log)
        open_ids = {ob.batch_id for ob in report.open_batches}
        self.assertIn(state_torn.sandbox_id, open_ids)
        self.assertNotIn(state_clean.sandbox_id, open_ids)


# =============================================================================
# Jobs lifecycle
# =============================================================================


class TestJobsBatchFraming(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        from mempalace.log.client import LogClient, MockBackend
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_complete_job_lifecycle_emits_committed(self) -> None:
        from mempalace.multiplex.jobs import (
            JobKind, mark_completed, mark_started, submit_job,
        )

        job = submit_job(
            kind=JobKind.RETRIEVE,
            consumer="test",
            log=self.log,
        )
        mark_started(job, log=self.log)
        mark_completed(job, outputs={"result": "ok"}, log=self.log)

        closed, close_kind = _has_close_for(self.log, self.backend, job.job_id)
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_committed")

        # All job events carry batch_id == job_id
        job_events = _events_with_batch_id(self.log, self.backend, job.job_id)
        kinds = [e[1] for e in job_events]
        self.assertIn("job_scheduled", kinds)
        self.assertIn("job_started", kinds)
        self.assertIn("job_completed", kinds)
        self.assertIn("batch_started", kinds)
        self.assertIn("batch_committed", kinds)

    def test_failed_job_emits_aborted(self) -> None:
        from mempalace.multiplex.jobs import (
            JobKind, mark_failed, submit_job,
        )

        job = submit_job(
            kind=JobKind.RETRIEVE,
            consumer="test",
            log=self.log,
        )
        mark_failed(job, error_kind="timeout", error_message="oops", log=self.log)
        closed, close_kind = _has_close_for(self.log, self.backend, job.job_id)
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_aborted")

    def test_torn_job_recovery_detection(self) -> None:
        from mempalace.multiplex.jobs import JobKind, mark_started, submit_job

        # Submit + start, never complete → torn
        job = submit_job(
            kind=JobKind.MINER_CLASS1,
            consumer="miner",
            log=self.log,
        )
        mark_started(job, log=self.log)
        # Crash before complete

        report = scan_for_orphans(self.log)
        open_ids = {ob.batch_id for ob in report.open_batches}
        self.assertIn(job.job_id, open_ids)


# =============================================================================
# Handles lifecycle
# =============================================================================


class TestHandlesBatchFraming(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        from mempalace.log.client import LogClient, MockBackend
        from mempalace.retrieve.handle import HandleManager
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)
        self.mgr = HandleManager(log_client=self.log)

    def test_allocate_close_clean_lifecycle(self) -> None:
        from mempalace.retrieve.gather import Scope
        from mempalace.schema.stance import Stance

        scope = Scope()
        stance = Stance()
        hid = self.mgr.allocate(scope, stance, consumer_id="agent.test")
        self.mgr.close(hid)

        closed, close_kind = _has_close_for(self.log, self.backend, hid)
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_committed")

        events = _events_with_batch_id(self.log, self.backend, hid)
        kinds = [e[1] for e in events]
        self.assertIn("handle_allocated", kinds)
        self.assertIn("handle_closed", kinds)

    def test_torn_handle_recovery_detection(self) -> None:
        from mempalace.retrieve.gather import Scope
        from mempalace.schema.stance import Stance

        # Allocate two handles, close one, leave one open
        clean_hid = self.mgr.allocate(Scope(), Stance(), consumer_id="ag.A")
        torn_hid = self.mgr.allocate(Scope(), Stance(), consumer_id="ag.B")
        self.mgr.close(clean_hid)

        report = scan_for_orphans(self.log)
        open_ids = {ob.batch_id for ob in report.open_batches}
        self.assertIn(torn_hid, open_ids)
        self.assertNotIn(clean_hid, open_ids)


# =============================================================================
# Migrate converter
# =============================================================================


class TestMigrateBatchFraming(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()

    def test_migration_run_is_a_batch(self) -> None:
        from mempalace.migrate import (
            Converter, LegacyDrawer, LegacyTheme,
        )

        conv = Converter(log=self.p["log"])
        # Tracking offsets so we know what came from this run
        before_offset = self.p["backend"].current_offset()
        report = conv.run(
            themes=[LegacyTheme(theme_id="t1", name="Test")],
            drawers=[
                LegacyDrawer(
                    drawer_id=f"d{i}", content=f"content{i}",
                    created_at_ms=1_000_000 + i,
                )
                for i in range(3)
            ],
        )

        # Find the migrate.converter batch
        events = list(self.p["log"].read_range(
            before_offset, self.p["backend"].current_offset() + 1,
        ))
        bs_events = [
            (o, k, p) for o, k, p in events
            if k == "batch_started" and p.get("consumer_id") == "migrate.converter"
        ]
        self.assertEqual(len(bs_events), 1)
        bid = bs_events[0][2]["batch_id"]

        # The batch is closed
        closed, close_kind = _has_close_for(
            self.p["log"], self.p["backend"], bid,
        )
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_committed")

        # Drawer events from the migration carry the migration batch_id
        # (they bypass Graph helpers; they're synth_drawer_event direct
        # appends with the batch_id stamped manually).
        drawer_events = [
            (o, k, p) for o, k, p in events
            if k == "drawer_captured" and p.get("batch_id") == bid
        ]
        self.assertEqual(len(drawer_events), 3)


# =============================================================================
# Canonicalizer check_promotions
# =============================================================================


class TestCanonPromotionBatchFraming(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        from mempalace.canonicalizer import Canonicalizer
        from mempalace.log.client import LogClient, MockBackend
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

        # Deterministic embedder so cluster promotion is reliable
        def embed(s: str) -> list[float]:
            v = [0.0] * 16
            for i, ch in enumerate(s.lower()[:16]):
                v[i] = (ord(ch) % 17) / 17.0
            n = sum(x * x for x in v) ** 0.5
            return [x / n for x in v] if n > 0 else v

        self.can = Canonicalizer(
            embedder=embed,
            promotion_min_members=3,
            promotion_min_passes=2,
            log=self.log,
        )

    def test_check_promotions_with_promotion_emits_batch(self) -> None:
        from mempalace.canonicalizer import CanonDomain

        # Drive a cluster to promotion threshold
        for surface, pid in [
            ("monday meal prep", "p1"),
            ("monday meal session", "p1"),
            ("monday meal cooking", "p2"),
            ("monday meal saturday", "p2"),
        ]:
            self.can.resolve(CanonDomain.THEME_NAMES, surface, pass_id=pid)

        before = self.backend.current_offset()
        promoted = self.can.check_promotions(CanonDomain.THEME_NAMES)
        self.assertGreaterEqual(len(promoted), 1)

        # Check_promotions emitted a batch
        events = list(self.log.read_range(before, self.backend.current_offset() + 1))
        bs_events = [
            (o, k, p) for o, k, p in events
            if k == "batch_started"
            and p.get("consumer_id") == "canonicalizer.check_promotions"
        ]
        self.assertEqual(len(bs_events), 1)
        bid = bs_events[0][2]["batch_id"]

        # Promoted events carry the batch_id
        promoted_events = [
            (o, k, p) for o, k, p in events
            if k == "canonical_promoted" and p.get("batch_id") == bid
        ]
        self.assertEqual(len(promoted_events), len(promoted))

        # Batch is closed
        closed, close_kind = _has_close_for(self.log, self.backend, bid)
        self.assertTrue(closed)
        self.assertEqual(close_kind, "batch_committed")

    def test_check_promotions_with_no_promotion_emits_no_batch(self) -> None:
        from mempalace.canonicalizer import CanonDomain

        # Add only 1 surface — won't meet min_members=3
        self.can.resolve(CanonDomain.THEME_NAMES, "lonely surface", pass_id="p1")

        before = self.backend.current_offset()
        promoted = self.can.check_promotions(CanonDomain.THEME_NAMES)
        self.assertEqual(promoted, [])

        # No batch_started emitted (we open the batch only when we
        # have something to promote)
        events = list(self.log.read_range(before, self.backend.current_offset() + 1))
        bs_events = [
            (o, k, p) for o, k, p in events
            if k == "batch_started"
            and p.get("consumer_id") == "canonicalizer.check_promotions"
        ]
        self.assertEqual(len(bs_events), 0)


# =============================================================================
# End-to-end: a torn process across multiple writers leaves all of them
# detectable
# =============================================================================


class TestEndToEndRecovery(unittest.TestCase):
    """Multiple writers all open batches; some close cleanly, some don't.
    Recovery distinguishes them all correctly."""

    def setUp(self) -> None:
        reset_module_state()
        from mempalace.log.client import LogClient, MockBackend
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_multiple_torn_writers_simultaneously(self) -> None:
        from mempalace.federate.sandbox import SandboxManager
        from mempalace.multiplex.jobs import JobKind, mark_completed, submit_job
        from mempalace.retrieve.gather import Scope
        from mempalace.retrieve.handle import HandleManager
        from mempalace.schema.stance import Stance

        sandbox_mgr = SandboxManager(log_client=self.log)
        handle_mgr = HandleManager(log_client=self.log)

        # Clean: complete a job
        clean_job = submit_job(
            kind=JobKind.RETRIEVE, consumer="test", log=self.log,
        )
        mark_completed(clean_job, outputs={}, log=self.log)

        # Torn: provision a sandbox, never tear down
        torn_sbx = sandbox_mgr.provision(foreign_palace_pubkey="peer_x")

        # Torn: allocate a handle, never close
        torn_hid = handle_mgr.allocate(Scope(), Stance(), consumer_id="agent")

        # Torn: submit a job, never complete
        torn_job = submit_job(
            kind=JobKind.MINER_CLASS1, consumer="miner", log=self.log,
        )

        report = scan_for_orphans(self.log)
        open_batch_ids = {ob.batch_id for ob in report.open_batches}

        # Three torn writers, three open batches, in that order
        self.assertEqual(len(open_batch_ids), 3)
        self.assertIn(torn_sbx.sandbox_id, open_batch_ids)
        self.assertIn(torn_hid, open_batch_ids)
        self.assertIn(torn_job.job_id, open_batch_ids)

        # Each consumer has its own committed_frontier
        self.assertIn("federate.sandbox", report.committed_frontiers)
        self.assertIn("retrieve.handles", report.committed_frontiers)
        self.assertIn("multiplex.jobs", report.committed_frontiers)


if __name__ == "__main__":
    unittest.main()
