"""
Adaptive search policy — Track 3.

Per HANDLES_DESIGN.md v2 §"Search policy — breadth ↔ depth interleaving":
the v1 traversal was static ("walk K hops, update context, repeat");
the substrate is bottom-up, so the search shouldn't be top-down-static.

# What this is

A policy that decides at each hop what to do next. Four directives:

  - `expand_breadth(N)` — pull more candidates side-by-side
  - `commit_depth(frame_id)` — depth-first into one dominant frame
  - `alternate(frame_a, frame_b, depth)` — alternating depth-K walks
    for direct comparison
  - `terminate(reason)` — done, surface what we have

The decision is data-dependent, examining frame confidences, the
cluster traversal pattern, and the remaining budget. The static
heuristic ships in this turn (Track 3); the learned-adjustment
matrix is a follow-on (the schema + hook are in place).

# Why the rules

Per the design spec:

  - **High dispersion across frames + budget remaining**:
    expand_breadth — the walk is exploring; pull more candidates.
  - **One dominant frame (confidence > threshold) + others fading**:
    commit_depth — stop spending budget on losing frames.
  - **Two close-confidence frames + dwindling budget**: alternate.
    Compare the converged endpoints.
  - **Walk is stuck (cluster pattern stable, no confidence change)**:
    terminate.

# Why this lives in `handle/`, not in `views/walk.py`

The walk module is graph mechanics — follow edges, return reachable
nodes. The search policy is *how the walk should behave* given the
query's accumulated routing intelligence. Different layers; the
policy consumes the walk module rather than embedding in it.

Spec ref: HANDLES_DESIGN.md v2 §"Search policy", Track 3 of
IMPLEMENTATION_ROADMAP.md.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .frame import InterpretiveFrame
    from .cluster_pattern import ClusterTraversalPattern
    from .context import HandleContext


# =============================================================================
# StepDirective — sum type
# =============================================================================


class DirectiveKind(str, enum.Enum):
    EXPAND_BREADTH = "expand_breadth"
    COMMIT_DEPTH = "commit_depth"
    ALTERNATE = "alternate"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class StepDirective:
    """What `SearchPolicy.next_step()` returns.

    Sum type with `kind` discriminator. Constructors below build
    each variant; the data is in the per-variant fields.

    Frozen + hashable so directives can flow into walk_completed
    audit events without copying.
    """

    kind: DirectiveKind

    # EXPAND_BREADTH
    breadth_count: int = 0
    """Number of candidates to fan out in this step. Set when
    kind == EXPAND_BREADTH."""

    # COMMIT_DEPTH
    commit_frame_id: str = ""
    """Frame to commit depth-first to. Set when kind == COMMIT_DEPTH."""

    # ALTERNATE
    alternate_frame_a: str = ""
    alternate_frame_b: str = ""
    alternate_depth: int = 0
    """How many depth-K hops to take into each frame before
    alternating. Set when kind == ALTERNATE."""

    # TERMINATE
    terminate_reason: str = ""
    """Why the walk is done. Audit + debugging."""

    # Common metadata
    rationale: str = ""
    """Free-text explanation of the policy's reasoning. For audit
    + UI. Not consumed by the walk."""

    @classmethod
    def expand_breadth(
        cls,
        N: int,
        *,
        rationale: str = "",
    ) -> "StepDirective":
        return cls(
            kind=DirectiveKind.EXPAND_BREADTH,
            breadth_count=N,
            rationale=rationale,
        )

    @classmethod
    def commit_depth(
        cls,
        frame_id: str,
        *,
        rationale: str = "",
    ) -> "StepDirective":
        return cls(
            kind=DirectiveKind.COMMIT_DEPTH,
            commit_frame_id=frame_id,
            rationale=rationale,
        )

    @classmethod
    def alternate(
        cls,
        frame_a: str,
        frame_b: str,
        *,
        depth: int = 2,
        rationale: str = "",
    ) -> "StepDirective":
        return cls(
            kind=DirectiveKind.ALTERNATE,
            alternate_frame_a=frame_a,
            alternate_frame_b=frame_b,
            alternate_depth=depth,
            rationale=rationale,
        )

    @classmethod
    def terminate(
        cls,
        *,
        reason: str = "",
        rationale: str = "",
    ) -> "StepDirective":
        return cls(
            kind=DirectiveKind.TERMINATE,
            terminate_reason=reason,
            rationale=rationale,
        )


# =============================================================================
# SearchBudget
# =============================================================================


DEFAULT_HOP_BUDGET = 32
DEFAULT_DEPTH_BUDGET = 16
DEFAULT_BREADTH_BUDGET = 64


@dataclass
class SearchBudget:
    """How much the policy can still spend.

    Three independent budgets:
      - `hops_remaining` — total hops the walk can still take.
      - `depth_remaining` — max depth in any single commit_depth.
      - `breadth_remaining` — max candidates that can still be
        fanned out in expand_breadth steps.

    Dwindling budgets push the policy toward terminate.
    """

    hops_remaining: int = DEFAULT_HOP_BUDGET
    depth_remaining: int = DEFAULT_DEPTH_BUDGET
    breadth_remaining: int = DEFAULT_BREADTH_BUDGET

    def is_dwindling(self, *, threshold_ratio: float = 0.25) -> bool:
        """True if any budget has fallen below the threshold ratio
        of its starting value.

        We don't track the original budget; the threshold is a
        coarse heuristic against the defaults. Callers with custom
        starting budgets pass their own threshold via the policy
        config.
        """
        return (
            self.hops_remaining < int(DEFAULT_HOP_BUDGET * threshold_ratio)
            or self.depth_remaining < int(DEFAULT_DEPTH_BUDGET * threshold_ratio)
            or self.breadth_remaining < int(DEFAULT_BREADTH_BUDGET * threshold_ratio)
        )

    def is_exhausted(self) -> bool:
        """True if no more steps are possible at all."""
        return (
            self.hops_remaining <= 0
            or (self.depth_remaining <= 0 and self.breadth_remaining <= 0)
        )

    def consume_hop(self, *, depth: int = 0, breadth: int = 0) -> None:
        """Mutate to subtract from budgets."""
        self.hops_remaining = max(0, self.hops_remaining - 1)
        if depth:
            self.depth_remaining = max(0, self.depth_remaining - depth)
        if breadth:
            self.breadth_remaining = max(0, self.breadth_remaining - breadth)


# =============================================================================
# Frame-confidence summary used by the policy
# =============================================================================


@dataclass(frozen=True)
class FrameConfidenceSummary:
    """What the policy needs to know about the current frames.

    Pre-extracted from the HandleContext rather than passed wholesale,
    so the policy stays decoupled from the full HandleContext shape
    (which is still under construction).
    """

    frames_with_confidences: tuple[tuple[str, float], ...]
    """list of (frame_id, confidence) — must be non-empty for the
    policy to decide between commit/alternate. Empty triggers
    expand_breadth as default exploration."""

    confidence_history_by_frame: dict[str, tuple[float, ...]] = field(
        default_factory=dict,
    )
    """Per-frame confidence over recent hops. Used to detect "fading"
    (confidence trending down) vs "stable" (no change)."""


def summarize_frames(
    frames: list["InterpretiveFrame"],
    *,
    history: dict[str, list[float]] | None = None,
) -> FrameConfidenceSummary:
    """Build a `FrameConfidenceSummary` from a list of frames.

    The `history` arg, when provided, supplies recent-hops
    confidence trajectories per frame. Tests + callers without
    history pass None / empty.
    """
    pairs = tuple(
        (f.frame_id, f.confidence) for f in frames
    )
    history_dict = {}
    if history is not None:
        history_dict = {k: tuple(v) for k, v in history.items()}
    return FrameConfidenceSummary(
        frames_with_confidences=pairs,
        confidence_history_by_frame=history_dict,
    )


# =============================================================================
# Adjustment hook
# =============================================================================


@runtime_checkable
class PolicyAdjustment(Protocol):
    """Per-user learned adjustments hook.

    Track 3's deferred learned-policy piece. The static heuristic
    ships now; this Protocol lets a future implementation inject
    learned biases without changing the heuristic code.

    A real implementation (deferred) consumes `walk_completed`
    audit events to learn which interleaving patterns produce
    high-quality results for this user.
    """

    def adjust_directive(
        self,
        proposed: StepDirective,
        summary: FrameConfidenceSummary,
        budget: SearchBudget,
    ) -> StepDirective:
        """Optionally substitute a different directive based on
        learned patterns. Returns `proposed` unchanged for the
        no-op base case."""
        ...


class NoopAdjustment:
    """The default — pass-through adjustment that always returns the
    proposed directive unchanged. Production code uses a real
    `PolicyAdjustment` once the learning loop ships."""

    def adjust_directive(
        self,
        proposed: StepDirective,
        summary: FrameConfidenceSummary,
        budget: SearchBudget,
    ) -> StepDirective:
        return proposed


# =============================================================================
# SearchPolicy
# =============================================================================


# Heuristic constants. Exposed for tuning / testing.
DOMINANT_FRAME_THRESHOLD = 0.7
"""Confidence above which a frame is "dominant" and the policy
commits depth into it."""

CLOSE_FRAMES_DELTA = 0.1
"""Two frames within this confidence delta are considered "close"
and trigger alternate-style comparison rather than commit."""

HIGH_DISPERSION_STDDEV = 0.15
"""Frame-confidence stddev above this is considered "high
dispersion" (frames are still spreading; explore more)."""

