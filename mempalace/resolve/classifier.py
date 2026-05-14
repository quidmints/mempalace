"""
Resolvability classifier.

Per R3 §3.2: the market creator submits a question; the classifier
returns one of four classifications and a JSON of available pipeline
paths. First-class component, not bootstrap-deferred.

Two consumer paths share the same trained classifier:
  - **Markets**: classifies at qualification time
  - **Montage layer**: classifies whether a draft pipeline can complete
    using only privacy-preserving inference

Bootstrap: cold-start uses frontier LLM + synthetic training data.
Once outcomes accumulate, the classifier fine-tunes against actual
resolution outcomes and runs locally for privacy-preserving paths.

Spec ref: R3 §3.2.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum


# =============================================================================
# Classification outcomes
# =============================================================================


class ResolvabilityClass(str, Enum):
    """Per R3 §3.2."""

    PUBLIC_LLM_RESOLVABLE = "public_llm_resolvable"
    PRIVACY_PRESERVING_REQUIRED = "privacy_preserving_required"
    JURY_ONLY = "jury_only"
    NOT_RESOLVABLE = "not_resolvable"


# =============================================================================
# Pipeline path
# =============================================================================


@dataclass
class PipelinePath:
    """One concrete pipeline that could resolve the question."""

    pipeline_id: str
    steps: list[str]                          # ordered step names
    privacy_mode: str                         # "public" | "local_only" | "sandbox"
    expected_confidence: float                # in [0,1]
    estimated_cost_units: float = 0.0
    notes: str = ""


@dataclass
class ClassificationResult:
    """Returned by the classifier."""

    classification: ResolvabilityClass
    confidence: float                         # in [0,1]
    available_pipelines: list[PipelinePath] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)
    classifier_version: str = "0.1.0"
    classifier_source: str = "frontier_llm"   # "frontier_llm" | "local_finetune"


# =============================================================================
# Heuristic bootstrap classifier
# =============================================================================


# Simple keyword cues for cold-start classification. Replaced by a
# trained classifier as outcomes accumulate (R3 §3.2).
_PALACE_CONTENT_KEYWORDS = (
    "i ", "my ", "me ", "subject's", "user's", "their drawer", "their period",
    "their schema", "their montage", "their feed",
)

_NOT_RESOLVABLE_KEYWORDS = (
    "consciousness", "soul", "free will", "metaphysical", "in essence",
    "objectively true", "the truth about",
)

_JURY_KEYWORDS = (
    "creative interpretation", "subjective preference",
    "best film", "most beautiful", "most important",
    "should they", "ought to", "morally",
)


def _heuristic_classify(question: str) -> tuple[ResolvabilityClass, float, list[str]]:
    q = question.lower()
    rejected: list[str] = []

    if any(k in q for k in _NOT_RESOLVABLE_KEYWORDS):
        return ResolvabilityClass.NOT_RESOLVABLE, 0.7, [
            "question references concepts outside the available evidence model"
        ]

    if any(k in q for k in _JURY_KEYWORDS):
        return ResolvabilityClass.JURY_ONLY, 0.6, [
            "question requires interpretive judgment"
        ]

    if any(k in q for k in _PALACE_CONTENT_KEYWORDS):
        return ResolvabilityClass.PRIVACY_PRESERVING_REQUIRED, 0.7, []

    return ResolvabilityClass.PUBLIC_LLM_RESOLVABLE, 0.6, []


# =============================================================================
# Classifier
# =============================================================================


# Caller injects a real classifier (e.g. LLM or local finetune); this
# is the seam between the public API and the inference backend.
ClassifierFn = Callable[[str], tuple[ResolvabilityClass, float, list[str]]]


def _default_pipelines_for(
    classification: ResolvabilityClass,
) -> list[PipelinePath]:
    """Reasonable default pipeline-paths per classification."""
    if classification == ResolvabilityClass.PUBLIC_LLM_RESOLVABLE:
        return [
            PipelinePath(
                pipeline_id="public_llm_only",
                steps=["web_search", "llm_resolve"],
                privacy_mode="public",
                expected_confidence=0.85,
                estimated_cost_units=1.0,
            ),
        ]
    if classification == ResolvabilityClass.PRIVACY_PRESERVING_REQUIRED:
        return [
            PipelinePath(
                pipeline_id="local_only",
                steps=["palace_query", "local_llm_resolve"],
                privacy_mode="local_only",
                expected_confidence=0.8,
                estimated_cost_units=0.5,
            ),
            PipelinePath(
                pipeline_id="sandbox",
                steps=[
                    "sandbox_provision",
                    "load_foreign_slice",
                    "structural_match",
                    "sandbox_resolve",
                ],
                privacy_mode="sandbox",
                expected_confidence=0.85,
                estimated_cost_units=2.0,
            ),
        ]
    if classification == ResolvabilityClass.JURY_ONLY:
        return [
            PipelinePath(
                pipeline_id="jury",
                steps=["mode_jury_only"],
                privacy_mode="public",
                expected_confidence=0.5,
                estimated_cost_units=5.0,
                notes="manual jury review required",
            ),
        ]
    return []


class ResolvabilityClassifier:
    """First-class resolvability classifier.

    The classifier is a `ClassifierFn`; this wrapper handles pipeline
    selection and result packaging.
    """

    def __init__(
        self,
        *,
        classifier_fn: ClassifierFn | None = None,
        version: str = "0.1.0",
        source: str = "frontier_llm",
    ) -> None:
        self._classifier_fn = classifier_fn or _heuristic_classify
        self._version = version
        self._source = source

    def classify(self, question: str) -> ClassificationResult:
        cls, conf, reasons = self._classifier_fn(question)
        pipelines = _default_pipelines_for(cls)
        return ClassificationResult(
            classification=cls,
            confidence=conf,
            available_pipelines=pipelines,
            rejected_reasons=reasons,
            classifier_version=self._version,
            classifier_source=self._source,
        )


__all__ = [
    "ClassificationResult",
    "ClassifierFn",
    "PipelinePath",
    "ResolvabilityClass",
    "ResolvabilityClassifier",
]
