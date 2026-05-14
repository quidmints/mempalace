"""
Neural ranker stub.

A second-tier ranker that learns cross-feature interactions from feedback
events. The full implementation is a small MLP trained on
(features, observed_relevance) pairs from `feedback_recorded` events;
this module establishes the interface and a training-stub that uses
hand-tuned bilinear cross-terms until enough feedback data has accumulated.

When the neural ranker has trained, the dispatch layer prefers it over
the factored ranker for consumers that want non-linear combinations
(MONTAGE, MATCHING). The factored ranker is still preferred for
FOYER and AGENT (low-latency, deterministic, interpretable).

Spec ref: Part 7, R3 §9.4.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from ..retrieve.gather import Candidate
from ..schema.stance import Stance
from .factored import FactoredMultiplicativeRanker
from .protocol import RankerManifest, ScoredCandidate


# =============================================================================
# Bilinear cross-term table
# =============================================================================


@dataclass
class CrossTermTable:
    """Hand-seeded cross-term weights, replaced by trained values when ready.

    Each entry maps (feature_a, feature_b) to a bilinear weight. The score
    contribution from cross-terms is sum(weight * f_a * f_b) over registered
    pairs.
    """

    weights: dict[tuple[str, str], float] = field(default_factory=lambda: {
        # Heat × velocity: a hot drawer that's been accessed recently
        # is doubly relevant.
        ("drawer_heat", "drawer_velocity_30d"): 0.3,
        # Canonicality × faithfulness: faithful canonical assertions
        # are gold; rare.
        ("theme_canonicality", "assertion_substrate_faithfulness"): 0.5,
        # Recency × fork: recent decisions matter more than old routine.
        ("drawer_recency_score", "event_fork_significance"): 0.4,
        # Heat × fork: ongoing decision points get extra weight.
        ("drawer_heat", "event_fork_significance"): 0.25,
    })


# =============================================================================
# Ranker
# =============================================================================


class NeuralRankerStub:
    """Stand-in for a trained neural ranker.

    Composition:
      score = base_factored + sum(cross_term_weight * f_a * f_b)
    where base_factored is the FactoredMultiplicativeRanker score.

    When trained weights are available, this class loads them via
    `load_weights()` and replaces the cross-term table.
    """

    def __init__(
        self,
        *,
        cross_terms: CrossTermTable | None = None,
        base_ranker: FactoredMultiplicativeRanker | None = None,
        cross_term_weight: float = 0.3,
    ) -> None:
        self.name = "neural_stub"
        self.version = "0.1.0"
        self._cross_terms = cross_terms or CrossTermTable()
        self._base = base_ranker or FactoredMultiplicativeRanker()
        self._cross_weight = cross_term_weight
        self._weights_hash = self._compute_weights_hash()

    def _compute_weights_hash(self) -> str:
        items = sorted(self._cross_terms.weights.items())
        s = "|".join(f"{k[0]},{k[1]}={v}" for k, v in items)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

    def declares(self) -> RankerManifest:
        return RankerManifest(
            name=self.name,
            version=f"{self.version}+{self._weights_hash}",
            feature_dependencies=tuple(set(
                f for pair in self._cross_terms.weights.keys() for f in pair
            )) + self._base.declares().feature_dependencies,
            consumes_stance=True,
            deterministic=True,
            side_effects=False,
            description="Neural-stub ranker: factored base + bilinear cross terms.",
        )

    def load_weights(self, weights: dict[tuple[str, str], float]) -> None:
        """Replace the cross-term table (e.g., after training)."""
        self._cross_terms = CrossTermTable(weights=dict(weights))
        self._weights_hash = self._compute_weights_hash()

    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        feature_names: list[str] | None = None,
    ) -> list[ScoredCandidate]:
        # Run the base factored ranker first
        base_scored = self._base.rank(candidates, stance, feature_names=feature_names)
        # Index by node_id for joining
        base_by_id = {sc.candidate.node_id: sc for sc in base_scored}

        out: list[ScoredCandidate] = []
        for cand in candidates:
            base_sc = base_by_id.get(cand.node_id)
            if base_sc is None:
                continue

            # Cross-term contribution
            cross_contrib = 0.0
            cross_axes: dict[str, float] = dict(base_sc.axes)
            for (fa, fb), w in self._cross_terms.weights.items():
                va = float(cand.features.get(fa, 0.0))
                vb = float(cand.features.get(fb, 0.0))
                term = w * va * vb
                cross_contrib += term
                cross_axes[f"cross:{fa}×{fb}"] = term

            # Combined: base + weighted cross. Sigmoid to [0, 1].
            raw = base_sc.score + self._cross_weight * cross_contrib
            combined = 1.0 / (1.0 + math.exp(-raw + 1.0))  # shifted sigmoid

            out.append(ScoredCandidate(
                candidate=cand,
                score=combined,
                axes=cross_axes,
                metadata={"base_score": base_sc.score},
            ))

        out.sort(key=lambda s: s.score, reverse=True)
        return out


__all__ = ["CrossTermTable", "NeuralRankerStub"]
