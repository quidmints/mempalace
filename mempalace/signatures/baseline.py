"""
Self-baseline tracking + drift detection.

Per R3 §8.1 / §5.2.2: the user's own historical signature snapshots
form a baseline. Drift detection compares a current snapshot against
the baseline to surface "the user has moved." Behavior-vs-baseline
markets resolve against the same baseline data, with an enforced
minimum baseline-window of 90 days (R3 default).

Drift is computed per signature axis. The result is a per-axis
drift score (negative similarity = drift) plus an overall summary.

This module is the *self-comparison* counterpart to triage_indicator.py
(which compares against foreign snapshots).

Spec ref: R3 §8.1, §5.2.2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .store import SignatureSnapshot, SignatureStore, signature_similarity


# =============================================================================
# Constants
# =============================================================================

# Minimum baseline window for behavior-vs-baseline markets (R3 §5.2.2).
# Markets with shorter baselines reject at creation time on-chain.
MIN_BASELINE_WINDOW_DAYS = 90
MIN_BASELINE_WINDOW_MS = MIN_BASELINE_WINDOW_DAYS * 24 * 3600 * 1000


# =============================================================================
# Drift result
# =============================================================================


@dataclass
class DriftReport:
    """Drift of a current snapshot vs a baseline aggregate."""

    period_id: str = ""
    baseline_snapshot_count: int = 0
    baseline_window_ms: int = 0
    baseline_window_meets_minimum: bool = False

    # per-axis drift in [0, 1]; higher = more drift
    drift_by_axis: dict[str, float] = field(default_factory=dict)

    # overall drift in [0, 1]
    overall_drift: float = 0.0

    # axes flagged as significantly drifting (above threshold)
    flagged_axes: list[str] = field(default_factory=list)


# =============================================================================
# Baseline aggregation
# =============================================================================


def _mean_dict_of_floats(dicts: Iterable[dict[str, float]]) -> dict[str, float]:
    """Element-wise mean across dicts."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for d in dicts:
        for k, v in d.items():
            sums[k] = sums.get(k, 0.0) + v
            counts[k] = counts.get(k, 0) + 1
    return {k: sums[k] / counts[k] for k in sums if counts[k] > 0}


def _mean_dict_of_vectors(dicts: Iterable[dict[str, list[float]]]) -> dict[str, list[float]]:
    """Element-wise vector mean across dicts."""
    sums: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for d in dicts:
        for k, v in d.items():
            if not v:
                continue
            if k not in sums:
                sums[k] = list(v)
                counts[k] = 1
            else:
                if len(sums[k]) != len(v):
                    continue  # skip dimension mismatches
                sums[k] = [a + b for a, b in zip(sums[k], v, strict=True)]
                counts[k] += 1
    return {k: [x / counts[k] for x in sums[k]] for k in sums if counts[k] > 0}


def aggregate_baseline(snapshots: list[SignatureSnapshot]) -> SignatureSnapshot:
    """Build a synthetic 'baseline snapshot' that's the mean of all
    snapshots in `snapshots`."""
    if not snapshots:
        return SignatureSnapshot()
    return SignatureSnapshot(
        snapshot_id="baseline_aggregate",
        period_id="baseline",
        captured_at_ms=snapshots[-1].captured_at_ms,
        window_start_ms=snapshots[0].window_start_ms,
        window_end_ms=snapshots[-1].window_end_ms,
        mean_position_by_theme=_mean_dict_of_vectors(
            [s.mean_position_by_theme for s in snapshots]
        ),
        velocity_by_theme=_mean_dict_of_floats(
            [s.velocity_by_theme for s in snapshots]
        ),
        # take all schemas seen in the baseline window
        schema_fingerprints=sorted({
            fp for s in snapshots for fp in s.schema_fingerprints
        }),
        contradiction_profile=_mean_dict_of_floats(
            [s.contradiction_profile for s in snapshots]
        ),
        fork_distribution_by_theme=_mean_dict_of_vectors(
            [s.fork_distribution_by_theme for s in snapshots]
        ),
        drawer_count=sum(s.drawer_count for s in snapshots),
        assertion_count=sum(s.assertion_count for s in snapshots),
    )


