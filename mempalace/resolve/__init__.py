"""
mempalace.resolve — resolvability classifier + resolution stack.

Per Part 13 / R3 §2-§3: the resolution subsystem is a specialization of
the generic Stack framework. It owns:

  classifier         — 4-way resolvability classifier + pipeline picker
  stack              — composes EvidenceVerify → inference → formula
                       → encode → feedback into a runnable Stack
  formula_registry   — 19 formula types from deterministic_resolve.py
                       plus 3 baseline-market formulas (R3 §5.4)
  deterministic      — formula evaluators against EvidenceSummary
  evidence_verify    — hardware-attestation chain check (Step wrapper)
  feedback           — feedback_recorded emission (credit-assignment root)
  market_state       — off-chain mirror of on-chain markets
  model_registry     — model attestation, weights-hash, version tracking
  device_context     — device identity + capability advertisement
  encode_result      — off-chain → on-chain (Borsh-compatible) encoder
  inference_steps    — TranscribeStep / ClassifyStep / VisualClassifyStep /
                       DocVerifyStep / MultimodalStep / WebSearchStep

Spec ref: Part 13, R3 §2 / §3.
"""

from .classifier import (
    ClassificationResult,
    PipelinePath,
    ResolvabilityClass,
    ResolvabilityClassifier,
)
from .deterministic import (
    MIN_RESOLUTION_CONFIDENCE_BPS,
    evaluate_formula,
    register_default_evaluators,
)
from .device_context import (
    DeviceCapability,
    DeviceContext,
    detect_platform,
    get_device_context,
    make_default_context,
    set_device_context,
)
from .encode_result import (
    ATTESTATION_HASH_SIZE,
    EncodedResolution,
    MAX_REASON_BYTES,
    METHOD_CODES,
    decode_resolution,
    encode_resolution,
)
from .evidence_verify import (
    AttestedEvidence,
    EvidenceVerifier,
    EvidenceVerifyStep,
)
from .feedback import (
    ResolutionFeedback,
    ResolutionFeedbackEmitter,
    snapshot_interpretation_versions,
)
from .formula_registry import (
    EvaluatorFn,
    EvidenceSummary,
    FormulaRegistry,
    FormulaType,
    Outcome,
    ResolutionFormula,
    TagCondition,
    VetoCondition,
    get_formula_registry,
    reset_formula_registry,
)
from .inference_steps import (
    ClassifyStep,
    DocVerifyStep,
    MultimodalStep,
    TranscribeStep,
    VisualClassifyStep,
    WebSearchStep,
    make_default_inference_stack,
)
from .market_state import (
    Market,
    MarketKind,
    MarketStateStore,
    MarketStatus,
    get_market_state_store,
    ingest_chain_update,
    set_market_state_store,
)
from .model_registry import (
    ModelClass,
    ModelEntry,
    ModelRegistry,
    get_model_registry,
    set_model_registry,
)
from .stack import (
    EncodeResultStep,
    FeedbackEmitStep,
    FormulaStep,
    ResolutionStack,
    ResolutionStackPlan,
    run_resolution,
)

__all__ = [
    # classifier
    "ClassificationResult",
    "PipelinePath",
    "ResolvabilityClass",
    "ResolvabilityClassifier",
    # deterministic
    "MIN_RESOLUTION_CONFIDENCE_BPS",
    "evaluate_formula",
    "register_default_evaluators",
    # device_context
    "DeviceCapability",
    "DeviceContext",
    "detect_platform",
    "get_device_context",
    "make_default_context",
    "set_device_context",
    # encode_result
    "ATTESTATION_HASH_SIZE",
    "EncodedResolution",
    "MAX_REASON_BYTES",
    "METHOD_CODES",
    "decode_resolution",
    "encode_resolution",
    # evidence_verify
    "AttestedEvidence",
    "EvidenceVerifier",
    "EvidenceVerifyStep",
    # feedback
    "ResolutionFeedback",
    "ResolutionFeedbackEmitter",
    "snapshot_interpretation_versions",
    # formula_registry
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
    # inference_steps
    "ClassifyStep",
    "DocVerifyStep",
    "MultimodalStep",
    "TranscribeStep",
    "VisualClassifyStep",
    "WebSearchStep",
    "make_default_inference_stack",
    # market_state
    "Market",
    "MarketKind",
    "MarketStateStore",
    "MarketStatus",
    "get_market_state_store",
    "ingest_chain_update",
    "set_market_state_store",
    # model_registry
    "ModelClass",
    "ModelEntry",
    "ModelRegistry",
    "get_model_registry",
    "set_model_registry",
    # stack
    "EncodeResultStep",
    "FeedbackEmitStep",
    "FormulaStep",
    "ResolutionStack",
    "ResolutionStackPlan",
    "run_resolution",
]
