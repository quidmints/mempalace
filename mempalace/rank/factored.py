"""
Factored multiplicative ranker.

Combines per-axis features multiplicatively, with each axis weighted by the
active stance dimension. This is the canonical first-tier ranker; cheap,
interpretable, deterministic.

The axes are:
  - recency        (drawer_recency_score)
  - heat           (drawer_heat)
  - canonicality   (theme_canonicality OR is_canonical)
  - velocity       (drawer_velocity_30d)
  - faithfulness   (assertion_substrate_faithfulness, when applicable)
  - fork           (event_fork_significance, when applicable)

Each axis contributes a factor in [floor, 1]. The aggregate score is the
geometric mean of the active factors. The geometric mean (rather than
sum) means a near-zero on any axis pulls the score down — this is the
"all axes must be reasonable" semantics rather than "any axis can carry."

Stance modulation:
  - correspondence_vs_coherence shifts weight between canonicality (corr)
    and heat (coh).
  - recency_bias modulates the recency exponent.
  - canonicality_floor sets the minimum canonicality factor.
  - exploration_entropy adds noise to the score (per R3 §9.4); zero
    exploration is purely deterministic; positive exploration adds bounded
    perturbation.

Spec ref: Part 7, R3 §9.4 (exploration tunable).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from ..retrieve.gather import Candidate
from ..schema.stance import Stance
from .protocol import RankerManifest, ScoredCandidate


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class FactoredConfig:
    """Per-axis exponents and floors. Tunable per palace."""

    recency_exponent: float = 1.0
    heat_exponent: float = 1.0
    canonicality_exponent: float = 1.0
    velocity_exponent: float = 0.5
    faithfulness_exponent: float = 1.0
    fork_exponent: float = 0.5
    floor: float = 0.05  # all factors clamped to >= floor before multiplication


# =============================================================================
# Determinism: reproducible exploration noise
# =============================================================================


def _exploration_noise(seed_text: str, magnitude: float) -> float:
    """Deterministic noise in [-magnitude, +magnitude] keyed to seed_text."""
    if magnitude <= 0:
        return 0.0
    h = hashlib.sha256(seed_text.encode("utf-8")).digest()
    # Use first 8 bytes as int, map to [-1, 1]
    n = int.from_bytes(h[:8], "big")
    normalized = (n / (2 ** 64 - 1)) * 2.0 - 1.0
    return normalized * magnitude


# =============================================================================
# Ranker
# =============================================================================


class FactoredMultiplicativeRanker:
    """The canonical factored ranker."""

    def __init__(self, config: FactoredConfig | None = None) -> None:
        self.config = config or FactoredConfig()
        self.name = "factored_multiplicative"
        self.version = "0.1.0"

    def declares(self) -> RankerManifest:
        return RankerManifest(
            name=self.name,
            version=self.version,
            feature_dependencies=(
                "drawer_recency_score",
                "drawer_heat",
                "theme_canonicality",
                "drawer_velocity_30d",
                "assertion_substrate_faithfulness",
                "event_fork_significance",
            ),
            consumes_stance=True,
            deterministic=True,
            side_effects=False,
            description="Multiplicative composition of per-axis features, stance-modulated.",
        )

    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        feature_names: list[str] | None = None,
    ) -> list[ScoredCandidate]:
        """Score candidates and return sorted descending by score."""
        cfg = self.config

        # Stance-modulated weighting between correspondence (canonicality)
        # and coherence (heat). cvc in [0, 1] shifts weight directly:
        # cvc=0 → all heat; cvc=1 → all canonicality; cvc=0.5 → balanced.
        canon_weight = stance.correspondence_vs_coherence
        heat_weight = 1.0 - canon_weight

        # Recency bias modulates the recency exponent
        eff_recency_exp = cfg.recency_exponent * stance.recency_bias

        scored: list[ScoredCandidate] = []
        for cand in candidates:
            f = cand.features
            axes: dict[str, float] = {}
            factors: list[float] = []

            # Recency
            recency = float(f.get("drawer_recency_score", 0.5))
            if eff_recency_exp != 0:
                factor = max(cfg.floor, recency ** abs(eff_recency_exp))
                if eff_recency_exp < 0:
                    factor = 1.0 - factor + cfg.floor  # flip if negative bias
                axes["recency"] = factor
                factors.append(factor)

            # Heat (coherence pull)
            heat = float(f.get("drawer_heat", 0.5))
            heat_factor = max(cfg.floor, heat ** cfg.heat_exponent)
            heat_factor = heat_factor * heat_weight + (1.0 - heat_weight)
            axes["heat"] = heat_factor
            factors.append(max(cfg.floor, heat_factor))

            # Canonicality (correspondence pull)
            canon = float(f.get("theme_canonicality", 0.0))
            # Apply floor from stance (stance.canonicality_floor in [0,1])
            canon = max(canon, stance.canonicality_floor)
            canon_factor = max(cfg.floor, canon ** cfg.canonicality_exponent)
            canon_factor = canon_factor * canon_weight + (1.0 - canon_weight)
            axes["canonicality"] = canon_factor
            factors.append(max(cfg.floor, canon_factor))

            # Velocity
            velocity_raw = float(f.get("drawer_velocity_30d", 0.0))
            # Normalize velocity to [0,1]: assume 30 accesses/30d is "high"
            velocity = min(1.0, velocity_raw / 30.0)
            vel_factor = max(cfg.floor, velocity ** cfg.velocity_exponent)
            axes["velocity"] = vel_factor
            factors.append(vel_factor)

            # Faithfulness (when assertion-typed)
            faith = f.get("assertion_substrate_faithfulness")
            if faith is not None:
                faith_factor = max(cfg.floor, float(faith) ** cfg.faithfulness_exponent)
                axes["faithfulness"] = faith_factor
                factors.append(faith_factor)

            # Fork significance (event-typed; signature-relevant per R3 §9.6)
            fork = f.get("event_fork_significance")
            if fork is not None:
                fork_factor = max(cfg.floor, float(fork) ** cfg.fork_exponent)
                axes["fork"] = fork_factor
                # Forks contribute additively-ish: weight as a boost factor
                # rather than reducing the rest. Apply as multiplicative
                # bonus capped at 2.0.
                factors.append(min(2.0, 1.0 + fork_factor))

            # Geometric mean
            if not factors:
                base_score = 0.5
            else:
                log_sum = sum(math.log(max(cfg.floor, f)) for f in factors)
                base_score = math.exp(log_sum / len(factors))

            # Exploration noise per R3 §9.4
            if stance.exploration_entropy > 0:
                seed_text = f"{cand.node_id}:{stance.consumer_kind.value}:{base_score}"
                noise = _exploration_noise(seed_text, stance.exploration_entropy * 0.2)
                base_score = max(0.0, min(1.0, base_score + noise))

            scored.append(ScoredCandidate(
                candidate=cand,
                score=base_score,
                axes=axes,
            ))

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored


__all__ = ["FactoredConfig", "FactoredMultiplicativeRanker"]
