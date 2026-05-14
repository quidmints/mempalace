"""
Resolution stack — specialized Stack with composition rules.

Per R3 §2.2: the resolution stack is the canonical sequence

    EvidenceVerifyStep → [inference steps] → FormulaStep → EncodeResultStep
                       → FeedbackEmitStep

Composition rules are formula-driven: the formula declares its
required pipeline (e.g. TRANSCRIBE+CLASSIFY for affect, TRANSCRIBE+
DOCVERIFY for text-match), and `ResolutionStack.for_formula()`
assembles the matching plan. Privacy gating is inherited from
`mempalace.stack.stack.Stack`: any step with `requires_external=True`
will be rejected outside `PrivacyMode.EXTERNAL`.

Spec ref: R3 §2.2.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..stack.context import PrivacyMode, StackContext
from ..stack.stack import Stack, StackResult
from ..stack.step import BaseStep, Step, StepManifest, StepResult
from .deterministic import evaluate_formula
from .encode_result import encode_resolution
from .evidence_verify import EvidenceVerifyStep
from .feedback import (
    ResolutionFeedback,
    ResolutionFeedbackEmitter,
    snapshot_interpretation_versions,
)
from .formula_registry import (
    EvidenceSummary,
    FormulaRegistry,
    Outcome,
    ResolutionFormula,
    get_formula_registry,
)
from .inference_steps import (
    ClassifyStep,
    DocVerifyStep,
    MultimodalStep,
    TranscribeStep,
    VisualClassifyStep,
    WebSearchStep,
)


# =============================================================================
# Inference-step taxonomy
# =============================================================================


# Map an inference-pipeline tag (used by formula.required_pipeline and the
# classifier's pipeline_options) to a default Step factory.
_INFERENCE_FACTORIES: dict[str, type[BaseStep]] = {
    "transcribe": TranscribeStep,
    "classify": ClassifyStep,
    "visual_classify": VisualClassifyStep,
    "doc_verify": DocVerifyStep,
    "multimodal": MultimodalStep,
    "web_search": WebSearchStep,
}


def _parse_pipeline_spec(required_pipeline: str) -> list[str]:
    """Parse `formula.required_pipeline` into an ordered list of step keys.

    Accepts either:
      - "+"-separated tokens, e.g. "transcribe+classify"
      - empty string → empty list (no inference steps required)
    """
    if not required_pipeline:
        return []
    return [t.strip().lower() for t in required_pipeline.split("+") if t.strip()]


# =============================================================================
# FormulaStep — runs the formula evaluator
# =============================================================================


class FormulaStep(BaseStep):
    """Run a registered formula evaluator against the evidence summary.

    Inputs (from ctx.inputs or upstream outputs):
      - "evidence_summary" : EvidenceSummary required by the evaluator
      - "formula_id"       : optional override; falls back to constructor

    Outputs:
      - "outcome"           : int (-1=indeterminate, 0=YES, 1=NO)
      - "confidence_bps"    : int
      - "resolution_method" : "deterministic" | "veto" | "insufficient"
      - "resolution_reason" : str
    """

    name = "resolve.formula"

    def __init__(
        self,
        *,
        formula_id: str = "",
        registry: FormulaRegistry | None = None,
    ) -> None:
        self._formula_id = formula_id
        self._registry = registry or get_formula_registry()

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("evidence_summary",),
            inputs_optional=("formula_id",),
            outputs=(
                "outcome",
                "confidence_bps",
                "resolution_method",
                "resolution_reason",
            ),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        # Pull a single map of upstream values (inputs + previous outputs)
        merged: dict[str, Any] = {}
        merged.update(ctx.inputs or {})
        merged.update(ctx.outputs or {})

        evidence = merged.get("evidence_summary")
        if not isinstance(evidence, EvidenceSummary):
            return StepResult(success=False, error="missing or invalid evidence_summary")

        formula_id = merged.get("formula_id") or self._formula_id
        if not formula_id:
            return StepResult(success=False, error="no formula_id provided")

        entry = self._registry.get(formula_id)
        if entry is None:
            return StepResult(
                success=False, error=f"formula_id not registered: {formula_id}"
            )

        formula, _evaluator = entry
        outcome, bps, reason = evaluate_formula(formula, evidence)

        if outcome == Outcome.INDETERMINATE and bps == 0:
            method = "veto" if "veto" in reason else "insufficient"
        elif outcome == Outcome.INDETERMINATE:
            method = "insufficient"
        else:
            method = "deterministic"

        return StepResult(
            success=True,
            outputs={
                "outcome": int(outcome),
                "confidence_bps": int(bps),
                "resolution_method": method,
                "resolution_reason": reason,
            },
            metadata={"formula_id": formula_id, "formula_type": formula.formula_type},
        )


# =============================================================================
# EncodeResultStep — produces on-chain bytes
# =============================================================================


class EncodeResultStep(BaseStep):
    """Serialize the formula's outcome into on-chain bytes.

    Inputs:
      - "market_id"             : str (required)
      - "outcome"                : int
      - "confidence_bps"         : int
      - "resolution_method"      : str
      - "resolution_reason"      : str (optional, capped on encode)
      - "resolver_attestation_hash" : 32 bytes (optional)
    Outputs:
      - "encoded_resolution_bytes" : bytes
      - "resolution_at_ms"         : int
    """

    name = "resolve.encode_result"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("market_id", "outcome", "confidence_bps", "resolution_method"),
            inputs_optional=("resolution_reason", "resolver_attestation_hash"),
            outputs=("encoded_resolution_bytes", "resolution_at_ms"),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged: dict[str, Any] = {}
        merged.update(ctx.inputs or {})
        merged.update(ctx.outputs or {})

        market_id = merged.get("market_id")
        if not market_id:
            return StepResult(success=False, error="missing market_id")

        try:
            now_ms = int(time.time() * 1000)
            blob = encode_resolution(
                market_id=str(market_id),
                outcome=int(merged.get("outcome", -1)),
                confidence_bps=int(merged.get("confidence_bps", 0)),
                method=str(merged.get("resolution_method", "insufficient")),
                resolver_attestation_hash=merged.get("resolver_attestation_hash"),
                resolution_at_ms=now_ms,
                reason_summary=str(merged.get("resolution_reason", "")),
            )
        except Exception as e:  # noqa: BLE001
            return StepResult(success=False, error=f"{type(e).__name__}: {e}")

        return StepResult(
            success=True,
            outputs={
                "encoded_resolution_bytes": blob,
                "resolution_at_ms": now_ms,
            },
        )


# =============================================================================
# FeedbackEmitStep — emits feedback_recorded as the credit-assignment root
# =============================================================================


class FeedbackEmitStep(BaseStep):
    """Emit a `feedback_recorded` event capturing the interpretation
    versions active at resolution time. This is the root of the
    credit-assignment chain (R3 §2.2).
    """

    name = "resolve.feedback_emit"

    def __init__(
        self,
        *,
        consumer: str = "resolve",
        feedback_kind: str = "resolution_completed",
        emitter: ResolutionFeedbackEmitter | None = None,
    ) -> None:
        self._emitter = emitter or ResolutionFeedbackEmitter()
        self._consumer = consumer
        self._feedback_kind = feedback_kind

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("market_id",),
            inputs_optional=(
                "interpretation_versions",
                "outcome",
                "confidence_bps",
            ),
            outputs=("feedback_emitted",),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged: dict[str, Any] = {}
        merged.update(ctx.inputs or {})
        merged.update(ctx.outputs or {})

        versions = merged.get("interpretation_versions")
        if not isinstance(versions, dict):
            versions = snapshot_interpretation_versions()

        fb = ResolutionFeedback(
            artifact_id=str(merged.get("market_id", "")),
            consumer=self._consumer,
            feedback_kind=self._feedback_kind,
            feedback_value={
                "outcome": merged.get("outcome"),
                "confidence_bps": merged.get("confidence_bps"),
                "method": merged.get("resolution_method"),
            },
            interpretation_versions=dict(versions),
        )
        self._emitter.emit(fb)
        return StepResult(success=True, outputs={"feedback_emitted": True})


# =============================================================================
# ResolutionStack — specialized Stack with composition rules
# =============================================================================


@dataclass
class ResolutionStackPlan:
    """Result of `ResolutionStack.compose()` — the ordered Step list plus
    a small description of why each step was chosen.
    """

    steps: list[Step] = field(default_factory=list)
    chosen_inference_keys: list[str] = field(default_factory=list)
    formula_id: str = ""
    formula_type: str = ""


class ResolutionStack(Stack):
    """Specialized Stack for resolving a market.

    Composition rules:
      1. Always start with EvidenceVerifyStep.
      2. Append inference steps based on `formula.required_pipeline`.
         If the formula doesn't declare one, pick a sensible default
         from the formula type (e.g. TRANSCRIBE for affect, DOC_VERIFY
         for text-match). Unknown keys are skipped with a metadata note.
      3. Append FormulaStep bound to formula_id.
      4. Append EncodeResultStep.
      5. Append FeedbackEmitStep.

    Privacy mode is enforced by the parent class. WebSearchStep is
    included only when `EXTERNAL` privacy mode is acceptable AND the
    formula's pipeline declares it.
    """

    def __init__(
        self,
        plan: list[Step],
        *,
        name: str = "resolution_stack",
        stop_on_failure: bool = True,
    ) -> None:
        super().__init__(plan, name=name, stop_on_failure=stop_on_failure)

    # ---- composition ------------------------------------------------------

    @staticmethod
    def _default_pipeline_for_type(formula_type: str) -> list[str]:
        """Fallback pipeline keys when formula.required_pipeline is empty."""
        ftype = formula_type.upper()
        # Audio / affect / conversation
        if ftype in ("CONVERSATION", "TRAIT_SCORE", "TAG_THRESHOLD", "TAG_RATIO"):
            return ["transcribe", "classify", "multimodal"]
        # Text-pattern
        if ftype in ("TEXT_CONTAINS", "DOCUMENT_PRESENT"):
            return ["doc_verify"]
        # Visual
        if ftype in ("VISUAL_THRESHOLD", "OBJECT_PRESENT", "SCENE_MATCH"):
            return ["visual_classify", "multimodal"]
        # Web-checkable
        if ftype in ("PIPELINE_MATCH", "TREND"):
            return ["web_search"]
        # Baseline-market formulas don't go through external inference
        return []

    @classmethod
    def compose(
        cls,
        formula: ResolutionFormula,
        *,
        privacy_mode: PrivacyMode = PrivacyMode.EXTERNAL,
        registry: FormulaRegistry | None = None,
        evidence_verifier: EvidenceVerifyStep | None = None,
        feedback_emitter: ResolutionFeedbackEmitter | None = None,
        inference_factories: dict[str, type[BaseStep]] | None = None,
    ) -> ResolutionStackPlan:
        """Build a plan for the given formula under the given privacy mode."""
        factories = dict(inference_factories or _INFERENCE_FACTORIES)

        # 1. evidence verification
        plan: list[Step] = []
        plan.append(evidence_verifier or EvidenceVerifyStep())

        # 2. inference steps from formula.required_pipeline (or default)
        keys = _parse_pipeline_spec(formula.required_pipeline)
        if not keys:
            keys = cls._default_pipeline_for_type(formula.formula_type)
        chosen: list[str] = []
        for key in keys:
            factory = factories.get(key)
            if factory is None:
                continue
            step = factory()
            mf = step.declares()
            # Enforce privacy gating ahead of validate()
            if mf.requires_external and privacy_mode != PrivacyMode.EXTERNAL:
                continue
            if mf.requires_sandbox and privacy_mode != PrivacyMode.SANDBOX:
                continue
            plan.append(step)
            chosen.append(key)

        # 3. formula evaluation
        plan.append(FormulaStep(formula_id=formula.formula_id, registry=registry))

        # 4. encode → on-chain
        plan.append(EncodeResultStep())

        # 5. feedback emission
        plan.append(feedback_emitter and _EmitterWrapStep(feedback_emitter)
                    or FeedbackEmitStep())

        return ResolutionStackPlan(
            steps=plan,
            chosen_inference_keys=chosen,
            formula_id=formula.formula_id,
            formula_type=formula.formula_type,
        )

    @classmethod
    def for_formula(
        cls,
        formula_id: str,
        *,
        privacy_mode: PrivacyMode = PrivacyMode.EXTERNAL,
        registry: FormulaRegistry | None = None,
        evidence_verifier: EvidenceVerifyStep | None = None,
        feedback_emitter: ResolutionFeedbackEmitter | None = None,
        inference_factories: dict[str, type[BaseStep]] | None = None,
        stop_on_failure: bool = True,
    ) -> ResolutionStack:
        """Look up the formula in the registry and return an executable stack."""
        reg = registry or get_formula_registry()
        entry = reg.get(formula_id)
        if entry is None:
            raise ValueError(f"unknown formula_id: {formula_id}")
        formula, _ = entry
        plan = cls.compose(
            formula,
            privacy_mode=privacy_mode,
            registry=reg,
            evidence_verifier=evidence_verifier,
            feedback_emitter=feedback_emitter,
            inference_factories=inference_factories,
        )
        return cls(plan.steps, name=f"resolution_stack:{formula_id}", stop_on_failure=stop_on_failure)


class _EmitterWrapStep(FeedbackEmitStep):
    """Internal: FeedbackEmitStep with a pre-built emitter."""

    def __init__(self, emitter: ResolutionFeedbackEmitter) -> None:
        super().__init__(emitter=emitter)


# =============================================================================
# Convenience: one-shot run
# =============================================================================


async def run_resolution(
    formula_id: str,
    *,
    market_id: str,
    evidence_summary: EvidenceSummary,
    evidence_records: Iterable[Any] = (),
    privacy_mode: PrivacyMode = PrivacyMode.EXTERNAL,
    extra_inputs: dict[str, Any] | None = None,
) -> StackResult:
    """Compose a resolution stack for the formula and execute it.

    Conveniences for tests / one-shot use; production callers typically
    build the stack once and reuse it.
    """
    stack = ResolutionStack.for_formula(formula_id, privacy_mode=privacy_mode)
    inputs: dict[str, Any] = {
        "market_id": market_id,
        "evidence_summary": evidence_summary,
        "evidence_records": list(evidence_records),
    }
    if extra_inputs:
        inputs.update(extra_inputs)
    ctx = StackContext(inputs=inputs, privacy_mode=privacy_mode)
    return await stack.execute(ctx)


__all__ = [
    "EncodeResultStep",
    "FeedbackEmitStep",
    "FormulaStep",
    "ResolutionStack",
    "ResolutionStackPlan",
    "run_resolution",
]
