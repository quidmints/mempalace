"""
Multiplexer — top-level orchestration of jobs, view offsets, and consumers.

Per Part 10.1 / 10.2: the daemon is a multiplexer. Many concurrent
jobs run against the same log; each consumer sees a snapshot at its
current view offset (snapshot consistency per consumer).

This module owns:

  - Multiplexer: holds the Scheduler and a registry of ConsumerView
    bindings.
  - ConsumerView: the (consumer_id, current_view_offset) pair, with
    a backpressure indicator (lag vs log head).
  - submit_job_for_consumer(): convenience that allocates a job pinned
    to the consumer's current view offset.
  - emit_view_offset_advanced(): when a consumer's view has caught up
    past a new offset.

The scheduler is the work-execution mechanism; the multiplexer is the
*orchestration policy* layer above it (consumer routing, backpressure,
view eviction).

Spec ref: Part 10.1, 10.2, 10.4.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import ViewOffsetAdvanced
from ..schema.identifiers import make_event_id_log
from .jobs import Job, JobKind, JobOutcome
from .scheduler import ResourceBudget, Scheduler


# =============================================================================
# Consumer view binding
# =============================================================================


@dataclass
class ConsumerView:
    """A consumer's current view-offset state."""

    consumer_id: str
    current_offset: int = 0
    last_advance_ms: int = 0

    # Backpressure: lag = log_head_offset - current_offset
    last_known_lag: int = 0
    backpressure_threshold: int = 1024
    inactive_after_ms: int = 60 * 60 * 1000        # 1h inactive => evictable

    @property
    def is_backpressured(self) -> bool:
        return self.last_known_lag > self.backpressure_threshold

    def is_inactive(self, *, now_ms: int) -> bool:
        return (now_ms - self.last_advance_ms) > self.inactive_after_ms


# =============================================================================
# Multiplexer
# =============================================================================


class Multiplexer:
    """Top-level orchestration: schedule jobs, track per-consumer views,
    detect backpressure, evict inactive views."""

    def __init__(
        self,
        *,
        budget: ResourceBudget | None = None,
        log: LogClient | None = None,
        allow_preemption: bool = True,
    ) -> None:
        self._scheduler = Scheduler(
            budget=budget,
            log=log,
            allow_preemption=allow_preemption,
        )
        self._log = log
        self._consumers: dict[str, ConsumerView] = {}
        self._lock = threading.Lock()

    # ---- consumer registry --------------------------------------------------

    def register_consumer(
        self,
        consumer_id: str,
        *,
        backpressure_threshold: int = 1024,
        inactive_after_ms: int = 60 * 60 * 1000,
    ) -> ConsumerView:
        with self._lock:
            cv = self._consumers.get(consumer_id)
            if cv is None:
                cv = ConsumerView(
                    consumer_id=consumer_id,
                    last_advance_ms=int(time.time() * 1000),
                    backpressure_threshold=backpressure_threshold,
                    inactive_after_ms=inactive_after_ms,
                )
                self._consumers[consumer_id] = cv
            return cv

    def consumer(self, consumer_id: str) -> ConsumerView | None:
        with self._lock:
            return self._consumers.get(consumer_id)

    def consumers(self) -> list[ConsumerView]:
        with self._lock:
            return list(self._consumers.values())

    def evict_inactive(self, *, now_ms: int | None = None) -> list[str]:
        """Evict consumer views that have been inactive past their
        inactive_after_ms. Returns the evicted consumer_ids."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        with self._lock:
            evicted = [
                cid for cid, cv in self._consumers.items()
                if cv.is_inactive(now_ms=now_ms)
            ]
            for cid in evicted:
                del self._consumers[cid]
        return evicted

    # ---- view offset advancement -------------------------------------------

    def emit_view_offset_advanced(
        self,
        *,
        consumer_id: str,
        new_offset: int,
        log_head_offset: int,
    ) -> None:
        """Record that a consumer caught up to `new_offset`.

        Emits ViewOffsetAdvanced and updates the consumer view + lag.
        """
        log_client = self._log or get_default_client()
        now = int(time.time() * 1000)
        with self._lock:
            cv = self._consumers.get(consumer_id)
            if cv is None:
                cv = ConsumerView(consumer_id=consumer_id)
                self._consumers[consumer_id] = cv
            cv.current_offset = new_offset
            cv.last_advance_ms = now
            cv.last_known_lag = max(0, log_head_offset - new_offset)
        log_client.append(ViewOffsetAdvanced(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor=f"multiplex.consumer.{consumer_id}",
            consumer_id=consumer_id,
            new_offset=new_offset,
        ))

    def backpressured_consumers(self) -> list[ConsumerView]:
        with self._lock:
            return [cv for cv in self._consumers.values() if cv.is_backpressured]

    # ---- job submission -----------------------------------------------------

    def submit_job_for_consumer(
        self,
        *,
        consumer_id: str,
        kind: JobKind,
        parameters: dict[str, Any] | None = None,
        runner: Callable[[Job], dict[str, Any]] | None = None,
    ) -> Job:
        """Submit a job pinned to the consumer's current view offset.

        Snapshot consistency: the runner can observe the log up to
        `job.view_offset` only. Anything beyond that is invisible to
        this run.
        """
        cv = self.register_consumer(consumer_id)
        with self._lock:
            offset = cv.current_offset
        return self._scheduler.submit(
            kind=kind,
            consumer=consumer_id,
            parameters=parameters,
            runner=runner,
            view_offset=offset,
        )

    def submit_job(
        self,
        *,
        kind: JobKind,
        consumer: str,
        parameters: dict[str, Any] | None = None,
        runner: Callable[[Job], dict[str, Any]] | None = None,
        view_offset: int = 0,
    ) -> Job:
        """Submit a job without pinning to a registered consumer view."""
        return self._scheduler.submit(
            kind=kind,
            consumer=consumer,
            parameters=parameters,
            runner=runner,
            view_offset=view_offset,
        )

    # ---- run cycle ----------------------------------------------------------

    def run_one_step(self) -> JobOutcome | None:
        return self._scheduler.run_one_step()

    def run_until_idle(self, *, max_iterations: int = 1000) -> list[JobOutcome]:
        return self._scheduler.run_until_idle(max_iterations=max_iterations)

    def pending_count(self) -> int:
        return self._scheduler.pending_count()

    def running_count(self) -> int:
        return self._scheduler.running_count()

    def stop(self) -> None:
        self._scheduler.stop()


# =============================================================================
# Module-level singleton
# =============================================================================


_MUX: Multiplexer | None = None
_MUX_LOCK = threading.Lock()


def get_multiplexer() -> Multiplexer:
    global _MUX
    with _MUX_LOCK:
        if _MUX is None:
            _MUX = Multiplexer()
        return _MUX


def set_multiplexer(mux: Multiplexer) -> None:
    global _MUX
    with _MUX_LOCK:
        _MUX = mux


__all__ = [
    "ConsumerView",
    "Multiplexer",
    "get_multiplexer",
    "set_multiplexer",
]
