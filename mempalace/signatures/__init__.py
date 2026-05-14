"""
mempalace.signatures — narrowed signature concept (R3 §8).

Two legitimate uses:

  1. self-baseline tracking (drift detection, behavior-vs-baseline markets)
  2. triage indicator (Layer 1 pre-filter with feedback loop)

The "unusual axes alignment as primary match signal" framing from earlier
revisions is dropped.

Spec ref: R3 §8.
"""

from .baseline import (
    BaselineMarketEligibility,
    DriftReport,
    MIN_BASELINE_WINDOW_DAYS,
    MIN_BASELINE_WINDOW_MS,
    aggregate_baseline,
    check_baseline_market_eligibility,
    compute_drift,
)
from .store import (
    SignatureSnapshot,
    SignatureStore,
    build_signature_snapshot,
    get_signature_store,
    set_signature_store,
    signature_similarity,
)
from .triage_indicator import (
    ADJUSTMENT_FACTOR,
    DEFAULT_COOLDOWN_DAYS,
    DEFAULT_COOLDOWN_MS,
    DEFAULT_TRIAGE_GATE,
    DEFAULT_WEIGHTS,
    FalsePositiveAdjustment,
    TriageIndicator,
    TriageScore,
    WEIGHT_FLOOR_FRACTION,
)

__all__ = [
    "ADJUSTMENT_FACTOR",
    "BaselineMarketEligibility",
    "DEFAULT_COOLDOWN_DAYS",
    "DEFAULT_COOLDOWN_MS",
    "DEFAULT_TRIAGE_GATE",
    "DEFAULT_WEIGHTS",
    "DriftReport",
    "FalsePositiveAdjustment",
    "MIN_BASELINE_WINDOW_DAYS",
    "MIN_BASELINE_WINDOW_MS",
    "SignatureSnapshot",
    "SignatureStore",
    "TriageIndicator",
    "TriageScore",
    "WEIGHT_FLOOR_FRACTION",
    "aggregate_baseline",
    "build_signature_snapshot",
    "check_baseline_market_eligibility",
    "compute_drift",
    "get_signature_store",
    "set_signature_store",
    "signature_similarity",
]
