"""
Deterministic formula execution.

Per R3 §2.2: loads formulas from `formula_registry.py` and evaluates
them against an `EvidenceSummary`. This is the deterministic resolver
— preferred when the formula can produce a confident outcome; falls
through to the LLM resolver otherwise.

This module ships:

  - Built-in evaluators for all 19 + 3 baseline formula types.
  - `register_default_evaluators()` to seed them into the registry.
  - `evaluate_formula()` — the thin entry point used by the resolution
    stack.

Spec ref: R3 §2.2.
"""

from __future__ import annotations

from .formula_registry import (
    EvidenceSummary,
    FormulaRegistry,
    FormulaType,
    Outcome,
    ResolutionFormula,
    get_formula_registry,
)


# =============================================================================
# Constants (mirror deterministic_resolve.py)
# =============================================================================


MIN_RESOLUTION_CONFIDENCE_BPS = 7_000           # 70%


# =============================================================================
# Veto check
# =============================================================================


def _veto_triggered(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[bool, str]:
    """Return (veto_fired, reason)."""
    for v in formula.veto_tags or []:
        ratio = evidence.tag_ratios.get(v.tag_name) or evidence.tag_ratios.get(v.tag_hash)
        if ratio is None:
            continue
        if ratio > v.max_ratio:
            return True, f"veto: {v.tag_name} ratio {ratio:.2f} > {v.max_ratio:.2f}"
    return False, ""


# =============================================================================
# Per-type evaluators (compact: each returns (Outcome, bps, reason))
# =============================================================================


def _eval_tag_threshold(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "tag_threshold: no conditions"
    cond = formula.conditions[0]
    bps = evidence.tag_bps.get(cond.tag_name) or evidence.tag_bps.get(cond.tag_hash) or 0
    if bps >= cond.min_bps:
        return Outcome.YES, bps, f"tag {cond.tag_name} bps={bps} ≥ {cond.min_bps}"
    return Outcome.NO, bps, f"tag {cond.tag_name} bps={bps} < {cond.min_bps}"


def _eval_tag_ratio(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "tag_ratio: no conditions"
    cond = formula.conditions[0]
    ratio = evidence.tag_ratios.get(cond.tag_name) or evidence.tag_ratios.get(cond.tag_hash) or 0.0
    bps = int(ratio * 10_000)
    if ratio >= cond.min_ratio:
        return Outcome.YES, bps, f"ratio {ratio:.2f} ≥ {cond.min_ratio:.2f}"
    return Outcome.NO, bps, f"ratio {ratio:.2f} < {cond.min_ratio:.2f}"


def _eval_multi_tag_and(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "multi_tag_and: no conditions"
    bps_min = 10_000
    for c in formula.conditions:
        bps = evidence.tag_bps.get(c.tag_name) or evidence.tag_bps.get(c.tag_hash) or 0
        if bps < c.min_bps:
            return Outcome.NO, bps, f"multi_tag_and: {c.tag_name} fails ({bps} < {c.min_bps})"
        bps_min = min(bps_min, bps)
    return Outcome.YES, bps_min, "all tag thresholds met"


def _eval_multi_tag_or(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "multi_tag_or: no conditions"
    best_bps = 0
    for c in formula.conditions:
        bps = evidence.tag_bps.get(c.tag_name) or evidence.tag_bps.get(c.tag_hash) or 0
        if bps >= c.min_bps:
            return Outcome.YES, bps, f"multi_tag_or: {c.tag_name} satisfied"
        best_bps = max(best_bps, bps)
    return Outcome.NO, best_bps, "no tag threshold met"


def _eval_domain_threshold(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "domain_threshold: no conditions"
    cond = formula.conditions[0]
    bps = evidence.domain_bps.get(cond.domain) or 0
    if bps >= cond.min_bps:
        return Outcome.YES, bps, f"domain {cond.domain} bps={bps} ≥ {cond.min_bps}"
    return Outcome.NO, bps, f"domain {cond.domain} bps={bps} < {cond.min_bps}"


def _eval_domain_ratio(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "domain_ratio: no conditions"
    cond = formula.conditions[0]
    total = sum(evidence.domain_counts.values())
    if total == 0:
        return Outcome.INDETERMINATE, 0, "domain_ratio: no domain evidence"
    ratio = evidence.domain_counts.get(cond.domain, 0) / total
    bps = int(ratio * 10_000)
    if ratio >= cond.min_ratio:
        return Outcome.YES, bps, f"domain ratio {ratio:.2f} ≥ {cond.min_ratio:.2f}"
    return Outcome.NO, bps, f"domain ratio {ratio:.2f} < {cond.min_ratio:.2f}"


def _eval_text_contains(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "text_contains: no conditions"
    cond = formula.conditions[0]
    pat = cond.text_pattern.lower()
    matches = sum(1 for t in evidence.text_blobs if pat in t.lower())
    if matches >= max(1, cond.min_count):
        return Outcome.YES, 9_000, f"text_contains: {matches} matches"
    return Outcome.NO, 0, f"text_contains: {matches} matches < {cond.min_count}"


def _eval_visual_threshold(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "visual_threshold: no conditions"
    cond = formula.conditions[0]
    score = evidence.visual_scores.get(cond.tag_name) or 0.0
    bps = int(score * 10_000)
    if score >= cond.min_value:
        return Outcome.YES, bps, f"visual_threshold {score:.2f} ≥ {cond.min_value:.2f}"
    return Outcome.NO, bps, f"visual_threshold {score:.2f} < {cond.min_value:.2f}"


def _eval_object_present(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "object_present: no conditions"
    cond = formula.conditions[0]
    present = bool(evidence.object_present.get(cond.tag_name))
    return (Outcome.YES if present else Outcome.NO), (9_500 if present else 500), (
        f"object_present: {cond.tag_name}={present}"
    )


def _eval_scene_match(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "scene_match: no conditions"
    cond = formula.conditions[0]
    score = evidence.scene_match.get(cond.tag_name) or 0.0
    bps = int(score * 10_000)
    if score >= cond.min_value:
        return Outcome.YES, bps, f"scene_match {score:.2f}"
    return Outcome.NO, bps, f"scene_match {score:.2f} below {cond.min_value:.2f}"


def _eval_document_present(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "document_present: no conditions"
    cond = formula.conditions[0]
    present = bool(evidence.document_present.get(cond.tag_name))
    return (Outcome.YES if present else Outcome.NO), (9_500 if present else 500), (
        f"document_present: {cond.tag_name}={present}"
    )


def _eval_location_confirmed(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "location_confirmed: no conditions"
    cond = formula.conditions[0]
    confirmed = bool(evidence.location_confirmed.get(cond.tag_name))
    return (Outcome.YES if confirmed else Outcome.NO), (9_500 if confirmed else 500), (
        f"location_confirmed: {cond.tag_name}={confirmed}"
    )


def _eval_environmental_range(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "environmental_range: no conditions"
    cond = formula.conditions[0]
    val = evidence.environmental.get(cond.tag_name)
    if val is None:
        return Outcome.INDETERMINATE, 0, "environmental_range: no measurement"
    if cond.min_value <= val <= cond.max_value:
        return Outcome.YES, 9_000, f"env {val} in [{cond.min_value}, {cond.max_value}]"
    return Outcome.NO, 1_000, f"env {val} outside [{cond.min_value}, {cond.max_value}]"


def _eval_witness_threshold(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "witness_threshold: no conditions"
    cond = formula.conditions[0]
    counts = evidence.witness_count_per_event
    qualifying = sum(1 for v in counts.values() if v >= cond.min_count)
    if qualifying >= max(1, cond.min_count):
        return Outcome.YES, 9_000, f"witness_threshold: {qualifying} qualifying events"
    return Outcome.NO, 0, f"witness_threshold: {qualifying} < {cond.min_count}"


def _eval_trait_score(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "trait_score: no conditions"
    cond = formula.conditions[0]
    score = evidence.trait_scores.get(cond.tag_name) or 0.0
    bps = int(score * 10_000)
    if score >= cond.min_value:
        return Outcome.YES, bps, f"trait_score {score:.2f} ≥ {cond.min_value:.2f}"
    return Outcome.NO, bps, f"trait_score {score:.2f} < {cond.min_value:.2f}"


def _eval_modality_consistency(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """Pass if at least 2 modalities agree (have evidence above threshold)."""
    visual_ok = any(s >= 0.5 for s in evidence.visual_scores.values())
    text_ok = bool(evidence.text_blobs)
    doc_ok = any(evidence.document_present.values())
    location_ok = any(evidence.location_confirmed.values())
    modality_count = sum([visual_ok, text_ok, doc_ok, location_ok])
    if modality_count >= 2:
        return Outcome.YES, 8_500, f"modality_consistency: {modality_count} modalities"
    return Outcome.NO, 1_500, f"modality_consistency: only {modality_count}"


def _eval_pipeline_match(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """Pipeline-match: requires the resolution to be performed by a
    specific pipeline (verified upstream); deterministic step
    treats missing required_pipeline as INDETERMINATE."""
    if not formula.required_pipeline:
        return Outcome.INDETERMINATE, 0, "pipeline_match: no required_pipeline"
    return Outcome.YES, 9_000, f"pipeline_match: {formula.required_pipeline} required"


def _eval_conversation(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """Conversation: needs >= min_sessions/min_devices."""
    if evidence.session_count < formula.min_sessions:
        return Outcome.NO, 0, (
            f"conversation: sessions {evidence.session_count} < {formula.min_sessions}"
        )
    if evidence.device_count < formula.min_devices:
        return Outcome.NO, 0, (
            f"conversation: devices {evidence.device_count} < {formula.min_devices}"
        )
    return Outcome.YES, 8_000, "conversation: thresholds met"


def _eval_trend(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """Trend: needs trend_window > 0 + at least one tag with directional bps."""
    if formula.trend_window <= 0:
        return Outcome.INDETERMINATE, 0, "trend: no window"
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "trend: no conditions"
    cond = formula.conditions[0]
    bps = evidence.tag_bps.get(cond.tag_name) or 0
    direction = (cond.direction or "").lower()
    if direction == "up" and bps >= cond.min_bps:
        return Outcome.YES, bps, f"trend up: bps={bps}"
    if direction == "down" and bps <= cond.min_bps:
        return Outcome.YES, bps, f"trend down: bps={bps}"
    return Outcome.NO, bps, f"trend: direction={direction} bps={bps}"


# Baseline-formula evaluators (R3 §5.4)


def _eval_baseline_velocity_sd(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """The baseline market resolves YES when the subject's velocity
    along the named axis differs from baseline by ≥ N standard
    deviations. The pre-computed value is in evidence."""
    if evidence.baseline_velocity_sd is None:
        return Outcome.INDETERMINATE, 0, "baseline_velocity_sd: no measurement"
    threshold = (
        formula.metadata.get("min_sd")
        or (formula.conditions[0].min_value if formula.conditions else 2.0)
    )
    if abs(evidence.baseline_velocity_sd) >= threshold:
        return Outcome.YES, 9_000, (
            f"velocity_sd {evidence.baseline_velocity_sd:.2f} ≥ {threshold}"
        )
    return Outcome.NO, 1_000, (
        f"velocity_sd {evidence.baseline_velocity_sd:.2f} < {threshold}"
    )


def _eval_baseline_drift_axis(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    if not formula.conditions:
        return Outcome.INDETERMINATE, 0, "baseline_drift_axis: no conditions"
    cond = formula.conditions[0]
    drift = evidence.baseline_axis_drift.get(cond.tag_name)
    if drift is None:
        return Outcome.INDETERMINATE, 0, "baseline_drift_axis: no axis measurement"
    bps = int(min(1.0, drift) * 10_000)
    if drift >= cond.min_value:
        return Outcome.YES, bps, f"drift on {cond.tag_name}={drift:.2f}"
    return Outcome.NO, bps, f"drift on {cond.tag_name}={drift:.2f} below threshold"


def _eval_baseline_contradiction_rate(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    rate = evidence.baseline_contradiction_rate
    if rate is None:
        return Outcome.INDETERMINATE, 0, "no contradiction-rate measurement"
    threshold = formula.metadata.get("min_rate") or 0.5
    if rate >= threshold:
        return Outcome.YES, int(rate * 10_000), (
            f"contradiction_rate {rate:.2f} ≥ {threshold}"
        )
    return Outcome.NO, int(rate * 10_000), (
        f"contradiction_rate {rate:.2f} < {threshold}"
    )


# =============================================================================
# Type → evaluator mapping
# =============================================================================


_EVALUATORS = {
    FormulaType.TAG_THRESHOLD: _eval_tag_threshold,
    FormulaType.TAG_RATIO: _eval_tag_ratio,
    FormulaType.MULTI_TAG_AND: _eval_multi_tag_and,
    FormulaType.MULTI_TAG_OR: _eval_multi_tag_or,
    FormulaType.DOMAIN_THRESHOLD: _eval_domain_threshold,
    FormulaType.DOMAIN_RATIO: _eval_domain_ratio,
    FormulaType.CONVERSATION: _eval_conversation,
    FormulaType.TREND: _eval_trend,
    FormulaType.PIPELINE_MATCH: _eval_pipeline_match,
    FormulaType.VISUAL_THRESHOLD: _eval_visual_threshold,
    FormulaType.OBJECT_PRESENT: _eval_object_present,
    FormulaType.SCENE_MATCH: _eval_scene_match,
    FormulaType.DOCUMENT_PRESENT: _eval_document_present,
    FormulaType.TEXT_CONTAINS: _eval_text_contains,
    FormulaType.LOCATION_CONFIRMED: _eval_location_confirmed,
    FormulaType.ENVIRONMENTAL_RANGE: _eval_environmental_range,
    FormulaType.MODALITY_CONSISTENCY: _eval_modality_consistency,
    FormulaType.WITNESS_THRESHOLD: _eval_witness_threshold,
    FormulaType.TRAIT_SCORE: _eval_trait_score,
    FormulaType.BASELINE_VELOCITY_SD: _eval_baseline_velocity_sd,
    FormulaType.BASELINE_DRIFT_AXIS: _eval_baseline_drift_axis,
    FormulaType.BASELINE_CONTRADICTION_RATE: _eval_baseline_contradiction_rate,
}


# =============================================================================
# Public entry points
# =============================================================================


def evaluate_formula(
    formula: ResolutionFormula,
    evidence: EvidenceSummary,
) -> tuple[Outcome, int, str]:
    """Evaluate a single formula against the evidence summary.

    Veto check first; veto fires → INDETERMINATE.
    Type-specific evaluator next.
    Confidence below MIN_RESOLUTION_CONFIDENCE_BPS → INDETERMINATE
    (the LLM resolver is the fallback path, R3 §2.2).
    """
    veto_fired, veto_reason = _veto_triggered(formula, evidence)
    if veto_fired:
        return Outcome.INDETERMINATE, 0, veto_reason

    evaluator = _EVALUATORS.get(formula.formula_type)
    if evaluator is None:
        return Outcome.INDETERMINATE, 0, f"unknown formula type: {formula.formula_type}"

    outcome, bps, reason = evaluator(formula, evidence)
    if outcome != Outcome.INDETERMINATE and bps < MIN_RESOLUTION_CONFIDENCE_BPS:
        return Outcome.INDETERMINATE, bps, (
            f"insufficient confidence: {bps} < {MIN_RESOLUTION_CONFIDENCE_BPS} ({reason})"
        )
    return outcome, bps, reason


def register_default_evaluators(registry: FormulaRegistry | None = None) -> int:
    """Register every type's evaluator under formula_id == FormulaType name.

    Convenient for tests; production builds register specific formulas.
    """
    reg = registry or get_formula_registry()
    count = 0
    for ftype, evaluator in _EVALUATORS.items():
        f = ResolutionFormula(formula_id=ftype, formula_type=ftype)
        reg.register(f, evaluator)
        count += 1
    return count


__all__ = [
    "MIN_RESOLUTION_CONFIDENCE_BPS",
    "evaluate_formula",
    "register_default_evaluators",
]
