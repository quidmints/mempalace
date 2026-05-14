"""
Job lifecycle.

Per Part 10.2 / R3 §10: concurrent jobs against the same log; job state
is itself in the log via JobScheduled / JobStarted / JobProgress /
JobCompleted / JobFailed / JobPaused / JobResumed events.

This module owns:

  - JobKind: typed enum of well-known job kinds
  - Job: a scheduled or running job with metadata and view offset
  - JobOutcome: terminal result (success or failure)
  - submit_job() helper that emits JobScheduled and returns the Job

Spec ref: Part 10.2.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    JobCompleted,
    JobFailed,
    JobPaused,
    JobProgress,
    JobResumed,
    JobScheduled,
    JobStarted,
)
from ..schema.identifiers import make_event_id_log, make_job_id


# =============================================================================
# Job kinds
# =============================================================================


class JobKind(str, Enum):
    """Well-known job kinds."""

    MINER_CLASS1 = "miner.class1"            # streaming
    MINER_CLASS2 = "miner.class2"            # periodic
    MINER_CLASS3 = "miner.class3"            # schema-induction
    SIGNATURE_EXTRACT = "signature.extract"  # signature snapshot extraction
    TRANSITION_CACHE = "derived.transition_cache"
    FOYER_RENDER = "derived.foyer_render"
    REALTIME_INDEX = "derived.realtime_index"
    VELOCITY_BATCH = "features.velocity_batch"
    RETRIEVE = "retrieve.realtime"
    SANDBOX_MATCH = "federate.sandbox_match"
    RECONCILE_EMBEDDINGS = "embed.reconcile"
    CANONICALIZE = "canonicalize.pass"


# Priority order from Part 10.3 (lower = higher priority)
KIND_PRIORITY: dict[JobKind, int] = {
    JobKind.RETRIEVE: 1,
    JobKind.MINER_CLASS1: 2,
    JobKind.SANDBOX_MATCH: 3,
    JobKind.FOYER_RENDER: 4,
    JobKind.MINER_CLASS2: 5,
    JobKind.SIGNATURE_EXTRACT: 6,
    JobKind.MINER_CLASS3: 7,
    JobKind.TRANSITION_CACHE: 8,
    JobKind.RECONCILE_EMBEDDINGS: 9,
    JobKind.REALTIME_INDEX: 4,
    JobKind.VELOCITY_BATCH: 6,
    JobKind.CANONICALIZE: 7,
}


# =============================================================================
# Job state
# =============================================================================


class JobStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    PREEMPTED = "preempted"


@dataclass
class Job:
    """A unit of work scheduled against the multiplexer."""

    job_id: str
    kind: JobKind
    consumer: str                                  # logical consumer name
    parameters: dict[str, Any] = field(default_factory=dict)

    status: JobStatus = JobStatus.SCHEDULED
    scheduled_at_ms: int = 0
    started_at_ms: int = 0
    completed_at_ms: int = 0

    # Snapshot consistency: each job runs against a specific view offset
    view_offset: int = 0

    # Optional callback to actually run the work; takes the job, returns
    # an outputs dict on success or raises on failure.
    runner: Callable[["Job"], dict[str, Any]] | None = None

    # Last reported progress in [0, 1]
    progress: float = 0.0

    # Number of preemption resumes (for telemetry)
    resume_count: int = 0

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 99)


@dataclass
class JobOutcome:
    """Terminal result of a job."""

    job_id: str
    success: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    error_kind: str = ""
    error_message: str = ""
    started_at_ms: int = 0
    completed_at_ms: int = 0


# =============================================================================
# Submission / progress / terminal helpers
# =============================================================================


_LOCK = threading.Lock()


def submit_job(
    *,
    kind: JobKind,
    consumer: str,
    parameters: dict[str, Any] | None = None,
    runner: Callable[[Job], dict[str, Any]] | None = None,
    view_offset: int = 0,
    log: LogClient | None = None,
) -> Job:
    """Build a Job and emit JobScheduled. Returns the Job (status SCHEDULED).

    Also opens a distributed-lifecycle batch keyed on `job_id`.
    `mark_completed`/`mark_failed` close it. If the process crashes
    between schedule and complete/fail, recovery sees an open batch
    for `multiplex.jobs:{job_id}`.
    """
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    job_id = make_job_id()
    job = Job(
        job_id=job_id,
        kind=kind,
        consumer=consumer,
        parameters=dict(parameters or {}),
        scheduled_at_ms=now,
        view_offset=view_offset,
        runner=runner,
    )
    log_client.open_batch(
        "multiplex.jobs",
        job_id,
        input_summary={"kind": kind.value, "consumer": consumer},
        actor="multiplex.scheduler",
    )
    log_client.append(JobScheduled(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.scheduler",
        job_id=job_id,
        job_kind=kind.value,
        consumer=consumer,
        parameters=dict(parameters or {}),
        batch_id=job_id,
    ))
    return job


def mark_started(job: Job, *, log: LogClient | None = None) -> None:
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    with _LOCK:
        job.status = JobStatus.RUNNING
        job.started_at_ms = now
    log_client.append(JobStarted(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        batch_id=job.job_id,
    ))


def report_progress(
    job: Job, fraction: float, *, note: str = "", log: LogClient | None = None
) -> None:
    log_client = log or get_default_client()
    fraction = max(0.0, min(1.0, fraction))
    now = int(time.time() * 1000)
    with _LOCK:
        job.progress = fraction
    log_client.append(JobProgress(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        progress_fraction=fraction,
        note=note,
        batch_id=job.job_id,
    ))


def mark_completed(
    job: Job,
    outputs: dict[str, Any],
    *,
    log: LogClient | None = None,
) -> JobOutcome:
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    with _LOCK:
        job.status = JobStatus.COMPLETED
        job.completed_at_ms = now
        job.progress = 1.0
    log_client.append(JobCompleted(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        outputs=dict(outputs),
        batch_id=job.job_id,
    ))
    # Close the distributed-lifecycle batch (success).
    log_client.close_batch(
        "multiplex.jobs", job.job_id,
        actor=f"multiplex.runner.{job.kind.value}",
    )
    return JobOutcome(
        job_id=job.job_id,
        success=True,
        outputs=dict(outputs),
        started_at_ms=job.started_at_ms,
        completed_at_ms=now,
    )


def mark_failed(
    job: Job,
    error_kind: str,
    error_message: str,
    *,
    log: LogClient | None = None,
) -> JobOutcome:
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    with _LOCK:
        job.status = JobStatus.FAILED
        job.completed_at_ms = now
    log_client.append(JobFailed(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        error_kind=error_kind,
        error_message=error_message,
        batch_id=job.job_id,
    ))
    # Close the distributed-lifecycle batch (failure → BatchAborted).
    log_client.abort_batch(
        "multiplex.jobs", job.job_id,
        reason=f"job_failed:{error_kind}",
        detail=error_message,
        actor=f"multiplex.runner.{job.kind.value}",
    )
    return JobOutcome(
        job_id=job.job_id,
        success=False,
        error_kind=error_kind,
        error_message=error_message,
        started_at_ms=job.started_at_ms,
        completed_at_ms=now,
    )


def mark_paused(job: Job, *, reason: str = "", log: LogClient | None = None) -> None:
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    with _LOCK:
        job.status = JobStatus.PAUSED
    log_client.append(JobPaused(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        reason=reason,
        batch_id=job.job_id,
    ))


def mark_resumed(job: Job, *, log: LogClient | None = None) -> None:
    log_client = log or get_default_client()
    now = int(time.time() * 1000)
    with _LOCK:
        job.status = JobStatus.RUNNING
        job.resume_count += 1
    log_client.append(JobResumed(
        event_id=make_event_id_log(now),
        recorded_at=now,
        actor=f"multiplex.runner.{job.kind.value}",
        job_id=job.job_id,
        batch_id=job.job_id,
    ))


__all__ = [
    "Job",
    "JobKind",
    "JobOutcome",
    "JobStatus",
    "KIND_PRIORITY",
    "mark_completed",
    "mark_failed",
    "mark_paused",
    "mark_resumed",
    "mark_started",
    "report_progress",
    "submit_job",
]
