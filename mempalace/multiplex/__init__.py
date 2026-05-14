"""
mempalace.multiplex — concurrent job orchestration.

Per Part 10: many concurrent jobs run against the same log; each
consumer sees a snapshot at its current view offset (snapshot
consistency per consumer); job state is itself in the log.

Submodules:

  jobs       — Job dataclass, lifecycle helpers, JobKind enum,
               KIND_PRIORITY table.
  scheduler  — Priority-aware job scheduler with optional preemption.
  mux        — Multiplexer that owns the scheduler + consumer-view
               registry + backpressure detection + view eviction.

Spec ref: Part 10.
"""

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
    report_progress,
    submit_job,
)
from .mux import (
    ConsumerView,
    Multiplexer,
    get_multiplexer,
    set_multiplexer,
)
from .scheduler import ResourceBudget, Scheduler

__all__ = [
    "ConsumerView",
    "Job",
    "JobKind",
    "JobOutcome",
    "JobStatus",
    "KIND_PRIORITY",
    "Multiplexer",
    "ResourceBudget",
    "Scheduler",
    "get_multiplexer",
    "mark_completed",
    "mark_failed",
    "mark_paused",
    "mark_resumed",
    "mark_started",
    "report_progress",
    "set_multiplexer",
    "submit_job",
]
