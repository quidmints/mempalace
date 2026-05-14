"""
Formula registry.

Per R3 §2.4 / §5.4: 19 formula types from the existing
`deterministic_resolve.py`, plus baseline-formula entries for
behavior-vs-baseline markets.

Each formula is identified by a `formula_id` and binds to an
evaluator function with signature

    evaluator(formula: ResolutionFormula, evidence: EvidenceSummary) -> Outcome

The registry is consulted by `deterministic.py` at resolution time.

Spec ref: R3 §2.4, §5.4.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


# =============================================================================
# Formula types
# =============================================================================


class FormulaType(str):
    """Names mirror the existing 19 types in deterministic_resolve.py."""

    TAG_THRESHOLD = "TAG_THRESHOLD"
    TAG_RATIO = "TAG_RATIO"
    MULTI_TAG_AND = "MULTI_TAG_AND"
    MULTI_TAG_OR = "MULTI_TAG_OR"
    DOMAIN_THRESHOLD = "DOMAIN_THRESHOLD"
    DOMAIN_RATIO = "DOMAIN_RATIO"
    CONVERSATION = "CONVERSATION"
    TREND = "TREND"
    PIPELINE_MATCH = "PIPELINE_MATCH"
    VISUAL_THRESHOLD = "VISUAL_THRESHOLD"
    OBJECT_PRESENT = "OBJECT_PRESENT"
    SCENE_MATCH = "SCENE_MATCH"
    DOCUMENT_PRESENT = "DOCUMENT_PRESENT"
    TEXT_CONTAINS = "TEXT_CONTAINS"
    LOCATION_CONFIRMED = "LOCATION_CONFIRMED"
    ENVIRONMENTAL_RANGE = "ENVIRONMENTAL_RANGE"
    MODALITY_CONSISTENCY = "MODALITY_CONSISTENCY"
    WITNESS_THRESHOLD = "WITNESS_THRESHOLD"
    TRAIT_SCORE = "TRAIT_SCORE"

    # Baseline-market formulas (R3 §5.4)
    BASELINE_VELOCITY_SD = "BASELINE_VELOCITY_SD"
    BASELINE_DRIFT_AXIS = "BASELINE_DRIFT_AXIS"
    BASELINE_CONTRADICTION_RATE = "BASELINE_CONTRADICTION_RATE"


# =============================================================================
# Outcome encoding
# =============================================================================


class Outcome(IntEnum):
    YES = 0
    NO = 1
    INDETERMINATE = -1


# =============================================================================
# Conditions and formulas
# =============================================================================


@dataclass
class TagCondition:
    """One tag-level condition (mirrors deterministic_resolve.py)."""

    tag_name: str = ""
    tag_hash: str = ""
    domain: str = ""
    content_types: list[int] = field(default_factory=list)
    min_bps: int = 0
    min_ratio: float = 0.0
    min_count: int = 0
    min_value: float = 0.0
    max_value: float = 0.0
    text_pattern: str = ""
    direction: str = ""
    weight: float = 1.0


@dataclass
class VetoCondition:
    tag_name: str = ""
    tag_hash: str = ""
    domain: str = ""
    max_ratio: float = 0.0


@dataclass
class ResolutionFormula:
    """A formula entry."""

    formula_id: str                                # registry key
    formula_type: str                              # one of FormulaType.*
    conditions: list[TagCondition] = field(default_factory=list)
    min_sessions: int = 1
    min_devices: int = 1
    veto_tags: list[VetoCondition] = field(default_factory=list)
    antispoof_veto: bool = True
    trend_window: int = 0
    required_pipeline: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Evidence summary
# =============================================================================


@dataclass
class EvidenceSummary:
    """Compact summary of evidence used by formula evaluators.

    The full content is in the underlying drawers/edges; this is the
    aggregated view a formula sees.
    """

    tag_counts: dict[str, int] = field(default_factory=dict)
    tag_bps: dict[str, int] = field(default_factory=dict)
    tag_ratios: dict[str, float] = field(default_factory=dict)
    domain_counts: dict[str, int] = field(default_factory=dict)
    domain_bps: dict[str, int] = field(default_factory=dict)
    text_blobs: list[str] = field(default_factory=list)
    visual_scores: dict[str, float] = field(default_factory=dict)
    object_present: dict[str, bool] = field(default_factory=dict)
    scene_match: dict[str, float] = field(default_factory=dict)
    document_present: dict[str, bool] = field(default_factory=dict)
    location_confirmed: dict[str, bool] = field(default_factory=dict)
    environmental: dict[str, float] = field(default_factory=dict)
    witness_count_per_event: dict[str, int] = field(default_factory=dict)
    session_count: int = 0
    device_count: int = 0
    trait_scores: dict[str, float] = field(default_factory=dict)

    # Baseline market evidence (R3 §5.4)
    baseline_axis_drift: dict[str, float] = field(default_factory=dict)
    baseline_velocity_sd: float | None = None
    baseline_contradiction_rate: float | None = None


# =============================================================================
# Evaluator type and registry
# =============================================================================


# Each evaluator returns (outcome, confidence_bps, reason)
EvaluatorFn = Callable[
    [ResolutionFormula, EvidenceSummary],
    tuple[Outcome, int, str],
]


class FormulaRegistry:
    """Maps formula_id → (formula, evaluator) pairs."""

    def __init__(self) -> None:
        self._by_id: dict[str, tuple[ResolutionFormula, EvaluatorFn]] = {}

    def register(
        self,
        formula: ResolutionFormula,
        evaluator: EvaluatorFn,
    ) -> None:
        self._by_id[formula.formula_id] = (formula, evaluator)

    def get(self, formula_id: str) -> tuple[ResolutionFormula, EvaluatorFn] | None:
        return self._by_id.get(formula_id)

    def all_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def size(self) -> int:
        return len(self._by_id)


_REGISTRY: FormulaRegistry | None = None


def get_formula_registry() -> FormulaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = FormulaRegistry()
    return _REGISTRY


def reset_formula_registry() -> None:
    """Test-only: drop the singleton."""
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "EvaluatorFn",
    "EvidenceSummary",
    "FormulaRegistry",
    "FormulaType",
    "Outcome",
    "ResolutionFormula",
    "TagCondition",
    "VetoCondition",
    "get_formula_registry",
    "reset_formula_registry",
]
