"""
Question qualifier — decides the mix of local inference and remote
(Claude API) calls for a given question.

# What this module is

Per the design conversation: a question qualifier that takes a
prediction-market question (or any other inference target) and
produces a `ResolutionPlan` whose steps mix:

  - Local-substrate inference (cheap, private, bounded reasoning).
  - Remote LLM calls (broader knowledge, opaque reasoning, costs
    money/quota).

The qualifier IS a stack of Steps (per `mempalace.stack`), so it
plugs into the same primitives the rankers, resolution, and
matching use. This is the "chef's kiss" unification the user asked
for.

# Where the question qualifier lands

  - **MemPalace prediction-market node**: the qualifier explains
    to a registered oracle node (Track 7) HOW to answer a market.
    The plan it produces is what `ResolutionJob.execute()` runs.
  - **Other AI-infra projects** (Hermes, Fincept, etc.): same
    pattern, different substrate. The qualifier is generic over
    the local-substrate accessor.

# What this module ships

  - `QuestionShape` enum — categorical / continuous / set /
    open-ended.
  - `LocalCapability` enum — what the local substrate can answer
    well vs. needs remote help for.
  - `QuestionQualifier` — takes a question, produces a typed
    `QualifiedQuestion` describing recommended local steps,
    recommended remote steps, and the mixing rule.
  - Free function `qualify` for the simple case.

# What this module does NOT ship

  - The remote-LLM client. Production wires that to Anthropic's
    SDK; the qualifier returns abstract "remote_call" step
    descriptors that the executor resolves at run time.
  - Cost/quota accounting. Qualifier returns budget hints; an
    upstream policy enforces them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Taxonomy
# =============================================================================


class QuestionShape(str, Enum):
    """The output shape the question expects."""

    BINARY = "binary"
    """Yes / no. e.g., "Will X happen by date Y?" """

    CATEGORICAL = "categorical"
    """One of a fixed set. e.g., "Who will win: A, B, or C?" """

    CONTINUOUS = "continuous"
    """A real-valued estimate. e.g., "What price will X be at?" """

    SET = "set"
    """A subset of a fixed set. e.g., "Which of these will be true?" """

    OPEN_ENDED = "open_ended"
    """Free-form text. e.g., "What did you mean when you said X?" """


class LocalCapability(str, Enum):
    """How well the local substrate can answer the question."""

    HIGH = "high"
    """The substrate has direct evidence. Local-only is fine."""

    MEDIUM = "medium"
    """The substrate has tangential evidence. Local + small remote
    confirmation."""

    LOW = "low"
    """The question is about external facts the substrate doesn't
    track (e.g., world events, market prices). Remote-led."""

    UNKNOWN = "unknown"
    """Qualifier couldn't determine. Default to medium-mix."""


# =============================================================================
# Output type
# =============================================================================


@dataclass
class QualifiedQuestion:
    """The qualifier's decision for one question."""

    question_text: str
    shape: QuestionShape
    local_capability: LocalCapability

    local_step_kinds: list[str] = field(default_factory=list)
    """Step kinds the executor should run in the local-only phase.
    e.g., ["substrate_recall", "self_assertion_lookup"]."""

    remote_step_kinds: list[str] = field(default_factory=list)
    """Step kinds for the remote phase. e.g., ["claude_chain_of_thought",
    "claude_search_grounded"]."""

    mixing_rule: str = "remote_after_local"
    """How to combine local and remote outputs:
      - "local_only": skip remote.
      - "remote_only": skip local.
      - "remote_after_local": local first, remote takes the local
        answer as additional context.
      - "local_after_remote": remote first (broad knowledge),
        local refines (private context).
      - "parallel_then_merge": run both, merge with a comparison
        step.
    """

    estimated_remote_token_budget: int = 0
    """Cap the qualifier suggests on remote tokens. The executor
    enforces."""

    rationale: str = ""
    """Free-form: why the qualifier landed where it did. For audit."""


# =============================================================================
# Qualifier
# =============================================================================


