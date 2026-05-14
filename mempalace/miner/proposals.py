"""
Proposal lifecycle store.

Per Part 10.6: every miner output is `provisional` until confirmed.
Lifecycle states:

  - provisional   — initial state on miner emit
  - confirmed     — user action or auto-promotion threshold met
  - rejected      — user action or strong contradicting evidence

Rejected outputs persist in the log as feedback for miner training;
they don't surface in retrieval (inhibition edges suppress them under
all stances).

This module owns:

  - ProposalStore: in-memory aggregation of proposals by id and lifecycle
  - confirm()/reject() helpers that emit lifecycle events
  - auto_promotion_check(): decides whether a provisional proposal
    crosses the auto-promotion threshold

Spec ref: Part 10.6.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Iterable

from .base import ProposalLifecycle, ProposalRecord


# =============================================================================
# Auto-promotion thresholds
# =============================================================================


# Per Part 10.6: corroboration threshold for auto-promotion. A
# provisional proposal that gets corroborated by N independent inputs
# can auto-promote to confirmed without explicit user action.
DEFAULT_AUTO_PROMOTION_CORROBORATION = 3
DEFAULT_AUTO_PROMOTION_CONFIDENCE = 0.85


# =============================================================================
# Store
# =============================================================================


@dataclass
class ProposalEntry:
    """Wraps a ProposalRecord with lifecycle metadata.

    `idempotency_key` (Phase 1 sub-slice 3) ties this entry to the
    batch + output_index that produced it. Used to dedupe retries on
    crash recovery.

    `from_torn_batch` is set when recovery determines the batch this
    entry was produced under was aborted. Quarantine policy: still
    visible to diagnostic queries (`all_including_torn()`) but
    excluded from default reads (`all()`, `by_kind()`, etc.).
    """

    record: ProposalRecord
    corroboration_count: int = 0
    rejection_count: int = 0
    confirmed_at_ms: int = 0
    rejected_at_ms: int = 0
    idempotency_key: tuple[str, str, int] | None = None
    from_torn_batch: bool = False


@dataclass
class AddResult:
    """Result of a `ProposalStore.add` with optional idempotency key."""

    entry: ProposalEntry
    deduplicated: bool = False  # True if the key was a retry of an earlier add


class ProposalStore:
    """In-memory store of proposals indexed by id and (kind, target).

    Phase 1 sub-slice 3 additions:
      - optional `idempotency_key` on add() for retry-safe writes
      - `from_torn_batch` quarantine marker per entry
      - `quarantine_torn_batches(batch_ids)` for recovery hooks
      - default reads exclude torn entries
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ProposalEntry] = {}
        # (consumer_id, batch_id, output_index) → proposal_id
        # for retry deduplication
        self._by_idempotency_key: dict[tuple[str, str, int], str] = {}
        # batch_id → set[proposal_id] for fast quarantine on recovery
        self._by_batch_id: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    # ---- ingest -----------------------------------------------------------

    def add(
        self,
        record: ProposalRecord,
        *,
        idempotency_key: tuple[str, str, int] | None = None,
        log_offset: int = 0,
        dependencies: list[tuple[str, int]] | None = None,
    ) -> AddResult:
        """Add a proposal, optionally with an idempotency key.

        Phase 2: if the record's version_stamp is unset, stamps it
        from the proposed_value + log_offset + dependencies. Pre-stamped
        records are passed through unchanged (the miner pass had
        better dependency info).

        If `idempotency_key` is provided and matches a previous add,
        returns the existing entry with `deduplicated=True` and does
        NOT increment corroboration. (Retries are not corroborations.)

        If the key is new, the entry is inserted and indexed.
        """
        # Phase 2: ensure the record has a version_stamp
        if not record.version_stamp.is_stamped:
            from ..schema.versioning import make_stamp
            content_bytes = json.dumps(
                {
                    "proposal_kind": record.proposal_kind,
                    "target_node_id": record.target_node_id,
                    "proposed_value": record.proposed_value,
                    "confidence": record.confidence,
                    "miner_class": record.miner_class,
                    "miner_version": record.miner_version,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            record.version_stamp = make_stamp(
                content=content_bytes,
                log_offset=log_offset,
                dependencies=dependencies or [],
            )

        with self._lock:
            # 1. Idempotency check: same (consumer, batch, output) → dedupe
            if idempotency_key is not None:
                seen_pid = self._by_idempotency_key.get(idempotency_key)
                if seen_pid is not None:
                    return AddResult(
                        entry=self._by_id[seen_pid],
                        deduplicated=True,
                    )

            # 2. Same proposal_id seen again → corroboration (legacy
            # pre-batch-framing path). This covers the case where a
            # proposal is independently re-derived from a different
            # batch — that's a real corroboration, not a retry.
            existing = self._by_id.get(record.proposal_id)
            if existing is not None:
                existing.corroboration_count += 1
                if idempotency_key is not None:
                    # Index this key against the existing proposal so
                    # future retries with the same key dedupe properly
                    self._by_idempotency_key[idempotency_key] = record.proposal_id
                return AddResult(entry=existing, deduplicated=False)

            # 3. Genuinely new
            entry = ProposalEntry(
                record=record,
                corroboration_count=1,
                idempotency_key=idempotency_key,
            )
            self._by_id[record.proposal_id] = entry
            if idempotency_key is not None:
                self._by_idempotency_key[idempotency_key] = record.proposal_id
                _, batch_id, _ = idempotency_key
                self._by_batch_id.setdefault(batch_id, set()).add(record.proposal_id)
            return AddResult(entry=entry, deduplicated=False)

    def add_batch(
        self,
        records: Iterable[ProposalRecord],
        *,
        consumer_id: str | None = None,
        batch_id: str | None = None,
    ) -> list[AddResult]:
        """Add a batch of records.

        If both `consumer_id` and `batch_id` are provided, each record
        is added under an auto-derived idempotency_key
        `(consumer_id, batch_id, i)` where i is the position in the
        iterable. This is the natural shape for miner pass outputs.
        """
        results = []
        for i, r in enumerate(records):
            key = (
                (consumer_id, batch_id, i)
                if consumer_id and batch_id
                else None
            )
            results.append(self.add(r, idempotency_key=key))
        return results

    # ---- recovery ---------------------------------------------------------

    def quarantine_torn_batches(self, torn_batch_ids: set[str]) -> int:
        """Mark all entries from these batch_ids as `from_torn_batch=True`.

        Called by the recovery hook after `scan_for_orphans` identifies
        torn batches. Returns the number of entries quarantined.

        Default reads (`all`, `by_kind`, `by_lifecycle`) exclude
        quarantined entries. Diagnostic API (`all_including_torn`)
        includes them.
        """
        n = 0
        with self._lock:
            for bid in torn_batch_ids:
                pids = self._by_batch_id.get(bid, set())
                for pid in pids:
                    entry = self._by_id.get(pid)
                    if entry is not None and not entry.from_torn_batch:
                        entry.from_torn_batch = True
                        n += 1
        return n

    # ---- lifecycle --------------------------------------------------------

    def confirm(self, proposal_id: str, *, now_ms: int) -> bool:
        with self._lock:
            entry = self._by_id.get(proposal_id)
            if entry is None:
                return False
            entry.record.lifecycle = ProposalLifecycle.CONFIRMED
            entry.confirmed_at_ms = now_ms
            return True

    def reject(self, proposal_id: str, *, now_ms: int) -> bool:
        with self._lock:
            entry = self._by_id.get(proposal_id)
            if entry is None:
                return False
            entry.record.lifecycle = ProposalLifecycle.REJECTED
            entry.rejected_at_ms = now_ms
            entry.rejection_count += 1
            return True

    # ---- introspection ----------------------------------------------------

    def get(self, proposal_id: str) -> ProposalEntry | None:
        with self._lock:
            return self._by_id.get(proposal_id)

    def by_lifecycle(self, lifecycle: ProposalLifecycle) -> list[ProposalEntry]:
        with self._lock:
            return [
                e for e in self._by_id.values()
                if e.record.lifecycle == lifecycle
                and not e.from_torn_batch
            ]

    def by_kind(self, kind: str) -> list[ProposalEntry]:
        with self._lock:
            return [
                e for e in self._by_id.values()
                if e.record.proposal_kind == kind
                and not e.from_torn_batch
            ]

    def all(self) -> list[ProposalEntry]:
        """All non-quarantined entries (default reads exclude torn)."""
        with self._lock:
            return [e for e in self._by_id.values() if not e.from_torn_batch]

    def all_including_torn(self) -> list[ProposalEntry]:
        """All entries including those quarantined from torn batches.
        Diagnostic use only."""
        with self._lock:
            return list(self._by_id.values())

    def size(self) -> int:
        """Count of non-quarantined entries."""
        with self._lock:
            return sum(
                1 for e in self._by_id.values() if not e.from_torn_batch
            )

    def size_including_torn(self) -> int:
        with self._lock:
            return len(self._by_id)


# =============================================================================
# Auto-promotion check
# =============================================================================


def auto_promotion_check(
    entry: ProposalEntry,
    *,
    corroboration_threshold: int = DEFAULT_AUTO_PROMOTION_CORROBORATION,
    confidence_threshold: float = DEFAULT_AUTO_PROMOTION_CONFIDENCE,
) -> bool:
    """Return True if the proposal qualifies for auto-promotion."""
    if entry.record.lifecycle != ProposalLifecycle.PROVISIONAL:
        return False
    if entry.rejection_count > 0:
        return False
    if entry.corroboration_count < corroboration_threshold:
        return False
    if entry.record.confidence < confidence_threshold:
        return False
    return True


# =============================================================================
# Module-level singleton
# =============================================================================


_STORE: ProposalStore | None = None
_STORE_LOCK = threading.Lock()


def get_proposal_store() -> ProposalStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ProposalStore()
        return _STORE


def set_proposal_store(store: ProposalStore) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store


__all__ = [
    "DEFAULT_AUTO_PROMOTION_CONFIDENCE",
    "DEFAULT_AUTO_PROMOTION_CORROBORATION",
    "ProposalEntry",
    "ProposalStore",
    "auto_promotion_check",
    "get_proposal_store",
    "set_proposal_store",
]
