"""
Tests for PHASE1 §J — sub-batch checkpointing.

Coverage:
  - BatchHandle.checkpoint() emits BatchCheckpointed under the right batch_id
  - checkpoint() raises if called before __enter__ or after close
  - Recovery scanner picks up BatchCheckpointed events
  - With a checkpoint, frontier rolls forward to the checkpoint, not back to BatchStarted-1
  - Multiple checkpoints — only the latest matters
  - Checkpoint without a corresponding open batch (out-of-window) is silently skipped
  - Closed batch with checkpoint events is ignored (already committed)
"""

from __future__ import annotations

import unittest

from mempalace.log.client import get_default_client
from mempalace.log.recovery import scan_for_orphans
from mempalace.schema.events import (
    BatchCheckpointed,
    BatchStarted,
    NodeCreated,
)
from mempalace.schema.identifiers import make_batch_id, make_event_id_log
from mempalace.tests.conftest import fresh_palace, reset_module_state


# ---------------------------------------------------------------------------
# BatchHandle.checkpoint()
# ---------------------------------------------------------------------------


class TestCheckpointEmission(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.log = get_default_client()

    def test_checkpoint_emits_event_with_batch_id(self) -> None:
        with self.log.batch(consumer_id="c", expected_count=2) as bh:
            bh.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id="n1", node_kind="entity", properties={},
            ))
            offset = bh.checkpoint(reason="periodic")
            self.assertGreater(offset, 0)
            bh.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id="n2", node_kind="entity", properties={},
            ))

        # Walk the log to confirm a BatchCheckpointed event was emitted
        # under the same batch_id as the surrounding events
        end = self.log.current_offset() + 1
        kinds = []
        batch_ids = []
        for o, kind, payload in self.log.read_range(1, end):
            kinds.append(kind)
            batch_ids.append(payload.get("batch_id", ""))
        self.assertIn("batch_checkpointed", kinds)
        # All non-empty batch_ids should match
        non_empty = [b for b in batch_ids if b]
        self.assertEqual(len(set(non_empty)), 1)

    def test_checkpoint_records_output_index_so_far(self) -> None:
        emitted_count = []
        with self.log.batch(consumer_id="c", expected_count=4) as bh:
            for i in range(3):
                bh.append(NodeCreated(
                    event_id=make_event_id_log(), recorded_at=1, actor="t",
                    node_id=f"n{i}", node_kind="entity", properties={},
                ))
            bh.checkpoint()
            emitted_count.append(bh.output_index)
            bh.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id="n3", node_kind="entity", properties={},
            ))

        # Find the checkpoint event in the log and check its output_index
        end = self.log.current_offset() + 1
        for o, kind, payload in self.log.read_range(1, end):
            if kind == "batch_checkpointed":
                self.assertEqual(payload["output_index_so_far"], 3)

    def test_checkpoint_before_enter_raises(self) -> None:
        # Construct via batch() but don't enter — call .checkpoint() directly
        # We have to access the BatchHandle without entering, which the
        # public API doesn't directly support. Use open_batch which returns
        # a handle without entering it.
        from mempalace.log.client import BatchHandle
        bh = BatchHandle(
            log=self.log, batch_id=make_batch_id(),
            consumer_id="c", expected_count=1,
            input_summary={}, actor="test",
        )
        with self.assertRaises(RuntimeError) as ctx:
            bh.checkpoint()
        self.assertIn("before __enter__", str(ctx.exception))

    def test_checkpoint_after_close_raises(self) -> None:
        with self.log.batch(consumer_id="c", expected_count=1) as bh:
            bh.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id="n1", node_kind="entity", properties={},
            ))
        # Now bh._closed should be True
        with self.assertRaises(RuntimeError) as ctx:
            bh.checkpoint()
        self.assertIn("after close", str(ctx.exception))


# ---------------------------------------------------------------------------
# Recovery scanner integration
# ---------------------------------------------------------------------------


def _emit_torn_batch_with_checkpoint(
    log,
    *,
    consumer_id: str,
    n_before_checkpoint: int,
    n_after_checkpoint: int,
    checkpoints: int = 1,
) -> str:
    """Helper: emit a batch with checkpoints, no commit/abort.
    Returns the batch_id."""
    bid = make_batch_id()
    log.append(BatchStarted(
        consumer_id=consumer_id,
        expected_count=n_before_checkpoint + n_after_checkpoint,
        batch_id=bid,
    ))
    out_idx = 0
    per_chunk = max(1, n_before_checkpoint // checkpoints)
    for chk in range(checkpoints):
        for i in range(per_chunk):
            log.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id=f"n_{chk}_{i}", node_kind="entity",
                properties={}, batch_id=bid,
            ))
            out_idx += 1
        log.append(BatchCheckpointed(
            consumer_id=consumer_id,
            output_index_so_far=out_idx,
            reason="periodic",
            batch_id=bid,
        ))
    # Trailing fragment after the last checkpoint
    for i in range(n_after_checkpoint):
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1, actor="t",
            node_id=f"n_trailing_{i}", node_kind="entity",
            properties={}, batch_id=bid,
        ))
    return bid