# =============================================================================
# Drift computation
# =============================================================================


def compute_drift(
    *,
    current: SignatureSnapshot,
    baseline_snapshots: list[SignatureSnapshot],
    drift_threshold: float = 0.4,
) -> DriftReport:
    """Compute drift of `current` vs the aggregate of `baseline_snapshots`."""
    if not baseline_snapshots:
        return DriftReport(
            period_id=current.period_id,
            baseline_snapshot_count=0,
            baseline_window_ms=0,
            baseline_window_meets_minimum=False,
            drift_by_axis={},
            overall_drift=0.0,
        )

    baseline = aggregate_baseline(baseline_snapshots)
    similarity = signature_similarity(current, baseline)
    drift_by_axis = {axis: max(0.0, 1.0 - sim) for axis, sim in similarity.items()}

    # overall = mean of per-axis drift (axis weights left to caller's
    # composition)
    if drift_by_axis:
        overall = sum(drift_by_axis.values()) / len(drift_by_axis)
    else:
        overall = 0.0

    flagged = [axis for axis, d in drift_by_axis.items() if d >= drift_threshold]

    window_ms = (
        baseline_snapshots[-1].window_end_ms - baseline_snapshots[0].window_start_ms
    )
    return DriftReport(
        period_id=current.period_id,
        baseline_snapshot_count=len(baseline_snapshots),
        baseline_window_ms=max(0, window_ms),
        baseline_window_meets_minimum=window_ms >= MIN_BASELINE_WINDOW_MS,
        drift_by_axis=drift_by_axis,
        overall_drift=overall,
        flagged_axes=sorted(flagged),
    )


# =============================================================================
# Behavior-vs-baseline market support
# =============================================================================


@dataclass
class BaselineMarketEligibility:
    """Whether a behavior-vs-baseline market can be created against
    a given axis on the user's signature."""

    eligible: bool
    axis: str
    window_ms: int
    snapshots_in_window: int
    reason: str = ""


def check_baseline_market_eligibility(
    *,
    store: SignatureStore,
    axis: str,
    window_start_ms: int,
    window_end_ms: int,
) -> BaselineMarketEligibility:
    """Check whether the requested baseline window is acceptable for
    a behavior-vs-baseline market.

    Constraints (R3 §5.2.2):
      - Window length ≥ MIN_BASELINE_WINDOW_DAYS (90)
      - At least 3 snapshots in the window
      - Axis must be one of the §8.2 named axes
    """
    valid_axes = {
        "mean_position",
        "velocity",
        "schema_fingerprint",
        "contradiction_profile",
        "fork_significance",
    }
    if axis not in valid_axes:
        return BaselineMarketEligibility(
            eligible=False,
            axis=axis,
            window_ms=0,
            snapshots_in_window=0,
            reason=f"axis '{axis}' is not a recognized signature axis",
        )

    window_ms = max(0, window_end_ms - window_start_ms)
    if window_ms < MIN_BASELINE_WINDOW_MS:
        return BaselineMarketEligibility(
            eligible=False,
            axis=axis,
            window_ms=window_ms,
            snapshots_in_window=0,
            reason=(
                f"window of {window_ms / (24*3600*1000):.1f} days is below "
                f"minimum {MIN_BASELINE_WINDOW_DAYS} days"
            ),
        )

    snaps = store.window(window_start_ms, window_end_ms)
    if len(snaps) < 3:
        return BaselineMarketEligibility(
            eligible=False,
            axis=axis,
            window_ms=window_ms,
            snapshots_in_window=len(snaps),
            reason="need at least 3 snapshots in window",
        )

    return BaselineMarketEligibility(
        eligible=True,
        axis=axis,
        window_ms=window_ms,
        snapshots_in_window=len(snaps),
    )


__all__ = [
    "BaselineMarketEligibility",
    "DriftReport",
    "MIN_BASELINE_WINDOW_DAYS",
    "MIN_BASELINE_WINDOW_MS",
    "aggregate_baseline",
    "check_baseline_market_eligibility",
    "compute_drift",
]
