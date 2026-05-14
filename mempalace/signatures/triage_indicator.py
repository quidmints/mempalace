"""
Triage indicator with feedback loop.

Per R3 §8.3: a cheap pre-filter on candidate match pairs before the
expensive layered matching runs. Similarity is computed as a weighted
sum of per-axis signature similarity. Feedback from confirmed false
positives down-weights the dimensions that aligned, with two safety
mechanisms:

  - **Cooldown**: a single false-positive pattern can't trigger another
    adjustment for 30 days (default).
  - **Cap**: a single dimension can't be down-weighted to zero, only
    attenuated. Floor at 0.1 of the original weight.

All adjustments are recorded as events; reversible.

Spec ref: R3 §8.3.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .store import SignatureSnapshot, signature_similarity


# =============================================================================
# Constants
# =============================================================================

# 30-day cooldown between adjustments triggered by a single FP pattern
DEFAULT_COOLDOWN_DAYS = 30
DEFAULT_COOLDOWN_MS = DEFAULT_COOLDOWN_DAYS * 24 * 3600 * 1000

# Floor at 0.1 × original — never zero out a dimension
WEIGHT_FLOOR_FRACTION = 0.1

# Down-weight step on a single FP confirmation (multiplicative)
ADJUSTMENT_FACTOR = 0.85

# Default per-axis triage weights (sum normalized later)
DEFAULT_WEIGHTS: dict[str, float] = {
    "mean_position": 0.25,
    "velocity": 0.20,
    "schema_fingerprint": 0.20,
    "contradiction_profile": 0.15,
    "fork_significance": 0.20,
}

# Triage gate threshold: candidate pairs below this don't proceed
DEFAULT_TRIAGE_GATE = 0.40


# =============================================================================
# Types
# =============================================================================


@dataclass
class TriageScore:
    """Result of running the triage indicator on one pair."""

    pair_score: float
    per_axis_similarity: dict[str, float] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    gate_passed: bool = False


@dataclass
class FalsePositiveAdjustment:
    """A recorded adjustment to triage weights from a confirmed FP."""

    adjustment_id: str
    fp_pattern_hash: str
    axis_deltas: dict[str, float]            # post-weight - pre-weight
    pre_weights: dict[str, float]
    post_weights: dict[str, float]
    recorded_at_ms: int
    reversible: bool = True


# =============================================================================
# Triage indicator
# =============================================================================


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in weights.items()}


def _fp_pattern_hash(per_axis_similarity: dict[str, float], threshold: float = 0.5) -> str:
    """Hash the *shape* of axes that aligned in the FP, used to enforce
    the per-pattern cooldown.

    Two FPs that aligned on the same set of axes share a hash; an FP
    that aligned on a different set gets a different hash and so is
    not blocked by the cooldown for the prior pattern.
    """
    aligned_axes = sorted(
        ax for ax, sim in per_axis_similarity.items() if sim >= threshold
    )
    digest = hashlib.blake2b(
        ",".join(aligned_axes).encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    return digest


class TriageIndicator:
    """Composes per-axis similarities into a single score using
    learned weights with cooldown + cap protection on adjustments.
    """

    def __init__(
        self,
        *,
        weights: dict[str, float] | None = None,
        gate_threshold: float = DEFAULT_TRIAGE_GATE,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        weight_floor_fraction: float = WEIGHT_FLOOR_FRACTION,
    ) -> None:
        self._original_weights = dict(weights or DEFAULT_WEIGHTS)
        self._weights = _normalize(self._original_weights)
        self._gate = gate_threshold
        self._cooldown_ms = cooldown_ms
        self._floor_fraction = weight_floor_fraction
        self._adjustments: list[FalsePositiveAdjustment] = []
        # last adjustment ts per FP pattern hash
        self._last_adjustment_by_pattern: dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- score ----------------------------------------------------------

    def score(
        self,
        local: SignatureSnapshot,
        foreign: SignatureSnapshot,
    ) -> TriageScore:
        """Score a candidate pair. Returns a TriageScore."""
        sims = signature_similarity(local, foreign)
        with self._lock:
            weights = dict(self._weights)
        # weighted sum over axes that both sides have a similarity for
        score = 0.0
        for axis, w in weights.items():
            score += w * sims.get(axis, 0.0)
        return TriageScore(
            pair_score=max(0.0, min(1.0, score)),
            per_axis_similarity=sims,
            weights_used=weights,
            gate_passed=score >= self._gate,
        )

    # ---- feedback ---------------------------------------------------------

    def record_false_positive(
        self,
        *,
        per_axis_similarity: dict[str, float],
        now_ms: int | None = None,
    ) -> FalsePositiveAdjustment | None:
        """Record a confirmed FP and adjust weights subject to cooldown
        + cap.

        Returns the FalsePositiveAdjustment that was applied, or None
        if the cooldown blocked the adjustment.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        pattern_hash = _fp_pattern_hash(per_axis_similarity)

        with self._lock:
            last = self._last_adjustment_by_pattern.get(pattern_hash, 0)
            if last > 0 and (now_ms - last) < self._cooldown_ms:
                return None  # cooldown blocked

            pre = dict(self._weights)
            post = dict(self._weights)

            # down-weight any axis whose similarity is high in this FP
            for axis, sim in per_axis_similarity.items():
                if axis not in post:
                    continue
                if sim < 0.5:
                    continue
                # apply multiplicative down-weight, but never below the floor
                original_w = self._original_weights.get(axis, post[axis])
                # original_w is also normalized? It's the *pre-normalization*
                # weight; floor is computed against the pre-normalization
                # value to prevent the renormalize step from blowing past it.
                floor = original_w * self._floor_fraction
                proposed = post[axis] * ADJUSTMENT_FACTOR
                post[axis] = max(floor, proposed)

            # re-normalize
            normalized_post = _normalize(post)
            self._weights = normalized_post

            adjustment = FalsePositiveAdjustment(
                adjustment_id=f"adj_{pattern_hash}_{now_ms}",
                fp_pattern_hash=pattern_hash,
                axis_deltas={k: normalized_post[k] - pre[k] for k in pre},
                pre_weights=pre,
                post_weights=normalized_post,
                recorded_at_ms=now_ms,
            )
            self._adjustments.append(adjustment)
            self._last_adjustment_by_pattern[pattern_hash] = now_ms

        return adjustment

    # ---- introspection / reversal ---------------------------------------

    def current_weights(self) -> dict[str, float]:
        with self._lock:
            return dict(self._weights)

    def adjustments(self) -> list[FalsePositiveAdjustment]:
        with self._lock:
            return list(self._adjustments)

    def reset(self) -> None:
        """Reset weights to original. (Reversibility per R3 §8.3.)"""
        with self._lock:
            self._weights = _normalize(self._original_weights)
            self._adjustments.clear()
            self._last_adjustment_by_pattern.clear()


__all__ = [
    "ADJUSTMENT_FACTOR",
    "DEFAULT_COOLDOWN_DAYS",
    "DEFAULT_COOLDOWN_MS",
    "DEFAULT_TRIAGE_GATE",
    "DEFAULT_WEIGHTS",
    "FalsePositiveAdjustment",
    "TriageIndicator",
    "TriageScore",
    "WEIGHT_FLOOR_FRACTION",
]
