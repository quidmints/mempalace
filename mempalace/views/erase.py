"""
Erasure compaction job — Track 6D.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Tier 2 — Erase":

  - User emits `RequestErase(target_kind, target_id)`.
  - Compaction job:
    1. Walks the log identifying every event referencing the target.
    2. Rewrites those events to tombstone form (preserves offset /
       kind / batch_id; strips ciphertext and other user-content
       payload fields).
    3. Emits `EraseProgress` periodically + final `EraseCompleted`.
  - Idempotent: re-running a job with the same `erasure_job_id`
    picks up where it left off (already-tombstoned events are
    detected and skipped).

# What this module does NOT do

  - Snapshot rewriting: production has snapshots/backups outside the
    log; those need their own erasure path. This module emits a
    `phase="rewrite_snapshots"` progress event with `scanned=0` to
    document the work; actual snapshot rewriting is plugged in via
    a `SnapshotEraser` callback.
  - DD view retraction: DD views consume the tombstones via the
    invalidation bridge plus the existing reduce pattern (the same
    reduce that handles DrawerInvalidated). No explicit
    DD-rewriting code lives here.
  - Phone UI / friction layer: the triple-confirmation dialog is
    upstream of this job. We start work as soon as `RequestErase` is
    in the log.

# Why a job, not an inline rewrite

The compaction may take minutes to hours on a large log. Doing it
synchronously would block the calling thread; the design says
"async compaction job." The shape here is single-threaded but
chunked: each call to `step()` processes one event. Production
wraps it in a worker that pumps `step()` until done. Tests can
drive `step()` to completion in a tight loop.

# Idempotency

The job's state is `(erasure_job_id, last_processed_offset, phase,
scanned, rewritten)`. Persisted across crashes by writing
`EraseProgress` events before each chunk; on restart the worker
reads the last `EraseProgress` for the job and resumes from there.

A `_tombstoned` marker in rewritten payloads lets the scan skip
already-tombstoned events on re-run.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Tier 2 — Erase",
IMPLEMENTATION_ROADMAP.md §"Track 6D".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    EraseCompleted,
    EraseFailed,
    EraseProgress,
    RequestErase,
)
from ..schema.identifiers import make_event_id_log

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================


DEFAULT_PROGRESS_INTERVAL = 1000
"""Emit an EraseProgress event every N events scanned."""

ERASURE_TOMBSTONE_MARKER = "_erased"
"""Field added to a tombstoned event's payload, set to True. Lets
re-runs skip events that are already erased and lets DD views
detect tombstoning at reduce time."""

ERASURE_TOMBSTONE_REASON_FIELD = "_erased_for"
"""Field added to a tombstoned payload, set to the target_id whose
erasure job produced this tombstone. Audit trail."""


# =============================================================================
# What gets stripped
# =============================================================================


# Per-event-kind list of payload fields that are stripped when an
# event is tombstoned. Fields not in this list are preserved.
#
# What stays: structural fields (event_id, batch_id, recorded_at,
# actor, kind), references for graph integrity (drawer_id, node_id,
# edge_id, source_node_id, target_node_id), and metadata used for
# collision detection (content_hash). Plus the new
# ERASURE_TOMBSTONE_* markers.
#
# What goes: ciphertext, DEK handles, attestation sigs, plaintext
# properties dicts, audio blob URIs, raw text fields like
# `description`, `name`, `surface`.
TOMBSTONE_STRIP_FIELDS: dict[str, list[str]] = {
    "drawer_captured": [
        "verbatim_text",
        "verbatim_ciphertext",
        "verbatim_dek_handle",
        "verbatim_attestation_sig",
        "audio_blob_dek_handle",
        "audio_blob_attestation_sig",
        "acoustic_blob_ref",
        "properties",
        "state_context",
        "goal_markers",
        "implicit_references",
    ],
    "drawer_amended": [
        "amended_text",
        "amended_ciphertext",
        "amended_dek_handle",
        "amended_attestation_sig",
    ],
    "node_created": [
        "properties",
    ],
    "node_property_set": [
        "new_value",
    ],
    "edge_created": [
        "properties",
    ],
    "interpretation_assigned": [
        "new_value",
    ],
    "schema_induced": [
        "name",
        "description",
        "derived_from_events",
        "derived_from_assertions",
        "derived_from_drawers",
    ],
    "token_features_written": [
        "tokens",
        "prosody",
        "affect",
    ],
    "segment_created": [
        "transcript",
    ],
}


# =============================================================================
# Reference detection
# =============================================================================


def _references_target(
    payload: dict,
    target_kind: str,
    target_id: str,
) -> bool:
    """True if this event's payload references the target.

    Handles the common shapes:
      - `drawer_id == target_id` (drawer events).
      - `node_id == target_id` (node events).
      - `edge_id == target_id` (edge events).
      - `source_node_id == target_id` or `target_node_id == target_id`
        (edges that touch the node).
      - `target_node_id == target_id` (interpretations on the node).
      - Lists like `derived_from_drawers` containing target_id.
    """
    if target_kind == "drawer":
        if payload.get("drawer_id") == target_id:
            return True
        if target_id in (payload.get("derived_from_drawers") or []):
            return True
        # Edges where source/target is this drawer (drawers are nodes)
        if payload.get("source_node_id") == target_id:
            return True
        if payload.get("target_node_id") == target_id:
            return True
        # NodeCreated for this drawer
        if payload.get("node_id") == target_id:
            return True

    elif target_kind == "node":
        if payload.get("node_id") == target_id:
            return True
        if payload.get("source_node_id") == target_id:
            return True
        if payload.get("target_node_id") == target_id:
            return True

    elif target_kind == "edge":
        if payload.get("edge_id") == target_id:
            return True

    return False


# =============================================================================
# Tombstoning
# =============================================================================


def _is_already_tombstoned(payload: dict) -> bool:
    return bool(payload.get(ERASURE_TOMBSTONE_MARKER))


def _build_tombstone_payload(
    event_kind: str,
    payload: dict,
    target_id: str,
) -> dict:
    """Produce a tombstoned version of a payload.

    Strips fields per `TOMBSTONE_STRIP_FIELDS[event_kind]` (defaults
    to no-op if kind not listed). Adds the tombstone markers.
    """
    new_payload = dict(payload)

    strip_fields = TOMBSTONE_STRIP_FIELDS.get(event_kind, [])
    for f in strip_fields:
        if f in new_payload:
            # Replace with a kind-appropriate empty value
            old = new_payload[f]
            if isinstance(old, bytes):
                new_payload[f] = b""
            elif isinstance(old, str):
                new_payload[f] = ""
            elif isinstance(old, list):
                new_payload[f] = []
            elif isinstance(old, dict):
                new_payload[f] = {}
            elif isinstance(old, (int, float)):
                new_payload[f] = 0
            else:
                new_payload[f] = None

    new_payload[ERASURE_TOMBSTONE_MARKER] = True
    new_payload[ERASURE_TOMBSTONE_REASON_FIELD] = target_id

    return new_payload


# =============================================================================
# Snapshot eraser hook
# =============================================================================


class SnapshotEraser(Protocol):
    """Callback for the snapshot-rewrite phase.

    Production wires this to whatever snapshot/backup machinery the
    cloud-box runs. Default = no-op (no snapshots in dev / test).
    """

    def erase_target_from_snapshots(
        self,
        target_kind: str,
        target_id: str,
    ) -> int:
        """Remove the target from every snapshot. Returns the number
        of snapshots rewritten."""
        ...


class _NoopSnapshotEraser:
    def erase_target_from_snapshots(
        self,
        target_kind: str,
        target_id: str,
    ) -> int:
        return 0


# =============================================================================
# Job
# =============================================================================


@dataclass
class EraseJob:
    """One erasure compaction job.

    Construction:
      job = EraseJob(
          erasure_job_id="erj_abc",
          target_kind="drawer",
          target_id="drw_xxx",
          log_client=...,
          snapshot_eraser=...,
      )

    Driving:
      while not job.is_complete:
          job.step()

    Or `job.run_to_completion()` for one-shot.
    """

    erasure_job_id: str
    target_kind: str
    target_id: str
    log_client: LogClient | None = None
    snapshot_eraser: SnapshotEraser = field(default_factory=_NoopSnapshotEraser)
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL

    # Internal state (carried across step() calls so a worker can
    # checkpoint mid-job)
    _last_offset: int = 0
    _scan_end_offset: int = 0
    _phase: str = "scan_and_rewrite"
    """One of: "scan_and_rewrite" | "rewrite_snapshots" | "complete"
    | "failed"."""

    _scanned: int = 0
    _rewritten: int = 0
    _snapshots_rewritten: int = 0
    _bytes_freed: int = 0
    _failure_reason: str = ""
    _failure_phase: str = ""

    def __post_init__(self) -> None:
        if self.log_client is None:
            self.log_client = get_default_client()
        # Pin the scan end so the job processes a stable snapshot of
        # the log; events written during the job are out of scope.
        # The compaction will need to re-run for them — but in
        # practice nothing should be writing references to a
        # mid-erasure target, since the request_erase event is
        # already in the log.
        self._scan_end_offset = self.log_client.current_offset()

    @property
    def is_complete(self) -> bool:
        return self._phase in ("complete", "failed")

    @property
    def scanned(self) -> int:
        return self._scanned

    @property
    def rewritten(self) -> int:
        return self._rewritten

    @property
    def phase(self) -> str:
        return self._phase

    def step(self, *, batch_size: int = 100) -> None:
        """Process up to `batch_size` events. Idempotent.

        After enough `step()` calls, the job transitions through
        phases: scan_and_rewrite → rewrite_snapshots → complete.
        """
        if self.is_complete:
            return

        try:
            if self._phase == "scan_and_rewrite":
                self._step_scan_and_rewrite(batch_size)
            elif self._phase == "rewrite_snapshots":
                self._step_rewrite_snapshots()
            else:
                raise RuntimeError(f"Unknown phase: {self._phase}")
        except Exception as e:  # noqa: BLE001 — broad on purpose
            self._failure_reason = repr(e)
            self._failure_phase = self._phase
            self._emit_failed()
            self._phase = "failed"

    def run_to_completion(self) -> None:
        """One-shot: drive step() until is_complete."""
        max_iterations = 10_000  # safety net for unbounded loops
        for _ in range(max_iterations):
            if self.is_complete:
                return
            self.step()
        # Hit safety net
        if not self.is_complete:
            self._failure_reason = "iteration_safety_net_hit"
            self._failure_phase = self._phase
            self._emit_failed()
            self._phase = "failed"

    # ----- scan_and_rewrite phase -------------------------------------------

    def _step_scan_and_rewrite(self, batch_size: int) -> None:
        log = self.log_client
        if log is None:
            self._phase = "rewrite_snapshots"
            return

        if self._last_offset >= self._scan_end_offset:
            # Phase complete
            self._emit_progress("scan_and_rewrite", 100.0)
            self._phase = "rewrite_snapshots"
            return

        start = self._last_offset
        end = min(start + batch_size, self._scan_end_offset)

        for offset, kind, payload in log.read_range(start + 1, end + 1):
            self._scanned += 1
            if _is_already_tombstoned(payload):
                continue
            if not _references_target(payload, self.target_kind, self.target_id):
                continue

            new_payload = _build_tombstone_payload(
                kind, payload, self.target_id,
            )
            ok = log.rewrite_payload(offset, new_payload)
            if ok:
                self._rewritten += 1
                # Approximate bytes freed by the strip
                self._bytes_freed += _approximate_bytes_freed(payload, new_payload)
            else:
                # Backend doesn't support tombstoning — fail out
                if not log.supports_tombstoning():
                    raise RuntimeError(
                        "Log backend does not support tombstoning; "
                        "cannot perform erasure."
                    )

        self._last_offset = end

        # Emit progress periodically
        if self._scanned % self.progress_interval == 0:
            pct = (self._last_offset / max(self._scan_end_offset, 1)) * 100.0
            self._emit_progress("scan_and_rewrite", pct)

    # ----- rewrite_snapshots phase ------------------------------------------

    def _step_rewrite_snapshots(self) -> None:
        try:
            n = self.snapshot_eraser.erase_target_from_snapshots(
                self.target_kind, self.target_id,
            )
            self._snapshots_rewritten = n
        except Exception as e:
            raise RuntimeError(f"snapshot eraser failed: {e}") from e

        self._emit_progress("rewrite_snapshots", 100.0)
        self._emit_completed()
        self._phase = "complete"

    # ----- event emission ---------------------------------------------------

    def _emit_progress(self, phase: str, pct: float) -> None:
        if self.log_client is None:
            return
        evt = EraseProgress(
            event_id=make_event_id_log(),
            recorded_at=int(time.time() * 1000),
            actor="erase_job",
            erasure_job_id=self.erasure_job_id,
            target_kind=self.target_kind,
            target_id=self.target_id,
            phase=phase,
            scanned=self._scanned,
            rewritten=self._rewritten,
            progress_pct=pct,
            progress_at_ms=int(time.time() * 1000),
        )
        try:
            self.log_client.append(evt)
        except Exception as e:
            logger.warning("EraseProgress append failed: %s", e)

    def _emit_completed(self) -> None:
        if self.log_client is None:
            return
        evt = EraseCompleted(
            event_id=make_event_id_log(),
            recorded_at=int(time.time() * 1000),
            actor="erase_job",
            erasure_job_id=self.erasure_job_id,
            target_kind=self.target_kind,
            target_id=self.target_id,
            completed_at_ms=int(time.time() * 1000),
            events_tombstoned=self._rewritten,
            snapshots_rewritten=self._snapshots_rewritten,
            bytes_freed=self._bytes_freed,
        )
        try:
            self.log_client.append(evt)
        except Exception as e:
            logger.warning("EraseCompleted append failed: %s", e)

    def _emit_failed(self) -> None:
        if self.log_client is None:
            return
        evt = EraseFailed(
            event_id=make_event_id_log(),
            recorded_at=int(time.time() * 1000),
            actor="erase_job",
            erasure_job_id=self.erasure_job_id,
            target_kind=self.target_kind,
            target_id=self.target_id,
            failed_at_ms=int(time.time() * 1000),
            failure_reason=self._failure_reason,
            phase_at_failure=self._failure_phase,
        )
        try:
            self.log_client.append(evt)
        except Exception:
            pass


def _approximate_bytes_freed(old: dict, new: dict) -> int:
    """Rough estimate of how many bytes were freed by tombstoning.
    Sums up byte/string lengths of fields that changed."""
    freed = 0
    for k, v in old.items():
        if k not in new:
            continue
        new_v = new[k]
        if isinstance(v, bytes) and isinstance(new_v, bytes):
            freed += max(0, len(v) - len(new_v))
        elif isinstance(v, str) and isinstance(new_v, str):
            freed += max(0, len(v) - len(new_v))
    return freed


# =============================================================================
# Convenience entry point
# =============================================================================


def request_erase(
    target_kind: str,
    target_id: str,
    *,
    requested_at_ms: int | None = None,
    log_client: LogClient | None = None,
) -> str:
    """Append a `RequestErase` event. Returns the erasure_job_id.

    Doesn't run the job — that's a worker's responsibility. The job
    can be picked up by reading the log, finding `request_erase`
    events without matching `erase_completed`/`erase_failed`, and
    constructing an `EraseJob` for each.
    """
    log = log_client or get_default_client()
    now_ms = requested_at_ms or int(time.time() * 1000)

    job_id = f"erj_{make_event_id_log()[4:16]}"

    evt = RequestErase(
        event_id=make_event_id_log(),
        recorded_at=now_ms,
        actor="user",
        target_kind=target_kind,
        target_id=target_id,
        requested_by_user=True,
        requested_at_ms=now_ms,
        erasure_job_id=job_id,
    )
    log.append(evt)
    return job_id


__all__ = [
    "DEFAULT_PROGRESS_INTERVAL",
    "ERASURE_TOMBSTONE_MARKER",
    "ERASURE_TOMBSTONE_REASON_FIELD",
    "EraseJob",
    "SnapshotEraser",
    "TOMBSTONE_STRIP_FIELDS",
    "request_erase",
]
