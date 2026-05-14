"""
Process-startup recovery hook (Phase 1 sub-slice 3).

Composes the pieces from `recovery.py` and the per-store quarantine
APIs into a single startup routine consumers call once on process
start.

Flow:
  1. `scan_for_orphans` finds open batches.
  2. `emit_recovery_aborts` makes the log self-consistent (every
     BatchStarted has a matching close).
  3. The set of newly-aborted batch_ids is forwarded to each store
     that supports `quarantine_torn_batches`. Quarantined entries
     remain visible to diagnostic APIs but are excluded from default
     reads.
  4. Returns the per-consumer `committed_frontier_offset` map for
     downstream readers to consult.

Per-writer policy is governed by the design doc (§D, PHASE1_DESIGN.md):
some writers want quarantine (interpretable forensics — miner
proposals, signatures, canonicalizer promotions), others want discard
(cheap-to-recompute work — graph.assert_triple, sandbox lifecycle,
jobs, handles, migrate). The hook here implements quarantine for the
stores that opt in. Discard policy is the absence of a store —
those writers' partial outputs are simply re-derived on the next run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from .client import LogClient
from .recovery import (
    OpenBatch,
    RecoveryReport,
    emit_recovery_aborts,
    scan_for_orphans,
)


class QuarantinableStore(Protocol):
    """Stores that participate in torn-batch quarantine implement
    this protocol."""

    def quarantine_torn_batches(self, torn_batch_ids: set[str]) -> int:
        ...


@dataclass
class RecoveryHookResult:
    """Result of `run_recovery_on_startup`."""

    report: RecoveryReport
    aborts_emitted: int = 0
    # store_name → count of entries quarantined in that store
    quarantine_counts: dict[str, int] = field(default_factory=dict)

    @property
    def torn_batch_ids(self) -> set[str]:
        return {ob.batch_id for ob in self.report.open_batches}

    @property
    def has_torn_batches(self) -> bool:
        return self.report.has_torn_batches

    def committed_frontier_for(self, consumer_id: str) -> int | None:
        return self.report.committed_frontiers.get(consumer_id)


def run_recovery_on_startup(
    log: LogClient,
    *,
    stores: dict[str, QuarantinableStore] | None = None,
    consumers: Iterable[str] | None = None,
) -> RecoveryHookResult:
    """Run the full recovery protocol on process start.

    Args:
      log: the log client to scan.
      stores: optional mapping of store-name → store-with-quarantine.
        Each will be told the set of torn batch_ids so it can mark
        affected entries. Conventionally:
          - "proposals":  ProposalStore
          - "signatures": SignatureStore
        Stores that don't yet implement `quarantine_torn_batches` are
        simply omitted.
      consumers: optional consumer-id filter; if given, scan returns
        open batches only for these consumers.

    Returns RecoveryHookResult. Callers typically:
      - check `has_torn_batches` for logging
      - call `committed_frontier_for(consumer_id)` to know where to
        resume each consumer

    The hook is idempotent: running it twice is safe. A second run
    finds zero open batches (the first run aborted them all) and
    quarantines no new entries.
    """
    report = scan_for_orphans(log, consumers=consumers)
    aborts = emit_recovery_aborts(log, report) if report.has_torn_batches else 0

    quarantine_counts: dict[str, int] = {}
    if stores and report.has_torn_batches:
        torn_ids = {ob.batch_id for ob in report.open_batches}
        for name, store in stores.items():
            try:
                quarantine_counts[name] = store.quarantine_torn_batches(torn_ids)
            except AttributeError:
                # Store didn't implement the protocol — skip silently
                quarantine_counts[name] = 0

    return RecoveryHookResult(
        report=report,
        aborts_emitted=aborts,
        quarantine_counts=quarantine_counts,
    )


__all__ = [
    "QuarantinableStore",
    "RecoveryHookResult",
    "run_recovery_on_startup",
]
