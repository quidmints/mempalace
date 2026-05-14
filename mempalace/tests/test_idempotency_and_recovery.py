"""Tests for Phase 1 sub-slice 3 — idempotency keys + recovery hook.

Covers:
  - ProposalStore idempotency-key dedup on retry
  - ProposalStore quarantine_torn_batches excludes from default reads
  - SignatureStore idempotency-key dedup
  - SignatureStore quarantine
  - Process-startup recovery hook composes scan + abort + quarantine
"""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.log.recovery_hook import run_recovery_on_startup
from mempalace.miner.proposals import ProposalStore
from mempalace.signatures.store import SignatureStore
from mempalace.tests.conftest import reset_module_state


def _stub_proposal(pid: str, kind: str = "memory_type"):
    """Build a minimal ProposalRecord for tests."""
    from mempalace.miner.base import ProposalLifecycle, ProposalRecord
    return ProposalRecord(
        proposal_id=pid,
        proposal_kind=kind,
        proposed_value={"x": pid},
        confidence=0.8,
        miner_class=1,
        lifecycle=ProposalLifecycle.PROVISIONAL,
    )


def _stub_snapshot(snap_id: str, period_id: str, captured_at_ms: int):
    from mempalace.signatures.store import SignatureSnapshot
    return SignatureSnapshot(
        snapshot_id=snap_id,
        period_id=period_id,
        captured_at_ms=captured_at_ms,
    )


class TestProposalStoreIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.store = ProposalStore()

    def test_add_without_key_legacy_path(self) -> None:
        r = self.store.add(_stub_proposal("pid1"))
        self.assertFalse(r.deduplicated)
        self.assertEqual(self.store.size(), 1)
        # Same proposal_id again → corroboration count rises
        r2 = self.store.add(_stub_proposal("pid1"))
        self.assertFalse(r2.deduplicated)
        self.assertEqual(r2.entry.corroboration_count, 2)

    def test_add_with_key_dedups_retry(self) -> None:
        key = ("miner.class1", "bat_x", 0)
        r = self.store.add(_stub_proposal("pid1"), idempotency_key=key)
        self.assertFalse(r.deduplicated)
        self.assertEqual(self.store.size(), 1)
        # Retry with same key → dedup, corroboration count NOT incremented
        r2 = self.store.add(_stub_proposal("pid1"), idempotency_key=key)
        self.assertTrue(r2.deduplicated)
        self.assertEqual(r2.entry.corroboration_count, 1)

    def test_different_keys_treated_as_independent(self) -> None:
        # Two miner runs on different inputs that produce the same logical
        # proposal — the second is a real corroboration, not a retry
        key1 = ("miner.class1", "bat_a", 0)
        key2 = ("miner.class1", "bat_b", 0)
        self.store.add(_stub_proposal("pid1"), idempotency_key=key1)
        r2 = self.store.add(_stub_proposal("pid1"), idempotency_key=key2)
        self.assertFalse(r2.deduplicated)
        self.assertEqual(r2.entry.corroboration_count, 2)

    def test_add_batch_with_consumer_and_batch(self) -> None:
        records = [_stub_proposal(f"pid{i}") for i in range(5)]
        results = self.store.add_batch(
            records, consumer_id="miner.class1", batch_id="bat_0",
        )
        self.assertEqual(len(results), 5)
        # Idempotency keys auto-derived from positions
        self.assertEqual(
            results[0].entry.idempotency_key,
            ("miner.class1", "bat_0", 0),
        )
        self.assertEqual(
            results[4].entry.idempotency_key,
            ("miner.class1", "bat_0", 4),
        )

    def test_quarantine_excludes_from_default_reads(self) -> None:
        key1 = ("miner", "bat_clean", 0)
        key2 = ("miner", "bat_torn", 0)
        self.store.add(_stub_proposal("clean1"), idempotency_key=key1)
        self.store.add(_stub_proposal("torn1"), idempotency_key=key2)

        # Both visible
        self.assertEqual(self.store.size(), 2)
        self.assertEqual(len(self.store.all()), 2)

        # Quarantine the torn batch
        n = self.store.quarantine_torn_batches({"bat_torn"})
        self.assertEqual(n, 1)

        # Default reads exclude
        self.assertEqual(self.store.size(), 1)
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.all()[0].record.proposal_id, "clean1")

        # Diagnostic API still includes torn entries
        self.assertEqual(self.store.size_including_torn(), 2)
        self.assertEqual(len(self.store.all_including_torn()), 2)

    def test_get_returns_torn_entries_for_explicit_lookup(self) -> None:
        # `get()` is an explicit lookup by id; the caller asked for it
        # specifically, so we return it (with the torn marker visible
        # in the entry). Default *list* reads filter; explicit gets
        # don't.
        key = ("miner", "bat_torn", 0)
        self.store.add(_stub_proposal("torn1"), idempotency_key=key)
        self.store.quarantine_torn_batches({"bat_torn"})
        e = self.store.get("torn1")
        self.assertIsNotNone(e)
        self.assertTrue(e.from_torn_batch)


class TestSignatureStoreIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.store = SignatureStore()

    def test_put_without_key_legacy_path(self) -> None:
        ok = self.store.put(_stub_snapshot("s1", "p1", 1000))
        self.assertTrue(ok)
        self.assertEqual(self.store.size(), 1)

    def test_put_with_key_dedups_retry(self) -> None:
        key = ("signatures.snapshotter", "bat_y", 0)
        ok1 = self.store.put(_stub_snapshot("s1", "p1", 1000), idempotency_key=key)
        ok2 = self.store.put(_stub_snapshot("s1", "p1", 1000), idempotency_key=key)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(self.store.size(), 1)

    def test_quarantine_excludes_from_chronological(self) -> None:
        self.store.put(
            _stub_snapshot("clean", "p_clean", 1000),
            idempotency_key=("snap", "bat_clean", 0),
        )
        self.store.put(
            _stub_snapshot("torn", "p_torn", 2000),
            idempotency_key=("snap", "bat_torn", 0),
        )
        self.assertEqual(self.store.size(), 2)
        self.store.quarantine_torn_batches({"bat_torn"})
        self.assertEqual(self.store.size(), 1)
        self.assertEqual(len(self.store.chronological()), 1)
        # Diagnostic includes torn
        self.assertEqual(len(self.store.chronological_including_torn()), 2)

    def test_get_filters_torn_period(self) -> None:
        self.store.put(
            _stub_snapshot("torn", "p_x", 1000),
            idempotency_key=("snap", "bat_torn", 0),
        )
        self.assertIsNotNone(self.store.get("p_x"))
        self.store.quarantine_torn_batches({"bat_torn"})
        self.assertIsNone(self.store.get("p_x"))


class TestRecoveryHook(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_clean_log_returns_zero_torn(self) -> None:
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import make_theme_id

        with self.log.batch("clean") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "X"},
            ))
        result = run_recovery_on_startup(self.log)
        self.assertFalse(result.has_torn_batches)
        self.assertEqual(result.aborts_emitted, 0)

    def test_torn_batch_quarantines_proposals(self) -> None:
        from mempalace.schema.events import BatchStarted, NodeCreated
        from mempalace.schema.identifiers import make_batch_id, make_theme_id

        # Set up: stores hold entries from both clean and torn batches
        proposals = ProposalStore()
        # Clean
        proposals.add(
            _stub_proposal("clean1"),
            idempotency_key=("miner", "bat_clean", 0),
        )
        # Torn — the batch_id matches what the log says is open
        torn_bid = make_batch_id()
        proposals.add(
            _stub_proposal("torn1"),
            idempotency_key=("miner", torn_bid, 0),
        )

        # Log: open batch with that id, no close
        self.log.append(BatchStarted(
            consumer_id="miner", expected_count=1, batch_id=torn_bid,
        ))
        self.log.append(NodeCreated(
            node_id=make_theme_id(), node_kind="theme",
            properties={"name": "T"}, batch_id=torn_bid,
        ))

        self.assertEqual(proposals.size(), 2)
        result = run_recovery_on_startup(
            self.log, stores={"proposals": proposals},
        )

        self.assertTrue(result.has_torn_batches)
        self.assertEqual(result.aborts_emitted, 1)
        self.assertEqual(result.quarantine_counts["proposals"], 1)
        # After quarantine, only the clean one is visible
        self.assertEqual(proposals.size(), 1)
        self.assertEqual(proposals.all()[0].record.proposal_id, "clean1")

    def test_hook_idempotent_on_second_run(self) -> None:
        from mempalace.schema.events import BatchStarted
        from mempalace.schema.identifiers import make_batch_id

        torn_bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="x", expected_count=1, batch_id=torn_bid,
        ))

        # First run: emits abort, quarantines, sets frontier
        result1 = run_recovery_on_startup(self.log)
        self.assertEqual(result1.aborts_emitted, 1)

        # Second run: log is already self-consistent
        result2 = run_recovery_on_startup(self.log)
        self.assertFalse(result2.has_torn_batches)
        self.assertEqual(result2.aborts_emitted, 0)

    def test_hook_returns_per_consumer_committed_frontier(self) -> None:
        from mempalace.schema.events import BatchStarted, NodeCreated
        from mempalace.schema.identifiers import make_batch_id, make_theme_id

        # Clean writer
        with self.log.batch("clean") as bh:
            bh.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": "A"},
            ))
        # Torn writer
        torn_bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="messy", expected_count=1, batch_id=torn_bid,
        ))
        torn_offset = self.backend.current_offset()

        result = run_recovery_on_startup(self.log)

        # Both consumers have a frontier
        self.assertIsNotNone(result.committed_frontier_for("clean"))
        self.assertIsNotNone(result.committed_frontier_for("messy"))
        # messy < clean
        self.assertLess(
            result.committed_frontier_for("messy"),
            result.committed_frontier_for("clean"),
        )
        # messy's frontier rolled back to before the open batch
        self.assertEqual(
            result.committed_frontier_for("messy"),
            torn_offset - 1,
        )


