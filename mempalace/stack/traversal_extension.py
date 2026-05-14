"""
Traversal-extension hook for `mempalace.stack` Steps.

# The question this answers

The user asked, in the May 6 thread:

  > How do these [steps] not only consume traversal, but define
  > traversal as a feedback loop?

The base `Step` Protocol (see `mempalace.stack.step`) consumes
inputs and produces outputs. It doesn't, by default, mutate the
walk it's part of. This module adds the affordance.

# How feedback works

A `TraversalExtendingStep` declares one of three intents:

  - `CONSUMES`: read traversal state but don't mutate it (default;
    backwards-compatible with existing Steps).
  - `EXTENDS`: append hops to the cluster pattern; the policy reads
    the new pattern on the next iteration. The walk grows.
  - `BRANCHES`: spawn a parallel walk fragment with a copy of the
    current state. Useful for "what if" exploration.
  - `TERMINATES`: signal the walk should end after this step;
    subsequent steps in the stack don't run.

When a step runs, it gets a `TraversalState` handle (new in this
module) carrying the live HandleContext. Mutating the state through
the handle's typed methods produces the feedback loop: the step's
output influences what the policy decides next time.

# Example: clarification step (fill-in-the-blank)

A clarification step takes a partial query and produces N candidate
completions. The step BRANCHES — for each candidate, it spawns a
new walk fragment to evaluate it. The policy treats each fragment
as its own walk; the highest-scoring fragment is the answer.

# Example: deduplication pre-processor

The dedupe step reads the candidate set produced upstream. If two
candidates are near-duplicates of each other, it CONSUMES (reads,
emits a smaller set, doesn't touch traversal). If a duplicate
suggests the original walk was ambiguous (and a new branch would
disambiguate), it BRANCHES.

# Example: budget-exhausted terminator

A step that observes the budget is depleted EMITS a
`TraversalIntent.TERMINATES` and the stack stops cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .step import Step, StepResult

if TYPE_CHECKING:
    from ..handle.cluster_pattern import Hop
    from ..handle.context import HandleContext
    from .context import StackContext


# =============================================================================
# Traversal intents
# =============================================================================


class TraversalIntent(str, Enum):
    """How a step interacts with the walk it's part of."""

    CONSUMES = "consumes"
    EXTENDS = "extends"
    BRANCHES = "branches"
    TERMINATES = "terminates"


# =============================================================================
# TraversalState — the handle steps mutate
# =============================================================================


@dataclass
class TraversalMutation:
    """One change a step requested to make to the walk."""

    intent: TraversalIntent
    """What kind of mutation this is."""

    appended_hops: list["Hop"] = field(default_factory=list)
    """For EXTENDS: hops to add to the cluster pattern."""

    branch_seed: dict[str, Any] | None = None
    """For BRANCHES: seed state for the new walk fragment."""

    termination_reason: str = ""
    """For TERMINATES: why."""


@dataclass
class TraversalState:
    """Live handle a step uses to read and mutate traversal state.

    Construction:
      state = TraversalState(handle_context=ctx)

    Reading:
      state.cluster_signature
      state.frame_count
      state.is_pattern_stable()

    Mutating:
      state.append_hop(Hop(...))
      state.branch(seed={"variant": "what_if_X"})
      state.terminate(reason="budget_exhausted")

    All mutations are recorded in `mutations`; the framework applies
    them after the step returns. (Steps don't directly mutate the
    HandleContext; they request mutations, the framework decides
    whether to apply them — same shape as PolicyAdjustment in
    search_policy.)
    """

    handle_context: "HandleContext"
    mutations: list[TraversalMutation] = field(default_factory=list)

    # ---- read accessors ----

    @property
    def cluster_signature(self) -> str:
        return self.handle_context.cluster_signature()

    @property
    def frame_count(self) -> int:
        return len(self.handle_context.frames)

    @property
    def total_hops(self) -> int:
        return self.handle_context.total_hops

    def is_pattern_stable(self, *, min_hops: int = 4) -> bool:
        return self.handle_context.is_pattern_stable(min_hops=min_hops)

    # ---- mutation requests ----

    def append_hop(self, hop: "Hop") -> None:
        """Request: extend the walk with this hop."""
        self.mutations.append(TraversalMutation(
            intent=TraversalIntent.EXTENDS,
            appended_hops=[hop],
        ))

    def branch(self, *, seed: dict[str, Any]) -> None:
        """Request: spawn a parallel walk fragment.

        The seed dict carries whatever the framework needs to
        instantiate the new fragment. Typical contents:
          - "variant": a label so the auditor knows why this branch
            exists (e.g., "what_if_X", "completion_<n>").
          - "starting_node_id": where the new fragment begins.
          - any step-specific extras.
        """
        self.mutations.append(TraversalMutation(
            intent=TraversalIntent.BRANCHES,
            branch_seed=dict(seed),
        ))

    def terminate(self, *, reason: str) -> None:
        """Request: end the walk after this step."""
        self.mutations.append(TraversalMutation(
            intent=TraversalIntent.TERMINATES,
            termination_reason=reason,
        ))


# =============================================================================
# TraversalExtendingStep — Step that takes TraversalState
# =============================================================================


@runtime_checkable
class TraversalExtendingStep(Protocol):
    """A Step that can read AND mutate traversal state.

    Same as the base Step contract but with `run_with_traversal`
    instead of `run` so the framework can pass the TraversalState.

    Backwards-compatible: existing Steps that only do `run(ctx)`
    are unaffected. The framework checks for this Protocol and
    routes accordingly.
    """

    @property
    def name(self) -> str: ...

    @property
    def traversal_intent(self) -> TraversalIntent: ...

    def run_with_traversal(
        self,
        ctx: "StackContext",
        state: TraversalState,
    ) -> StepResult: ...


# =============================================================================
# Mutation application
# =============================================================================


def apply_mutations(
    handle_context: "HandleContext",
    mutations: list[TraversalMutation],
    *,
    spawn_branch: Any | None = None,  # Callable[[dict], None]
) -> dict[str, Any]:
    """Apply a step's traversal mutations to the carrier.

    Returns a summary dict for audit logging:
      {
        "extended_hops": int,
        "branched": int,
        "terminated": bool,
        "termination_reason": str,
      }

    `spawn_branch` is a caller-supplied callback for handling
    BRANCHES mutations. The framework can no-op branch requests
    (default), spawn parallel handle contexts, or queue them for
    deferred evaluation. Pass None to ignore branching.
    """
    summary = {
        "extended_hops": 0,
        "branched": 0,
        "terminated": False,
        "termination_reason": "",
    }
    for m in mutations:
        if m.intent == TraversalIntent.EXTENDS:
            for hop in m.appended_hops:
                handle_context.add_hop(hop)
                summary["extended_hops"] += 1
        elif m.intent == TraversalIntent.BRANCHES:
            if spawn_branch is not None and m.branch_seed is not None:
                spawn_branch(m.branch_seed)
            summary["branched"] += 1
        elif m.intent == TraversalIntent.TERMINATES:
            summary["terminated"] = True
            summary["termination_reason"] = m.termination_reason
            break  # no point processing later mutations
        # CONSUMES = no-op
    return summary


__all__ = [
    "TraversalExtendingStep",
    "TraversalIntent",
    "TraversalMutation",
    "TraversalState",
    "apply_mutations",
]
