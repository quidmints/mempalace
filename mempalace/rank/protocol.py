"""
Ranker protocol.

A Ranker takes (candidates, stance) and returns scored candidates. The
protocol is intentionally minimal so rankers can be composed in stacks
and dispatched dynamically from the registry.

Per R3 §1: rankers are a specialization of Step (RankerStep). They share
the stacking machinery; their domain-specific shape is a typed
candidates/stance input and a typed scored-candidates output.

Per R3 §6: rankers run in process isolation with capability-restricted
APIs. The protocol here defines the contract; isolation enforcement is
in `rank.isolation`.

Spec ref: Part 7 (rankers), R3 §1, §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..retrieve.gather import Candidate
from ..schema.stance import Stance


# =============================================================================
# Scored candidate
# =============================================================================


@dataclass
class ScoredCandidate:
    """A candidate plus its score and the per-axis breakdown.

    The breakdown is preserved through ranking so downstream consumers
    (the trusted aggregator, evaluation hooks) can introspect why a
    candidate scored as it did.
    """

    candidate: Candidate
    score: float
    axes: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Ranker protocol
# =============================================================================


@runtime_checkable
class Ranker(Protocol):
    """Ranker contract.

    Implementations:
      - FactoredMultiplicativeRanker (rank.factored)
      - NeuralRankerStub             (rank.neural_stub)
      - any user-supplied ranker subject to isolation rules
    """

    name: str
    version: str

    def declares(self) -> RankerManifest: ...
    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        feature_names: list[str] | None = None,
    ) -> list[ScoredCandidate]: ...


# =============================================================================
# Ranker manifest
# =============================================================================


@dataclass
class RankerManifest:
    """Static declaration of a ranker's behavior.

    feature_dependencies: features the ranker reads from candidate.features.
    consumes_stance:      whether the ranker reads stance fields.
    deterministic:        if True, same inputs always yield same outputs
                          (no randomness). Required for replayability.
    side_effects:         must be False; rankers are pure functions.
    """

    name: str
    version: str
    feature_dependencies: tuple[str, ...] = ()
    consumes_stance: bool = True
    deterministic: bool = True
    side_effects: bool = False
    description: str = ""


# =============================================================================
# Helpers
# =============================================================================


def empty_score(candidate: Candidate, axes: dict[str, float] | None = None) -> ScoredCandidate:
    """Convenience: a zero-score scored candidate."""
    return ScoredCandidate(
        candidate=candidate,
        score=0.0,
        axes=dict(axes or {}),
    )


def normalize_scores(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Normalize scores to [0, 1] by max. In-place.

    If max is 0, leaves scores unchanged.
    """
    if not scored:
        return scored
    max_score = max(s.score for s in scored)
    if max_score <= 0:
        return scored
    for s in scored:
        s.score = s.score / max_score
    return scored


__all__ = [
    "Ranker",
    "RankerManifest",
    "ScoredCandidate",
    "empty_score",
    "normalize_scores",
]
