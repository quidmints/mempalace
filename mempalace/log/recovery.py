"""
Log recovery — scan for torn batches.

Per Phase 1 design: on consumer startup, scan the log for
`BatchStarted` events that have no matching `BatchCommitted` or
`BatchAborted` carrying the same `batch_id`. Those are torn —
the writer crashed mid-batch. The recovery protocol:

  1. Identify open batches.
  2. For each: emit `BatchAborted(reason="recovery_orphan")` so the log
     becomes self-consistent (no infinite open batches).
  3. Compute `committed_frontier_offset` per consumer = highest log
     offset where every batch with first event ≤ this offset has been
     closed (committed or aborted, including just-now-aborted orphans).

This module owns step (1) and (3); step (2) is up to the caller (we
don't auto-emit because the policy choice — quarantine vs discard
of partial outputs — lives with each writer's store).

Spec ref: PHASE1_DESIGN.md §D.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .client import LogClient


@dataclass
class OpenBatch:
    """A `BatchStarted` event with no matching close.

    `start_offset` is the log offset of the BatchStarted event. The
    consumer's recovery routine uses it to roll its frontier back to
    `start_offset - 1`, which is the largest "guaranteed clean" offset.

    `latest_checkpoint_offset` is the log offset of the most recent
    `BatchCheckpointed` for this batch (PHASE1 §J — sub-batch
    checkpointing). When non-zero, the consumer can roll its frontier
    forward to this offset instead of all the way back to
    `start_offset - 1`, saving replay work for long batches.

    `latest_checkpoint_output_index` is the value of
    `output_index_so_far` at that checkpoint — the count of events
    in this batch that are durable.
    """
    batch_id: str
    consumer_id: str
    start_offset: int
    expected_count: int
    input_summary: dict[str, Any] = field(default_factory=dict)
    latest_checkpoint_offset: int = 0
    latest_checkpoint_output_index: int = 0

    @property
    def has_checkpoint(self) -> bool:
        return self.latest_checkpoint_offset > 0

    @property
    def safe_frontier_offset(self) -> int:
        """The highest log offset for this batch that recovery can
        treat as durable. Equals the latest checkpoint when one
        exists; otherwise `start_offset - 1` (rollback to before
        the batch started)."""
        if self.has_checkpoint:
            return self.latest_checkpoint_offset
        return self.start_offset - 1


@dataclass
class RecoveryReport:
    """Result of a recovery scan."""

    open_batches: list[OpenBatch] = field(default_factory=list)
    closed_batch_count: int = 0
    scanned_offsets: int = 0
    # Per-consumer committed_frontier_offset.
    # Empty dict means no batches seen for that consumer (frontier =
    # log's current_offset for that consumer, no rollback needed).
    committed_frontiers: dict[str, int] = field(default_factory=dict)

    @property
    def has_torn_batches(self) -> bool:
        return bool(self.open_batches)

    def open_batches_for_consumer(self, consumer_id: str) -> list[OpenBatch]:
        return [b for b in self.open_batches if b.consumer_id == consumer_id]


def scan_for_orphans(
    log: LogClient,
    *,
    consumers: Iterable[str] | None = None,
    start_offset: int = 1,
    end_offset: int | None = None,
) -> RecoveryReport:
    """Scan the log between [start_offset, end_offset] for torn batches.

    Args:
      log: LogClient to scan.
      consumers: if given, only report orphans for these consumer_ids.
        If None, report all open batches regardless of consumer.
      start_offset: inclusive lower bound. Default 1 (whole log).
      end_offset: exclusive upper bound. Default = log.current_offset() + 1.

    Returns:
      RecoveryReport with open_batches, closed_batch_count, scanned
      offset count, and per-consumer committed_frontier_offset.
    """
    if end_offset is None:
        end_offset = log.current_offset() + 1

    consumer_filter = set(consumers) if consumers is not None else None

    # Map of batch_id → (consumer_id, start_offset, payload)
    open_starts: dict[str, OpenBatch] = {}
    # Per-consumer: highest offset of any closed batch's start, AND
    # highest offset of any non-batch event seen.
    # We compute committed_frontier_offset as: the largest offset such
    # that no open batch has start_offset ≤ this offset.
    consumers_seen: set[str] = set()
    consumer_lowest_open_start: dict[str, int] = {}
    closed = 0
    scanned = 0

    for offset, kind, payload in log.read_range(start_offset, end_offset):
        scanned += 1

        if kind == "batch_started":
            cid = payload.get("consumer_id", "")
            consumers_seen.add(cid)
            if consumer_filter is not None and cid not in consumer_filter:
                continue
            bid = payload.get("batch_id", "")
            if not bid:
                # Malformed BatchStarted — skip silently; the validator
                # would have rejected it, but we read what's there.
                continue
            open_starts[bid] = OpenBatch(
                batch_id=bid,
                consumer_id=cid,
                start_offset=offset,
                expected_count=int(payload.get("expected_count", 0)),
                input_summary=dict(payload.get("input_summary", {})),
            )

        elif kind in ("batch_committed", "batch_aborted"):
            bid = payload.get("batch_id", "")
            if bid in open_starts:
                del open_starts[bid]
                closed += 1
            # else: close-without-open — possible if scan range starts
            # mid-history. Silently skip; caller can re-scan from 1.

        elif kind == "batch_checkpointed":
            # PHASE1 §J — track the latest checkpoint per open batch.
            # Frontier-advancement happens below when computing
            # committed_frontiers; here we just record per-batch state.
            bid = payload.get("batch_id", "")
            if bid in open_starts:
                ob = open_starts[bid]
                ob.latest_checkpoint_offset = offset
                ob.latest_checkpoint_output_index = int(
                    payload.get("output_index_so_far", 0),
                )
            # else: checkpoint-without-open — same logic as
            # close-without-open: the BatchStarted is outside our
            # scan window. Silently skip.

    # Track per-consumer lowest-safe-frontier across all open batches.
    # When checkpoints exist, the safe frontier is the latest checkpoint;
    # otherwise it's start_offset - 1.
    for ob in open_starts.values():
        prev = consumer_lowest_open_start.get(ob.consumer_id)
        # Use safe_frontier_offset+1 so the comparison key matches
        # what the old "lowest start_offset" represented (the offset
        # AT WHICH things become uncertain).
        uncertain_at = ob.safe_frontier_offset + 1
        if prev is None or uncertain_at < prev:
            consumer_lowest_open_start[ob.consumer_id] = uncertain_at

    # committed_frontier_offset per consumer:
    #   - If no open batch for consumer: frontier = end_offset - 1 (everything seen)
    #   - If open batch at offset N: frontier = N - 1 (everything strictly before)
    committed_frontiers: dict[str, int] = {}
    for cid in consumers_seen:
        if consumer_filter is not None and cid not in consumer_filter:
            continue
        lowest = consumer_lowest_open_start.get(cid)
        if lowest is None:
            committed_frontiers[cid] = end_offset - 1
        else:
            committed_frontiers[cid] = lowest - 1

    return RecoveryReport(
        open_batches=list(open_starts.values()),
        closed_batch_count=closed,
        scanned_offsets=scanned,
        committed_frontiers=committed_frontiers,
    )


def emit_recovery_aborts(
    log: LogClient,
    report: RecoveryReport,
    *,
    actor: str = "recovery",
) -> int:
    """Emit `BatchAborted(reason="recovery_orphan")` for every open
    batch in the report. Returns the number of aborts emitted.

    After this call, the log is self-consistent: no `BatchStarted`
    without a matching close. A re-scan would return zero open
    batches.

    Note: this does not clean up the partial outputs that were written
    under the open `batch_id`. That's the writer's responsibility per
    its quarantine vs discard policy.
    """
    from ..schema.events import BatchAborted

    n = 0
    for ob in report.open_batches:
        log.append(BatchAborted(
            consumer_id=ob.consumer_id,
            partial_count=ob.expected_count,  # unknown; use expected as best guess
            reason="recovery_orphan",
            detail=f"orphan_at_offset_{ob.start_offset}",
            batch_id=ob.batch_id,
            actor=actor,
        ))
        n += 1
    return n


def committed_frontier(
    log: LogClient,
    consumer_id: str,
    *,
    start_offset: int = 1,
    end_offset: int | None = None,
) -> int:
    """Convenience: return `committed_frontier_offset` for a single consumer.

    Equal to the largest offset N such that every batch this consumer
    started with start_offset ≤ N has been closed.
    """
    report = scan_for_orphans(
        log,
        consumers=[consumer_id],
        start_offset=start_offset,
        end_offset=end_offset,
    )
    return report.committed_frontiers.get(consumer_id, (end_offset or log.current_offset() + 1) - 1)


__all__ = [
    "OpenBatch",
    "RecoveryReport",
    "committed_frontier",
    "emit_recovery_aborts",
    "scan_for_orphans",
]
