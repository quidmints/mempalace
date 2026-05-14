"""
Python-side log client.

Wraps the Rust-side append-only log (mempalace_core, via PyO3). Provides:

  - append(event) — validate then append; returns log offset.
  - current_offset() — the latest log offset.
  - read_range(start, end) — read events in an offset range.

The Rust binding (`mempalace_core`) is imported lazily so Python-only
unit tests can run with a mock log. In production the binding is loaded;
in tests, `LogClient.set_backend(MockBackend())` substitutes an in-memory
implementation.

Spec ref: Part 2.4
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, runtime_checkable

from ..schema.events import AnyEvent, AppendRejected, Event
from ..schema.validators import validate_event, NodeKindLookup, ValidationResult


# =============================================================================
# Backend protocol
#
# The actual Rust binding implements LogBackend. The MockBackend below is for
# tests and for development before the Rust crate is built.
# =============================================================================

@runtime_checkable
class LogBackend(Protocol):
    def append(self, event_kind: str, payload: dict) -> int:
        """Append a serialized event. Returns the log offset assigned."""
        ...

    def current_offset(self) -> int: ...

    def read_range(self, start: int, end: int) -> list[tuple[int, str, dict]]:
        """Return list of (offset, kind, payload) tuples in [start, end)."""
        ...


# =============================================================================
# Mock backend (in-memory)
#
# Used in tests and during early development.
# =============================================================================

@dataclass
class MockBackend:
    _entries: list[tuple[int, str, dict]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _offset: int = 0

    def append(self, event_kind: str, payload: dict) -> int:
        with self._lock:
            self._offset += 1
            self._entries.append((self._offset, event_kind, payload))
            return self._offset

    def current_offset(self) -> int:
        with self._lock:
            return self._offset

    def read_range(self, start: int, end: int) -> list[tuple[int, str, dict]]:
        with self._lock:
            return [
                (offset, kind, payload)
                for offset, kind, payload in self._entries
                if start <= offset < end
            ]

    # Test helpers — not part of LogBackend protocol.
    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._offset = 0

    # Erasure support — Track 6D's compaction job uses this to
    # rewrite events in-place, preserving offset / kind but stripping
    # ciphertext. Real production backends implement this against
    # their on-disk format; MockBackend mutates the in-memory list.
    def rewrite_payload(
        self,
        offset: int,
        new_payload: dict,
    ) -> bool:
        """Replace the payload at `offset` with `new_payload`.

        Returns True if rewrite succeeded; False if offset not found.
        Caller is responsible for ensuring `new_payload` preserves
        whatever invariants matter (e.g. event_id, batch_id).
        """
        with self._lock:
            for i, (off, kind, _payload) in enumerate(self._entries):
                if off == offset:
                    self._entries[i] = (off, kind, new_payload)
                    return True
            return False


@runtime_checkable
class TombstoningBackend(Protocol):
    """Optional Protocol — backends that support Track 6D erasure.

    Not all backends implement this. The erasure compaction job
    checks for it and falls back to a degraded path (logs warning,
    refuses to perform irreversible erasure) when absent.
    """

    def rewrite_payload(self, offset: int, new_payload: dict) -> bool: ...


# =============================================================================
# Log client
# =============================================================================

@dataclass
class AppendResult:
    offset: int
    accepted: bool
    validation: ValidationResult


class LogClient:
    """Single Python-facing interface to the event log.

    Thread-safe. Any consumer that needs to read or append events goes
    through this client.
    """

    def __init__(
        self,
        backend: LogBackend | None = None,
        node_kind_lookup: NodeKindLookup | None = None,
    ) -> None:
        self._backend: LogBackend = backend or MockBackend()
        self._node_kind_lookup = node_kind_lookup
        self._lock = threading.Lock()
        # batch_id → consumer_id for the Rust bridge.
        # Populated on batch_started, consulted on in-batch events,
        # cleared on batch_committed/aborted.
        self._batch_consumer_map: dict[str, str] = {}

    def set_backend(self, backend: LogBackend) -> None:
        with self._lock:
            self._backend = backend

    def set_node_kind_lookup(self, lookup: NodeKindLookup | None) -> None:
        with self._lock:
            self._node_kind_lookup = lookup

    def append(self, event: Event) -> AppendResult:
        """Validate and append an event.

        On validation failure: emits an `append_rejected` event in place of
        the original. The original is *not* appended; the rejection is.

        Returns AppendResult with the assigned log offset.

        Phase 5 wire: after a successful append, notifies the Rust
        frontier bridge of batch lifecycle (BatchStarted / Committed /
        Aborted) and per-consumer applied offsets. Bridge calls are
        no-ops when the Rust extension isn't built.
        """
        # Fill in recorded_at if not set
        if not event.recorded_at:
            event.recorded_at = int(time.time() * 1000)

        validation = validate_event(event, self._node_kind_lookup)
        if not validation.ok:
            rejected = validation.to_rejected_event(event)
            rejected.recorded_at = event.recorded_at
            with self._lock:
                offset = self._backend.append(
                    rejected.kind,
                    _serialize_event(rejected),
                )
            return AppendResult(offset=offset, accepted=False, validation=validation)

        with self._lock:
            offset = self._backend.append(
                event.kind,
                _serialize_event(event),
            )

        # Phase 5 wire: notify the Rust frontier bridge.
        # Lifecycle events drive batch_started / batch_closed; in-batch
        # events drive record_applied keyed by the consumer that opened
        # the batch.
        self._notify_rust_frontier(event, offset)

        return AppendResult(offset=offset, accepted=True, validation=validation)

    def _notify_rust_frontier(self, event: Event, offset: int) -> None:
        """Push this append to the Rust frontier bridge.

        Three event categories:

          - `batch_started`: notify_batch_opened. Also remember the
            consumer_id under this batch_id so subsequent in-batch
            events can be attributed.
          - `batch_committed` / `batch_aborted`: notify_batch_closed.
            Forget the batch_id → consumer_id mapping.
          - Anything else with a non-empty batch_id: notify_applied
            under the consumer of the matching batch_started.
            Events with batch_id="" (implicit single-event batches
            with no recorded consumer) are skipped — they don't belong
            to any consumer's frontier.
        """
        from .rust_bridge import get_frontier_bridge

        try:
            bridge = get_frontier_bridge()
        except Exception:
            # Bridge construction itself shouldn't ever fail, but if it
            # does, do not propagate — frontier wiring must never break
            # the core append path.
            return

        kind = event.kind

        if kind == "batch_started":
            consumer_id = getattr(event, "consumer_id", "")
            batch_id = getattr(event, "batch_id", "")
            if consumer_id and batch_id:
                # Remember the mapping for in-batch events.
                with self._lock:
                    self._batch_consumer_map[batch_id] = consumer_id
                bridge.notify_batch_opened(consumer_id, batch_id, offset)
            return

        if kind in ("batch_committed", "batch_aborted"):
            consumer_id = getattr(event, "consumer_id", "")
            batch_id = getattr(event, "batch_id", "")
            if consumer_id and batch_id:
                bridge.notify_batch_closed(consumer_id, batch_id)
                # Forget the mapping (after notifying close, no more
                # in-batch events should arrive on this batch_id).
                with self._lock:
                    self._batch_consumer_map.pop(batch_id, None)
            return

        # In-batch event: look up the consumer via batch_id.
        batch_id = getattr(event, "batch_id", "")
        if not batch_id:
            # No batch_id → not part of any consumer's frontier.
            return
        with self._lock:
            consumer_id = self._batch_consumer_map.get(batch_id)
        if consumer_id is None:
            # We didn't see the batch_started for this batch — could be
            # a recovery scenario where events landed before the bridge
            # was warm. Skip. (The scan-based path handles it.)
            return
        bridge.notify_applied(consumer_id, offset)

    def current_offset(self) -> int:
        return self._backend.current_offset()

    def read_range(
        self, start: int, end: int
    ) -> Iterator[tuple[int, str, dict]]:
        """Read events with offset in [start, end). Yields (offset, kind, payload)."""
        for entry in self._backend.read_range(start, end):
            yield entry

    def supports_tombstoning(self) -> bool:
        """Whether the backend implements Track 6D erasure support."""
        return isinstance(self._backend, TombstoningBackend)

    def rewrite_payload(self, offset: int, new_payload: dict) -> bool:
        """Track 6D — rewrite an event's payload in-place.

        Used by the erasure compaction job to tombstone events.
        Returns False if backend doesn't support tombstoning OR
        the offset wasn't found.
        """
        if not self.supports_tombstoning():
            return False
        return self._backend.rewrite_payload(offset, new_payload)

    def batch(
        self,
        consumer_id: str,
        *,
        batch_id: str | None = None,
        expected_count: int = 0,
        input_summary: dict | None = None,
        actor: str = "system",
    ) -> "BatchHandle":
        """Open a multi-event batch (Phase 1 framing).

        Use as a context manager:

            with log.batch("graph.assert_triple", expected_count=4) as bh:
                bh.append(NodeCreated(...))
                bh.append(EdgeCreated(...))
                bh.append(EdgeCreated(...))

        On clean exit emits `BatchCommitted`. On exception emits
        `BatchAborted` and re-raises. All events appended through the
        handle carry the batch's `batch_id`.

        `consumer_id` identifies the writer for recovery scans.
        `batch_id` is auto-generated if not provided.
        """
        from ..schema.identifiers import make_batch_id
        bid = batch_id or make_batch_id()
        return BatchHandle(
            log=self,
            batch_id=bid,
            consumer_id=consumer_id,
            expected_count=expected_count,
            input_summary=dict(input_summary or {}),
            actor=actor,
        )

    def open_batch(
        self,
        consumer_id: str,
        batch_id: str,
        *,
        expected_count: int = 0,
        input_summary: dict | None = None,
        actor: str = "system",
    ) -> None:
        """Open a distributed-lifecycle batch.

        Used when batch open/close are separated by user calls — e.g.
        a sandbox lifecycle (provision opens; teardown closes) or a job
        lifecycle (schedule opens; complete/fail closes).

        Caller is responsible for matching `close_batch` or
        `abort_batch` later. Until that happens, the batch is open and
        will be reported by `scan_for_orphans` if the process restarts
        in between.

        `batch_id` is the caller's own identifier — typically the
        `sandbox_id`, `job_id`, `handle_id`, or `match_id`. Re-using a
        domain-specific id as the batch_id makes recovery diagnostics
        readable.
        """
        from ..schema.events import BatchStarted
        self.append(BatchStarted(
            consumer_id=consumer_id,
            expected_count=expected_count,
            input_summary=dict(input_summary or {}),
            batch_id=batch_id,
            actor=actor,
        ))

    def close_batch(
        self,
        consumer_id: str,
        batch_id: str,
        *,
        actual_count: int = 0,
        actor: str = "system",
    ) -> None:
        """Close a distributed-lifecycle batch (clean commit)."""
        from ..schema.events import BatchCommitted
        self.append(BatchCommitted(
            consumer_id=consumer_id,
            actual_count=actual_count,
            batch_id=batch_id,
            actor=actor,
        ))

    def abort_batch(
        self,
        consumer_id: str,
        batch_id: str,
        *,
        partial_count: int = 0,
        reason: str = "user_cancel",
        detail: str = "",
        actor: str = "system",
    ) -> None:
        """Close a distributed-lifecycle batch (abort)."""
        from ..schema.events import BatchAborted
        self.append(BatchAborted(
            consumer_id=consumer_id,
            partial_count=partial_count,
            reason=reason,
            detail=detail[:512],
            batch_id=batch_id,
            actor=actor,
        ))


# =============================================================================
# BatchHandle — context manager for multi-event ops
# =============================================================================


@dataclass
class BatchAppendResult:
    """Per-event append result inside a batch.

    `output_index` is the monotonic position of this event within the
    batch (0-based). Combined with `batch_id` and the writer's
    `consumer_id`, this is the idempotency key for stores that dedupe
    on retry.
    """
    offset: int
    output_index: int
    accepted: bool
    validation: ValidationResult


class BatchHandle:
    """Open batch handle. Returned by `LogClient.batch()`.

    Tracks `output_index` (auto-increments per `append`) and the
    `committed`/`aborted` state so the close path knows whether to emit
    BatchCommitted or BatchAborted.
    """

    def __init__(
        self,
        *,
        log: "LogClient",
        batch_id: str,
        consumer_id: str,
        expected_count: int,
        input_summary: dict,
        actor: str,
    ) -> None:
        self._log = log
        self.batch_id = batch_id
        self.consumer_id = consumer_id
        self._expected_count = expected_count
        self._input_summary = input_summary
        self._actor = actor
        self._output_index = 0
        self._opened = False
        self._closed = False

    def __enter__(self) -> "BatchHandle":
        # Late import: events module imports identifiers which is fine,
        # but we want BatchStarted/Aborted/Committed.
        from ..schema.events import BatchStarted

        self._log.append(BatchStarted(
            consumer_id=self.consumer_id,
            expected_count=self._expected_count,
            input_summary=self._input_summary,
            batch_id=self.batch_id,
            actor=self._actor,
        ))
        self._opened = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        from ..schema.events import BatchAborted, BatchCommitted

        if self._closed:
            return False

        if exc_type is None:
            self._log.append(BatchCommitted(
                consumer_id=self.consumer_id,
                actual_count=self._output_index,
                batch_id=self.batch_id,
                actor=self._actor,
            ))
        else:
            # Best-effort abort. Don't suppress the exception.
            try:
                self._log.append(BatchAborted(
                    consumer_id=self.consumer_id,
                    partial_count=self._output_index,
                    reason="exception",
                    detail=f"{exc_type.__name__}: {exc_val}"[:512],
                    batch_id=self.batch_id,
                    actor=self._actor,
                ))
            except Exception:
                # If even the abort emission fails, swallow — the
                # recovery scan will still find the orphan BatchStarted.
                pass
        self._closed = True
        return False  # do not suppress

    def append(self, event: Event) -> BatchAppendResult:
        """Append an event under this batch_id.

        Stamps `event.batch_id` automatically. Returns the assigned
        offset, the output_index within the batch, and validation.
        """
        if not self._opened:
            raise RuntimeError(
                f"BatchHandle for {self.consumer_id} used before __enter__"
            )
        if self._closed:
            raise RuntimeError(
                f"BatchHandle for {self.consumer_id} used after __exit__"
            )

        event.batch_id = self.batch_id
        idx = self._output_index
        self._output_index += 1

        result = self._log.append(event)
        return BatchAppendResult(
            offset=result.offset,
            output_index=idx,
            accepted=result.accepted,
            validation=result.validation,
        )

    def abort(self, reason: str = "user_cancel", detail: str = "") -> None:
        """Explicit abort. After calling this, further `append` will
        raise; __exit__ becomes a no-op."""
        from ..schema.events import BatchAborted

        if self._closed:
            return
        self._log.append(BatchAborted(
            consumer_id=self.consumer_id,
            partial_count=self._output_index,
            reason=reason,
            detail=detail[:512],
            batch_id=self.batch_id,
            actor=self._actor,
        ))
        self._closed = True

    def checkpoint(self, reason: str = "periodic") -> int:
        """Emit a `BatchCheckpointed` marker — PHASE1 §J sub-batch
        checkpointing.

        Long-running batches call this at safe intermediate boundaries
        so that a crash mid-batch only loses the trailing fragment
        after the last checkpoint, not the entire batch.

        Recovery treats events between `BatchStarted` and the latest
        `BatchCheckpointed` as durable; they don't need replay. The
        checkpoint records the current `output_index_so_far` so
        recovery can reconcile partial results.

        Returns the log offset of the checkpoint event.

        Args:
          reason: short diagnostic code ("periodic" / "boundary" /
            "manual" / "external_dependency_synced" / etc.).

        Raises:
          RuntimeError if the batch hasn't entered or is already closed.
        """
        from ..schema.events import BatchCheckpointed

        if not self._opened:
            raise RuntimeError(
                f"BatchHandle for {self.consumer_id} checkpoint before __enter__"
            )
        if self._closed:
            raise RuntimeError(
                f"BatchHandle for {self.consumer_id} checkpoint after close"
            )

        result = self._log.append(BatchCheckpointed(
            consumer_id=self.consumer_id,
            output_index_so_far=self._output_index,
            reason=reason,
            batch_id=self.batch_id,
            actor=self._actor,
        ))
        return result.offset

    @property
    def output_index(self) -> int:
        """Current output index — equal to the count of events appended."""
        return self._output_index


# =============================================================================
# Serialization
#
# We use plain dict serialization for portability between Python and Rust.
# Dataclass-to-dict via __dict__; nested dataclasses (StateContext, etc.)
# are flattened by the per-event-kind explicit fields rather than relying
# on recursion.
# =============================================================================

def _serialize_event(event: Event) -> dict:
    """Serialize an event to a plain dict for the backend.

    Excludes `recorded_at` from the payload because the backend records it
    explicitly. Excludes `event_id` for similar reason — the backend assigns
    log offsets.
    """
    out: dict = {}
    for field_name, value in event.__dict__.items():
        if field_name in ("recorded_at",):
            # Carried as separate metadata at the backend layer; included for
            # completeness so tests can round-trip.
            out[field_name] = value
            continue
        if hasattr(value, "__dict__"):
            # Nested dataclass — convert to dict recursively. Limited depth.
            out[field_name] = dict(value.__dict__)
        else:
            out[field_name] = value
    return out


# =============================================================================
# Module-level singleton
#
# In production a single LogClient is shared across the Python process. Tests
# can construct their own; the singleton here is for convenience.
# =============================================================================

_default_client: LogClient | None = None
_default_lock = threading.Lock()


def get_default_client() -> LogClient:
    """Return the process-wide LogClient singleton (creating if needed)."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = LogClient()
        return _default_client


def set_default_client(client: LogClient) -> None:
    """Replace the process-wide singleton (useful in tests)."""
    global _default_client
    with _default_lock:
        _default_client = client
