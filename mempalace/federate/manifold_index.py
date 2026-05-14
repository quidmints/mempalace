"""
Manifold index — velocity field summary for federation.

Per R3 §9.5 (Layer 1 input) and Part 9.2: each palace publishes a
velocity-field summary that captures its current "manifold tension":
which themes/periods/events are bursting, where uncertainty lives,
and what tunnels (sudden cross-region paths) have appeared.

The summary is small and structurally non-revealing: it exposes
abstract activity over the canonicalized vocabulary (theme/period/event),
not substrate.

Three components:

  1. Path-weight EMA: exponentially-weighted average of edge traversal
     in the last ~30 days. Surfaces "what's currently flowing".
  2. Uncertainty: per-region entropy of which assertions are being
     refined. High = active rethinking.
  3. Tunnels: shortcut paths that emerged recently — sudden links
     between regions that were previously distant.

Spec ref: R3 §9.5, Part 9.2.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Constants
# =============================================================================

# Vocabulary: regions are indexed by canonicalized {theme, period, event}.
# These three are the user-facing structural-meaning vocabulary per R3 §6.4.

DEFAULT_HALF_LIFE_DAYS = 30.0
TUNNEL_MIN_GAIN = 3.0   # tunnel threshold: traversal weight must exceed
                        # baseline by 3x to count as a "sudden" shortcut


# =============================================================================
# Path-weight EMA
# =============================================================================


@dataclass
class PathWeightEMA:
    """Exponentially-weighted average of edge traversal counts."""

    weights: dict[str, float] = field(default_factory=dict)  # edge_signature -> ema
    last_update_ms: int = 0
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS

    def _decay_factor(self, now_ms: int) -> float:
        if self.last_update_ms <= 0:
            return 1.0
        elapsed_s = max(0.0, (now_ms - self.last_update_ms) / 1000.0)
        elapsed_days = elapsed_s / 86_400.0
        if self.half_life_days <= 0:
            return 1.0
        return math.pow(0.5, elapsed_days / self.half_life_days)

    def step(self, edge_traversals: dict[str, float], *, now_ms: int | None = None) -> None:
        """Apply decay then add fresh traversals."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        decay = self._decay_factor(now_ms)
        # decay all existing weights
        for k in list(self.weights.keys()):
            self.weights[k] *= decay
            if self.weights[k] < 1e-9:
                del self.weights[k]
        # add fresh
        for k, v in edge_traversals.items():
            self.weights[k] = self.weights.get(k, 0.0) + v
        self.last_update_ms = now_ms


# =============================================================================
# Uncertainty estimation
# =============================================================================


def region_uncertainty(
    refinement_counts_by_region: dict[str, int],
    total_assertions_by_region: dict[str, int],
) -> dict[str, float]:
    """Per-region uncertainty proxy.

    Returns the rate of refinement (refinements / total) by region,
    clipped to [0, 1]. Higher = more active rethinking in that region.
    """
    out: dict[str, float] = {}
    for region, refines in refinement_counts_by_region.items():
        total = total_assertions_by_region.get(region, 0)
        if total <= 0:
            out[region] = 0.0
        else:
            out[region] = min(1.0, refines / total)
    return out


# =============================================================================
# Tunnel discovery
# =============================================================================


@dataclass
class Tunnel:
    """A sudden cross-region path that emerged recently."""

    region_a: str
    region_b: str
    gain_ratio: float                  # current_weight / baseline_weight
    discovered_at_ms: int


def discover_tunnels(
    current_path_weights: dict[tuple[str, str], float],
    baseline_path_weights: dict[tuple[str, str], float],
    *,
    min_gain: float = TUNNEL_MIN_GAIN,
    min_baseline: float = 0.1,
) -> list[Tunnel]:
    """Find region pairs whose traversal jumped vs the baseline."""
    now = int(time.time() * 1000)
    tunnels: list[Tunnel] = []
    for pair, current in current_path_weights.items():
        if current <= 0:
            continue
        baseline = baseline_path_weights.get(pair, 0.0)
        # use min_baseline as a floor to avoid divide-by-zero amplification
        # of tiny baselines
        denom = max(baseline, min_baseline)
        gain = current / denom
        if gain >= min_gain:
            tunnels.append(
                Tunnel(
                    region_a=pair[0],
                    region_b=pair[1],
                    gain_ratio=gain,
                    discovered_at_ms=now,
                )
            )
    # sort by gain descending
    tunnels.sort(key=lambda t: -t.gain_ratio)
    return tunnels


# =============================================================================
# Velocity-field summary (the published artifact)
# =============================================================================


@dataclass
class VelocityFieldSummary:
    """The summary that gets attached to a public manifest.

    Small enough to ship; structurally informative enough for Layer 1.
    """

    schema_version: str = "velocity_field.v1"
    generated_at_ms: int = 0
    horizon_days: float = DEFAULT_HALF_LIFE_DAYS

    # Region → activity weight (theme/period/event regions, anonymized
    # via canonicalizer schema_ids; not substrate text).
    activity_by_region: dict[str, float] = field(default_factory=dict)

    # Region → uncertainty in [0, 1]
    uncertainty_by_region: dict[str, float] = field(default_factory=dict)

    # Recently-discovered tunnels (region_a, region_b, gain_ratio)
    tunnels: list[tuple[str, str, float]] = field(default_factory=list)

    def to_layer1_velocity(self) -> dict[str, float]:
        """Project to a flat velocity dict compatible with Layer 1's
        velocity_corr input.

        Produces a per-region "intensity" combining activity and
        (optionally) tunnel involvement.
        """
        out: dict[str, float] = {}
        for region, activity in self.activity_by_region.items():
            unc = self.uncertainty_by_region.get(region, 0.0)
            # intensity weighting: activity + 0.5*uncertainty
            out[region] = float(activity) + 0.5 * float(unc)
        # bump tunnel-endpoint regions
        for r_a, r_b, gain in self.tunnels:
            for r in (r_a, r_b):
                out[r] = out.get(r, 0.0) + 0.1 * float(gain)
        return out


def build_velocity_field_summary(
    *,
    activity_by_region: dict[str, float],
    refinement_counts_by_region: dict[str, int],
    total_assertions_by_region: dict[str, int],
    current_path_weights: dict[tuple[str, str], float],
    baseline_path_weights: dict[tuple[str, str], float],
    horizon_days: float = DEFAULT_HALF_LIFE_DAYS,
    now_ms: int | None = None,
) -> VelocityFieldSummary:
    """Assemble a publishable VelocityFieldSummary."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    uncertainty = region_uncertainty(
        refinement_counts_by_region, total_assertions_by_region
    )
    tunnels = discover_tunnels(current_path_weights, baseline_path_weights)
    return VelocityFieldSummary(
        generated_at_ms=now_ms,
        horizon_days=horizon_days,
        activity_by_region=dict(activity_by_region),
        uncertainty_by_region=uncertainty,
        tunnels=[(t.region_a, t.region_b, t.gain_ratio) for t in tunnels[:32]],
    )


__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "PathWeightEMA",
    "TUNNEL_MIN_GAIN",
    "Tunnel",
    "VelocityFieldSummary",
    "build_velocity_field_summary",
    "discover_tunnels",
    "region_uncertainty",
]