@dataclass
class QuestionQualifier:
    """Stateful qualifier. Tests pass a fixed substrate-capability
    probe; production wires it to the actual substrate index."""

    substrate_capability_probe: Any = None  # Callable[[str], LocalCapability]
    """Optional callable: given the question text, returns the local
    capability assessment. If None, defaults to UNKNOWN."""

    def qualify(self, question_text: str) -> QualifiedQuestion:
        """Produce a QualifiedQuestion for the input."""
        shape = self._detect_shape(question_text)
        capability = self._probe_capability(question_text)

        local_steps, remote_steps, mixing, budget, rationale = (
            self._plan_for(shape, capability, question_text)
        )

        return QualifiedQuestion(
            question_text=question_text,
            shape=shape,
            local_capability=capability,
            local_step_kinds=local_steps,
            remote_step_kinds=remote_steps,
            mixing_rule=mixing,
            estimated_remote_token_budget=budget,
            rationale=rationale,
        )

    # ---- internals ----

    def _detect_shape(self, q: str) -> QuestionShape:
        """Cheap rule-based shape detection. Production may upgrade
        with an LLM-based classifier."""
        ql = q.lower().strip()
        # Binary signals
        if re.search(r"\b(will|has|did|is|are|can|should|would)\b", ql) and (
            ql.endswith("?") or "by date" in ql or "by " in ql
        ):
            # Heuristic: starts-with auxiliary + question mark = binary
            if ql.split() and ql.split()[0] in (
                "will", "has", "did", "is", "are", "can", "should", "would",
            ):
                return QuestionShape.BINARY
        # Continuous: contains a number-related concept
        if re.search(r"\b(price|count|how many|how much|value|amount)\b", ql):
            return QuestionShape.CONTINUOUS
        # Categorical: "which of"
        if re.search(r"\bwhich (of|one)\b", ql):
            return QuestionShape.CATEGORICAL
        # Set: "all of"
        if re.search(r"\b(all of|which all)\b", ql):
            return QuestionShape.SET
        return QuestionShape.OPEN_ENDED

    def _probe_capability(self, q: str) -> LocalCapability:
        """Probe the local substrate for evidence relevance."""
        if self.substrate_capability_probe is not None:
            return self.substrate_capability_probe(q)
        return LocalCapability.UNKNOWN

    def _plan_for(
        self,
        shape: QuestionShape,
        capability: LocalCapability,
        question: str,
    ) -> tuple[list[str], list[str], str, int, str]:
        """Decide step kinds + mixing rule + budget + rationale."""

        # Local-only when high local capability and binary/categorical
        # (the substrate has direct evidence; no need for external).
        if capability == LocalCapability.HIGH and shape in (
            QuestionShape.BINARY, QuestionShape.CATEGORICAL,
        ):
            return (
                ["substrate_recall", "self_assertion_lookup"],
                [],
                "local_only",
                0,
                "high local capability + crisp shape → local-only",
            )

        # Remote-only when low local capability (substrate has nothing
        # relevant; calling local just costs hops without payoff).
        if capability == LocalCapability.LOW:
            return (
                [],
                ["claude_chain_of_thought", "claude_search_grounded"],
                "remote_only",
                4000,
                "low local capability → remote-led",
            )

        # Continuous prediction → remote_after_local: get local
        # priors, then remote refines with broader context.
        if shape == QuestionShape.CONTINUOUS:
            return (
                ["substrate_recall", "rhyme_cluster_priors"],
                ["claude_chain_of_thought"],
                "remote_after_local",
                3000,
                "continuous output benefits from remote refinement of "
                "local priors",
            )

        # Open-ended → parallel_then_merge: local for personal context,
        # remote for breadth, merge.
        if shape == QuestionShape.OPEN_ENDED:
            return (
                ["substrate_recall", "self_narrative_extract"],
                ["claude_chain_of_thought"],
                "parallel_then_merge",
                4000,
                "open-ended question gets parallel local + remote "
                "with merge",
            )

        # Default: medium capability, balanced mix.
        return (
            ["substrate_recall"],
            ["claude_chain_of_thought"],
            "remote_after_local",
            2000,
            "default balanced mix",
        )


def qualify(
    question_text: str,
    *,
    substrate_capability_probe: Any = None,
) -> QualifiedQuestion:
    """Convenience: one-shot qualification."""
    q = QuestionQualifier(
        substrate_capability_probe=substrate_capability_probe,
    )
    return q.qualify(question_text)


__all__ = [
    "LocalCapability",
    "QualifiedQuestion",
    "QuestionQualifier",
    "QuestionShape",
    "qualify",
]
