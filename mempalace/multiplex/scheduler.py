"""
Job scheduler.

Per Part 10.3: picks scheduled jobs in priority order, runs them
(possibly preempting lower-priority work), and feeds the lifecycle
events through jobs.py helpers.

Resource-aware in spirit, but resource enforcement is per-platform and
not in scope for this layer. The scheduler exposes hooks the daemon
can wire to a real CPU/GPU/IO budget when it runs.

Concurrency:

  - submit() puts a job in the SCHEDULED queue.
  - The mux loop picks the highest-priority SCHEDULED job that fits
    the current resource budget and dispatches it.
  - Higher-priority jobs can preempt currently-running lower-priority
    jobs; preempted jobs are paused (and resumed once the higher-
    priority job completes).

This module ships an in-process scheduler. Production wiring uses the
same interface against the daemon's actual scheduler.

Spec ref: Part 10.3.
"""

from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from .jobs import (
    Job,
    JobKind,
    JobOutcome,
    JobStatus,
    KIND_PRIORITY,
    mark_completed,
    mark_failed,
    mark_paused,
    mark_resumed,
    mark_started,
    submit_job,
)


# =============================================================================
# Resource budget
# =============================================================================


@dataclass
class ResourceBudget:
    """Coarse budget the scheduler will respect.

    Real enforcement is platform-specific; this struct is what the
    scheduler consults before dispatching a job. The daemon tunes
    these numbers based on the host's actual capacity and battery.
    """

    max_concurrent_jobs: int = 8
    max_concurrent_per_kind: dict[JobKind, int] = field(default_factory=dict)
    cpu_budget_units: float = 1.0          # 1.0 = full host
    gpu_budget_units: float = 0.0          # 0.0 = no GPU available
    io_budget_units: float = 1.0


# =============================================================================
# Scheduler
# =============================================================================


@dataclass(order=True)
class _Pending:
    """Heap entry: (priority, scheduled_at_ms, job)."""

    priority: int
    scheduled_at_ms: int
    seq: int                                # tiebreaker for stable order
    job: Job = field(compare=False)


class Scheduler:
    """Priority-aware job scheduler with optional preemption."""

    def __init__(
        self,
        *,
        budget: ResourceBudget | None = None,
        log: LogClient | None = None,
        allow_preemption: bool = True,
    ) -> None:
        self._budget = budget or ResourceBudget()
        self._log = log
        self._allow_preemption = allow_preemption

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        self._pending: list[_Pending] = []
        self._running: dict[str, Job] = {}     # job_id -> Job
        self._kind_counts: dict[JobKind, int] = {}

        self._seq = 0
        self._stopping = False

    # ---- submission --------------------------------------------------------

    def submit(
        self,
        *,
        kind: JobKind,
        consumer: str,
        parameters: dict[str, Any] | None = None,
        runner: Callable[[Job], dict[str, Any]] | None = None,
        view_offset: int = 0,
    ) -> Job:
        """Submit a job. Emits JobScheduled and queues it for dispatch."""
        job = submit_job(
            kind=kind,
            consumer=consumer,
            parameters=parameters,
            runner=runner,
            view_offset=view_offset,
            log=self._log,
        )
        with self._cond:
            self._seq += 1
            heapq.heappush(
                self._pending,
                _Pending(
                    priority=job.priority,
                    scheduled_at_ms=job.scheduled_at_ms,
                    seq=self._seq,
                    job=job,
                ),
            )
            self._cond.notify_all()
        return job

    # ---- inspection --------------------------------------------------------

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def running_jobs(self) -> list[Job]:
        with self._lock:
            return list(self._running.values())

    # ---- dispatch ----------------------------------------------------------

    def _can_dispatch(self, job: Job) -> tuple[bool, str]:
        if len(self._running) >= self._budget.max_concurrent_jobs:
            # Check whether preemption could free a slot
            if not self._allow_preemption:
                return False, "max_concurrent_jobs reached"
            # find lower-priority running job
            preempt_target = self._find_preempt_target(job.priority)
            if preempt_target is None:
                return False, "no preempt target available"
        per_kind_cap = self._budget.max_concurrent_per_kind.get(job.kind)
        if per_kind_cap is not None:
            if self._kind_counts.get(job.kind, 0) >= per_kind_cap:
                return False, f"per-kind cap reached for {job.kind.value}"
        return True, ""

    def _find_preempt_target(self, incoming_priority: int) -> Job | None:
        """Find the lowest-priority running job that's a worse fit
        than the incoming job (higher number = lower priority)."""
        worst: Job | None = None
        worst_priority = incoming_priority
        for job in self._running.values():
            if job.priority > worst_priority:
                worst = job
                worst_priority = job.priority
        return worst

    def _preempt(self, job: Job) -> None:
        """Pause a running job (caller holds the lock)."""
        del self._running[job.job_id]
        self._kind_counts[job.kind] = self._kind_counts.get(job.kind, 0) - 1
        # Re-queue so it can resume later
        self._seq += 1
        heapq.heappush(
            self._pending,
            _Pending(
                priority=job.priority,
                scheduled_at_ms=int(time.time() * 1000),
                seq=self._seq,
                job=job,
            ),
        )
        # Pause event
        mark_paused(job, reason="preempted_by_higher_priority", log=self._log)
        job.status = JobStatus.PREEMPTED

    def _dispatch_one(self) -> Job | None:
        """Pop the highest-priority pending job and start it.

        Caller must NOT hold the lock; this method handles its own locking.
        """
        with self._cond:
            if not self._pending:
                return None
            top = heapq.heappop(self._pending)
            job = top.job

            # Resource check
            ok, reason = self._can_dispatch(job)
            if not ok:
                if self._allow_preemption:
                    target = self._find_preempt_target(job.priority)
                    if target is not None:
                        self._preempt(target)
                    else:
                        # cannot preempt; put back
                        heapq.heappush(self._pending, top)
                        return None
                else:
                    heapq.heappush(self._pending, top)
                    return None

            # Mark running before releasing the lock
            self._running[job.job_id] = job
            self._kind_counts[job.kind] = self._kind_counts.get(job.kind, 0) + 1

        # Outside the lock now
        if job.status == JobStatus.PREEMPTED:
            mark_resumed(job, log=self._log)
        else:
            mark_started(job, log=self._log)
        return job

    def _finish(self, job: Job) -> None:
        with self._lock:
            self._running.pop(job.job_id, None)
            self._kind_counts[job.kind] = max(
                0, self._kind_counts.get(job.kind, 0) - 1
            )
            self._cond.notify_all()

    def run_one_step(self) -> JobOutcome | None:
        """Run a single dispatch + execute cycle synchronously.

        Returns the JobOutcome for the dispatched job, or None if none
        could be dispatched.
        """
        job = self._dispatch_one()
        if job is None:
            return None
        try:
            if job.runner is None:
                outcome = mark_failed(
                    job, "no_runner", "job has no runner attached", log=self._log
                )
            else:
                outputs = job.runner(job)
                outcome = mark_completed(job, outputs, log=self._log)
        except Exception as e:  # noqa: BLE001
            outcome = mark_failed(
                job, type(e).__name__, str(e), log=self._log
            )
        finally:
            self._finish(job)
        return outcome

    def run_until_idle(self, *, max_iterations: int = 1000) -> list[JobOutcome]:
        """Run jobs until no more pending work; returns all outcomes.

        Iteration cap is a safety against runaway re-submission.
        """
        outcomes: list[JobOutcome] = []
        for _ in range(max_iterations):
            outcome = self.run_one_step()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # ---- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()


__all__ = [
    "ResourceBudget",
    "Scheduler",
]