class TestRecoveryWithCheckpoint(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.log = get_default_client()

    def test_torn_batch_with_checkpoint_advances_frontier(self) -> None:
        _emit_torn_batch_with_checkpoint(
            self.log, consumer_id="c",
            n_before_checkpoint=3, n_after_checkpoint=2,
        )
        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 1)
        ob = report.open_batches[0]
        self.assertTrue(ob.has_checkpoint)
        self.assertEqual(ob.latest_checkpoint_output_index, 3)
        # The frontier should be at the checkpoint offset, not start-1
        self.assertGreater(ob.safe_frontier_offset, ob.start_offset)
        self.assertEqual(report.committed_frontiers["c"],
                         ob.latest_checkpoint_offset)

    def test_torn_batch_without_checkpoint_rolls_back(self) -> None:
        bid = make_batch_id()
        self.log.append(BatchStarted(
            consumer_id="c", expected_count=2, batch_id=bid,
        ))
        self.log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1, actor="t",
            node_id="n1", node_kind="entity", properties={}, batch_id=bid,
        ))
        report = scan_for_orphans(self.log)
        ob = report.open_batches[0]
        self.assertFalse(ob.has_checkpoint)
        # Frontier rolls back to before the batch started
        self.assertEqual(ob.safe_frontier_offset, ob.start_offset - 1)
        self.assertEqual(report.committed_frontiers["c"], ob.start_offset - 1)

    def test_multiple_checkpoints_use_latest(self) -> None:
        _emit_torn_batch_with_checkpoint(
            self.log, consumer_id="c",
            n_before_checkpoint=6, n_after_checkpoint=2,
            checkpoints=3,  # 3 checkpoints, each at +2 events
        )
        report = scan_for_orphans(self.log)
        ob = report.open_batches[0]
        # The latest checkpoint should be the third one — output_index = 6
        self.assertEqual(ob.latest_checkpoint_output_index, 6)

    def test_committed_batch_with_checkpoints_not_in_open(self) -> None:
        """A batch that has checkpoints AND a clean commit shouldn't
        show up as open."""
        with self.log.batch(consumer_id="c", expected_count=4) as bh:
            for i in range(2):
                bh.append(NodeCreated(
                    event_id=make_event_id_log(), recorded_at=1, actor="t",
                    node_id=f"n{i}", node_kind="entity", properties={},
                ))
            bh.checkpoint()
            for i in range(2, 4):
                bh.append(NodeCreated(
                    event_id=make_event_id_log(), recorded_at=1, actor="t",
                    node_id=f"n{i}", node_kind="entity", properties={},
                ))
        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 0)

    def test_checkpoint_without_open_batch_silently_skipped(self) -> None:
        """A BatchCheckpointed event whose BatchStarted is outside the
        scan window must not crash the scanner — it's simply ignored."""
        # Emit a checkpoint with a bogus batch_id not seen as Started
        self.log.append(BatchCheckpointed(
            consumer_id="c",
            output_index_so_far=5,
            reason="periodic",
            batch_id="bch_dangling",
        ))
        # Should not raise
        report = scan_for_orphans(self.log)
        self.assertEqual(len(report.open_batches), 0)

    def test_two_consumers_independent_frontiers(self) -> None:
        """Each consumer's frontier is computed independently — one
        consumer's torn batch with checkpoint shouldn't affect another."""
        # Consumer A: torn batch with checkpoint at output_index=2
        _emit_torn_batch_with_checkpoint(
            self.log, consumer_id="a",
            n_before_checkpoint=2, n_after_checkpoint=1,
        )
        # Consumer B: clean batch
        with self.log.batch(consumer_id="b", expected_count=1) as bh:
            bh.append(NodeCreated(
                event_id=make_event_id_log(), recorded_at=1, actor="t",
                node_id="n_b", node_kind="entity", properties={},
            ))

        report = scan_for_orphans(self.log)
        # Only a has open batches
        self.assertEqual(len(report.open_batches_for_consumer("a")), 1)
        self.assertEqual(len(report.open_batches_for_consumer("b")), 0)
        # a's frontier is at the checkpoint
        ob_a = report.open_batches_for_consumer("a")[0]
        self.assertEqual(report.committed_frontiers["a"],
                         ob_a.latest_checkpoint_offset)
        # b's frontier is at the end of log
        self.assertGreater(report.committed_frontiers["b"],
                           ob_a.latest_checkpoint_offset)


if __name__ == "__main__":
    unittest.main()
