"""
Walk driver — Track 3 integration glue.

Connects the search policy to the existing walk infrastructure via
a thin driver loop. Keeps the policy decoupled from the actual
edge-traversal mechanics.

# Why a driver, not a method on SearchPolicy

The policy is a pure decision function. The driver is the loop:
  1. Ask the policy what to do next.
  2. Execute the directive (call `walk_outgoing`, fan out, alternate, etc.)
  3. Update the cluster pattern + budget + frame confidences.
  4. Repeat.
  5. On terminate, emit `WalkCompleted` audit event.

Keeping these separate means tests can unit-test the policy without
spinning up a real walk, and the walk machinery can evolve
independently.

# What this module provides

  - `WalkDriver` — orchestrates the policy/walk/audit loop.
  - `WalkOutcome` — the return shape (final state + the audit event).

The actual edge-traversal calls are stubs in this module — the
driver calls them but the production daemon plugs in real
implementations via the `WalkExecutor` Protocol. Tests pass a
fixture executor.

Spec ref: HANDLES_DESIGN.md v2 §"Search policy", IMPLEMENTATION_ROADMAP.md
§"Track 3", events.py §"Walk audit".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..log.client import LogClient, get_default_client
from ..schema.events import WalkCompleted
from ..schema.identifiers import make_event_id_log
from .cluster_pattern import ClusterTraversalPattern, Hop
from .frame import InterpretiveFrame
from .search_policy import (
    DirectiveKind,
    FrameConfidenceSummary,
    SearchBudget,
    SearchPolicy,
    StepDirective,
    summarize_frames,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Executor protocol
# =============================================================================


@dataclass
class StepOutcome:
    """What an executor returns after performing one directive."""

    hops_taken: list[Hop] = field(default_factory=list)
    """Hops the walk actually traversed during this step. Used to
    update the cluster pattern."""

    breadth_consumed: int = 0
    depth_consumed: int = 0
    """How much of the breadth/depth budget the step actually used."""

    frame_confidence_updates: dict[str, float] = field(default_factory=dict)
    """frame_id → new confidence after this step. The driver
    applies these to its frame list before the next decision."""

    note: str = ""


@runtime_checkable
class WalkExecutor(Protocol):
    """Pluggable executor that performs the policy's directives.

    Production daemons supply a real implementation that calls into
    `views.walk` and rankers. Tests supply a fixture executor that
    returns canned outcomes.
    """

    def execute_expand_breadth(
        self,
        N: int,
        frames: list[InterpretiveFrame],
    ) -> StepOutcome: ...

    def execute_commit_depth(
        self,
        frame_id: str,
        frames: list[InterpretiveFrame],
    ) -> StepOutcome: ...

    def execute_alternate(
        self,
        frame_a: str,
        frame_b: str,
        depth: int,
        frames: list[InterpretiveFrame],
    ) -> StepOutcome: ...


# =============================================================================
# Outcome
# =============================================================================


@dataclass
class WalkOutcome:
    """What `WalkDriver.run()` returns."""

    handle_id: str
    total_hops: int
    final_top_frame_id: str
    final_top_frame_confidence: float
    terminate_reason: str
    final_cluster_signature: str
    directive_trace: list[dict]
    walk_completed_event_id: str
    """The event_id of the appended WalkCompleted event. Empty if no
    log was provided / append failed."""


# =============================================================================
# Driver
# =============================================================================


MAX_DRIVER_STEPS = 256
"""Hard cap on driver iterations; the policy should normally
terminate well before this. Defense against infinite loops in
buggy executors."""


@dataclass
class WalkDriver:
    """Orchestrates the policy/walk/audit loop.

    Construction:
      driver = WalkDriver(policy=SearchPolicy.adaptive(),
                          executor=my_executor,
                          log_client=...)

    Run:
      outcome = driver.run(handle_id="...", query_hash="...",
                           frames=[...], budget=SearchBudget())
    """

    policy: SearchPolicy
    executor: WalkExecutor
    log_client: LogClient | None = None
    """If None, defaults to the process-wide log on `run()`. Pass
    `False`-y in tests where audit emission isn't desired."""

    cluster_window_size: int = 8
    confidence_history_size: int = 5
    """How many recent confidence values to keep per frame for the
    policy's history-aware checks."""

    def run(
        self,
        *,
        handle_id: str,
        query_hash: str,
        frames: list[InterpretiveFrame],
        budget: SearchBudget,
        cluster_pattern: ClusterTraversalPattern | None = None,
        emit_audit_event: bool = True,
    ) -> WalkOutcome:
        """Run the walk to completion.

        Returns when:
          - The policy emits a `terminate(...)` directive, OR
          - The budget is exhausted, OR
          - MAX_DRIVER_STEPS is hit (safety net).
        """
        cluster_pattern = cluster_pattern or ClusterTraversalPattern(
            window_size=self.cluster_window_size,
        )
        # Per-frame confidence trajectories
        history: dict[str, list[float]] = {
            f.frame_id: [f.confidence] for f in frames
        }
        directive_trace: list[dict] = []
        total_hops = 0
        terminate_reason = ""

        for step_idx in range(MAX_DRIVER_STEPS):
            summary = summarize_frames(frames, history=history)
            directive = self.policy.next_step(
                summary,
                budget,
                cluster_pattern=cluster_pattern,
            )
            directive_trace.append(self._serialize_directive(directive, step_idx))

            if directive.kind == DirectiveKind.TERMINATE:
                terminate_reason = directive.terminate_reason
                break

            outcome = self._execute(directive, frames)

            # Update cluster pattern from any hops the executor took
            for hop in outcome.hops_taken:
                cluster_pattern.add_hop(hop)
            total_hops += len(outcome.hops_taken)

            # Update budget
            budget.consume_hop(
                depth=outcome.depth_consumed,
                breadth=outcome.breadth_consumed,
            )

            # Apply frame-confidence updates from the executor
            for frame_id, new_conf in outcome.frame_confidence_updates.items():
                self._update_frame_confidence(frames, history, frame_id, new_conf)
        else:
            # Loop completed without break — driver hit the safety cap
            terminate_reason = "driver_step_cap"
            logger.warning(
                "WalkDriver hit MAX_DRIVER_STEPS=%d for handle %s",
                MAX_DRIVER_STEPS,
                handle_id,
            )

        # Final state
        sorted_frames = sorted(frames, key=lambda f: -f.confidence)
        if sorted_frames:
            top = sorted_frames[0]
            final_frame_id, final_conf = top.frame_id, top.confidence
        else:
            final_frame_id, final_conf = "", 0.0

        cluster_sig = cluster_pattern.cluster_signature()

        # Audit emission
        event_id = ""
        if emit_audit_event and self.log_client is not False:
            log = self.log_client or get_default_client()
            evt = WalkCompleted(
                event_id=make_event_id_log(),
                recorded_at=int(time.time() * 1000),
                actor="search_policy",
                handle_id=handle_id,
                query_hash=query_hash,
                directive_trace=directive_trace,
                total_hops=total_hops,
                final_top_frame_id=final_frame_id,
                final_top_frame_confidence=final_conf,
                terminate_reason=terminate_reason,
                final_cluster_signature=cluster_sig,
                completed_at_ms=int(time.time() * 1000),
            )
            result = log.append(evt)
            if result.accepted:
                event_id = evt.event_id

        return WalkOutcome(
            handle_id=handle_id,
            total_hops=total_hops,
            final_top_frame_id=final_frame_id,
            final_top_frame_confidence=final_conf,
            terminate_reason=terminate_reason,
            final_cluster_signature=cluster_sig,
            directive_trace=directive_trace,
            walk_completed_event_id=event_id,
        )

    # -------- Internals ------------------------------------------------------

    def _execute(
        self,
        directive: StepDirective,
        frames: list[InterpretiveFrame],
    ) -> StepOutcome:
        if directive.kind == DirectiveKind.EXPAND_BREADTH:
            return self.executor.execute_expand_breadth(
                directive.breadth_count, frames
            )
        if directive.kind == DirectiveKind.COMMIT_DEPTH:
            return self.executor.execute_commit_depth(
                directive.commit_frame_id, frames
            )
        if directive.kind == DirectiveKind.ALTERNATE:
            return self.executor.execute_alternate(
                directive.alternate_frame_a,
                directive.alternate_frame_b,
                directive.alternate_depth,
                frames,
            )
        # TERMINATE handled by caller; should never reach here
        raise ValueError(f"Unhandled directive kind: {directive.kind}")

    def _update_frame_confidence(
        self,
        frames: list[InterpretiveFrame],
        history: dict[str, list[float]],
        frame_id: str,
        new_conf: float,
    ) -> None:
        """Apply a confidence update; update history trail."""
        for f in frames:
            if f.frame_id == frame_id:
                f.confidence = new_conf
                break
        traj = history.setdefault(frame_id, [])
        traj.append(new_conf)
        if len(traj) > self.confidence_history_size:
            del traj[: len(traj) - self.confidence_history_size]

    @staticmethod
    def _serialize_directive(
        d: StepDirective,
        step_idx: int,
    ) -> dict:
        """Wire-format the directive for the audit trace.

        Free-form dict — schema can evolve without a migration.
        """
        out: dict = {
            "step": step_idx,
            "kind": d.kind.value,
            "rationale": d.rationale,
        }
        if d.kind == DirectiveKind.EXPAND_BREADTH:
            out["breadth_count"] = d.breadth_count
        elif d.kind == DirectiveKind.COMMIT_DEPTH:
            out["frame_id"] = d.commit_frame_id
        elif d.kind == DirectiveKind.ALTERNATE:
            out["frame_a"] = d.alternate_frame_a
            out["frame_b"] = d.alternate_frame_b
            out["depth"] = d.alternate_depth
        elif d.kind == DirectiveKind.TERMINATE:
            out["reason"] = d.terminate_reason
        return out


__all__ = [
    "MAX_DRIVER_STEPS",
    "StepOutcome",
    "WalkDriver",
    "WalkExecutor",
    "WalkOutcome",
]