DEFAULT_BREADTH_FANOUT = 8
"""Default `expand_breadth(N)` when no other signal pushes the
fanout count."""

DEFAULT_ALTERNATE_DEPTH = 2
"""Default depth for `alternate` directives."""

STUCK_PATTERN_HOPS = 4
"""Cluster pattern stable for this many hops → terminate."""


@dataclass
class SearchPolicy:
    """Adaptive search policy.

    Build via `SearchPolicy.adaptive()` for the standard heuristic.
    Build directly with explicit constants for tests.
    """

    dominant_threshold: float = DOMINANT_FRAME_THRESHOLD
    close_delta: float = CLOSE_FRAMES_DELTA
    dispersion_stddev: float = HIGH_DISPERSION_STDDEV
    breadth_fanout: int = DEFAULT_BREADTH_FANOUT
    alternate_depth: int = DEFAULT_ALTERNATE_DEPTH
    stuck_hops: int = STUCK_PATTERN_HOPS

    adjustment: PolicyAdjustment = field(default_factory=NoopAdjustment)
    """Hook for learned adjustments. Defaults to no-op."""

    @classmethod
    def adaptive(cls) -> "SearchPolicy":
        """Default policy: data-dependent interleaving. The four
        rules from the design.
        """
        return cls()

    def next_step(
        self,
        summary: FrameConfidenceSummary,
        budget: SearchBudget,
        *,
        cluster_pattern: "ClusterTraversalPattern | None" = None,
    ) -> StepDirective:
        """Decide what kind of step to take next.

        Decision tree (rules from HANDLES_DESIGN.md v2):

          1. Budget exhausted → terminate("budget_exhausted").
          2. Cluster pattern stuck → terminate("walk_stuck").
          3. No frames yet → expand_breadth (exploration default).
          4. One frame dominates with budget remaining and no close
             second → commit_depth into the dominant frame.
          5. Two frames are close in confidence + budget dwindling
             → alternate to compare directly.
          6. High dispersion + budget healthy → expand_breadth.
          7. Otherwise → expand_breadth as the safe default.

        After the heuristic produces a directive, the adjustment
        hook gets a chance to override (no-op by default).
        """
        proposed = self._heuristic(summary, budget, cluster_pattern)
        return self.adjustment.adjust_directive(proposed, summary, budget)

    def next_step_for_context(
        self,
        ctx: "HandleContext",
        budget: SearchBudget,
    ) -> StepDirective:
        """Convenience: derive `summary` + `cluster_pattern` from a
        HandleContext and call `next_step`.

        This is the integration point between the carrier (HandleContext)
        and the policy. The policy stays decoupled from the full
        HandleContext shape (still computes only on the pre-extracted
        fields it needs), but callers no longer have to extract them
        manually.
        """
        summary = summarize_frames(ctx.frames)
        return self.next_step(
            summary, budget, cluster_pattern=ctx.cluster_pattern,
        )

    # -------- Internals ------------------------------------------------------

    def _heuristic(
        self,
        summary: FrameConfidenceSummary,
        budget: SearchBudget,
        cluster_pattern: "ClusterTraversalPattern | None",
    ) -> StepDirective:
        # Rule 1 — budget exhausted
        if budget.is_exhausted():
            return StepDirective.terminate(
                reason="budget_exhausted",
                rationale="No remaining budget for further hops.",
            )

        # Rule 2 — walk stuck
        if cluster_pattern is not None and cluster_pattern.is_stable(
            min_hops=self.stuck_hops
        ):
            return StepDirective.terminate(
                reason="walk_stuck",
                rationale=(
                    f"Cluster pattern stable for {self.stuck_hops}+ hops; "
                    "no signal to continue."
                ),
            )

        # Rule 3 — no frames; explore
        frames = list(summary.frames_with_confidences)
        if not frames:
            return StepDirective.expand_breadth(
                self.breadth_fanout,
                rationale="No frames yet; exploration default.",
            )

        # Sort frames by confidence descending
        frames.sort(key=lambda kv: -kv[1])
        top_id, top_conf = frames[0]

        # Detect "fading others": frames whose history shows downward
        # trend.  This guards rule 4 from triggering when the second
        # frame is climbing back.
        others_fading = self._others_are_fading(summary, top_id)

        # Rule 4 — one dominant frame, others fading
        if (
            top_conf >= self.dominant_threshold
            and others_fading
            and not budget.is_exhausted()
        ):
            return StepDirective.commit_depth(
                top_id,
                rationale=(
                    f"Frame {top_id} dominates "
                    f"(confidence={top_conf:.2f} ≥ {self.dominant_threshold:.2f}) "
                    "and others are fading; commit depth-first."
                ),
            )

        # Rule 5 — two close frames + dwindling budget
        if len(frames) >= 2:
            second_id, second_conf = frames[1]
            if (
                top_conf - second_conf <= self.close_delta
                and budget.is_dwindling()
            ):
                return StepDirective.alternate(
                    top_id,
                    second_id,
                    depth=self.alternate_depth,
                    rationale=(
                        f"Frames {top_id} ({top_conf:.2f}) and "
                        f"{second_id} ({second_conf:.2f}) close; "
                        "budget dwindling — alternate to compare."
                    ),
                )

        # Rule 6 — high dispersion + healthy budget
        confidences = [c for _, c in frames]
        if len(confidences) >= 2:
            stddev = statistics.stdev(confidences)
            if stddev >= self.dispersion_stddev and not budget.is_dwindling():
                return StepDirective.expand_breadth(
                    self.breadth_fanout,
                    rationale=(
                        f"High frame-confidence dispersion (stddev={stddev:.2f} "
                        f"≥ {self.dispersion_stddev:.2f}); budget healthy. "
                        "Expand breadth."
                    ),
                )

        # Rule 7 — fallback: expand_breadth as safe default
        return StepDirective.expand_breadth(
            self.breadth_fanout,
            rationale="Default exploration step.",
        )

    @staticmethod
    def _others_are_fading(
        summary: FrameConfidenceSummary,
        top_frame_id: str,
    ) -> bool:
        """True if the non-top frames show a decreasing confidence
        trend across the recent history. Returns True when there's
        no history (so the dominant-frame rule can fire on first
        evaluation if dominance is strong; the safer interpretation
        is "absent contrary evidence, the heuristic trusts the
        signal")."""
        history = summary.confidence_history_by_frame
        if not history:
            return True

        non_top = [(k, v) for k, v in history.items() if k != top_frame_id]
        if not non_top:
            # No other frames at all → trivially "fading" in the
            # sense that there's nothing to compete
            return True

        for _frame_id, traj in non_top:
            if len(traj) < 2:
                # Not enough history to say
                continue
            # Compare last value to mean of earlier; if last > mean
            # by a meaningful margin, this frame is climbing, not
            # fading.
            last = traj[-1]
            earlier_mean = sum(traj[:-1]) / len(traj[:-1])
            if last > earlier_mean + 0.05:
                return False

        return True


__all__ = [
    "CLOSE_FRAMES_DELTA",
    "DEFAULT_ALTERNATE_DEPTH",
    "DEFAULT_BREADTH_BUDGET",
    "DEFAULT_BREADTH_FANOUT",
    "DEFAULT_DEPTH_BUDGET",
    "DEFAULT_HOP_BUDGET",
    "DOMINANT_FRAME_THRESHOLD",
    "DirectiveKind",
    "FrameConfidenceSummary",
    "HIGH_DISPERSION_STDDEV",
    "NoopAdjustment",
    "PolicyAdjustment",
    "STUCK_PATTERN_HOPS",
    "SearchBudget",
    "SearchPolicy",
    "StepDirective",
    "summarize_frames",
]