class TestEndToEndPartialUpdateRecovery(unittest.TestCase):
    """Closes the loop on the partial-update concern: a torn miner
    pass leaves quarantined proposals; downstream rankers reading the
    store don't see them; recovery hook runs once and the system is
    in a known-clean state."""

    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.log = LogClient(backend=self.backend)

    def test_torn_miner_run_does_not_pollute_downstream_reads(self) -> None:
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import make_theme_id

        proposals = ProposalStore()

        # 1. Clean miner run: 3 proposals
        with self.log.batch("miner.class1", expected_count=3) as bh:
            for i in range(3):
                bh.append(NodeCreated(
                    node_id=make_theme_id(), node_kind="theme",
                    properties={"name": f"clean_{i}"},
                ))
                proposals.add(
                    _stub_proposal(f"clean_{i}"),
                    idempotency_key=("miner.class1", bh.batch_id, i),
                )

        # 2. Torn miner run: emitted 2 of 5 expected proposals before crash
        try:
            with self.log.batch("miner.class1", expected_count=5) as bh:
                for i in range(2):
                    bh.append(NodeCreated(
                        node_id=make_theme_id(), node_kind="theme",
                        properties={"name": f"torn_{i}"},
                    ))
                    proposals.add(
                        _stub_proposal(f"torn_{i}"),
                        idempotency_key=("miner.class1", bh.batch_id, i),
                    )
                raise RuntimeError("simulated crash mid-miner")
        except RuntimeError:
            pass

        # Pre-recovery: store has all 5 entries. Downstream readers
        # would see all 5, including the torn ones (this is the bug
        # we're fixing).
        self.assertEqual(proposals.size_including_torn(), 5)

        # 3. Run recovery hook
        # Note: the BatchAborted from the `with` block already closed
        # the torn batch in the log. So scan_for_orphans returns 0
        # open batches — the in-context exception path wrote the abort.
        # We need a different test path for "process crashed BEFORE
        # the abort emission" — that's the next test below.

        # For this test, we manually invoke the quarantine to simulate
        # the case where the recovery-hook integration learns about
        # already-aborted batches and still wants to quarantine their
        # outputs.

        # Find the aborted batch_id
        events = list(self.log.read_range(0, self.backend.current_offset() + 1))
        aborted_bids = {
            e[2]["batch_id"] for e in events if e[1] == "batch_aborted"
        }
        proposals.quarantine_torn_batches(aborted_bids)

        # Default reads now exclude the torn proposals
        self.assertEqual(proposals.size(), 3)
        clean_ids = {e.record.proposal_id for e in proposals.all()}
        self.assertEqual(clean_ids, {"clean_0", "clean_1", "clean_2"})

    def test_process_crash_before_abort_emission(self) -> None:
        """The harder case: process dies between event-write and
        abort emission. Recovery scan finds an orphan; the hook
        emits the abort and quarantines the partial outputs."""
        from mempalace.schema.events import BatchStarted, NodeCreated
        from mempalace.schema.identifiers import make_batch_id, make_theme_id

        proposals = ProposalStore()

        # Manually simulate "process crashed between writes":
        # BatchStarted + 2 events, no close.
        torn_bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="miner.class1", expected_count=5, batch_id=torn_bid,
        ))
        for i in range(2):
            self.log.append(NodeCreated(
                node_id=make_theme_id(), node_kind="theme",
                properties={"name": f"torn_{i}"}, batch_id=torn_bid,
            ))
            proposals.add(
                _stub_proposal(f"torn_{i}"),
                idempotency_key=("miner.class1", torn_bid, i),
            )

        # Pre-recovery: store has 2 entries; log has 1 open batch
        self.assertEqual(proposals.size_including_torn(), 2)

        # Recovery hook runs
        result = run_recovery_on_startup(
            self.log, stores={"proposals": proposals},
        )

        self.assertTrue(result.has_torn_batches)
        self.assertEqual(result.aborts_emitted, 1)
        self.assertEqual(result.quarantine_counts["proposals"], 2)

        # Default reads now show empty (both entries quarantined)
        self.assertEqual(proposals.size(), 0)
        self.assertEqual(proposals.size_including_torn(), 2)

        # Resume token: miner.class1's frontier rolled back to before
        # the open batch. The miner can re-run from there with a fresh
        # batch_id, and the new outputs go into the store with new
        # idempotency keys. No collision.
        frontier = result.committed_frontier_for("miner.class1")
        self.assertIsNotNone(frontier)


if __name__ == "__main__":
    unittest.main()
