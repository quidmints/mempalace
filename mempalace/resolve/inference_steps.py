"""
Inference steps for the resolution stack.

Per R3 §2.3: external-pipeline integrations (web_search, doc_verify,
multimodal, visual_classify, classify, transcribe) become first-class
Steps in the resolution stack. Each one:

  - declares its IO contract via StepManifest
  - sets `requires_external=True` for network-touching ops (so they only
    run in EXTERNAL privacy mode)
  - reads structured inputs from `ctx.inputs` / `ctx.outputs`
  - writes structured outputs back to `ctx.outputs` for downstream steps

The original executors lived in `oracle/execution_plan/executors/`. They
took (step, input_results, session_context) and returned a StepResult
keyed on stringly-typed tags. This wrapping preserves the same logic
while normalizing the contract.

Network calls are stubbed at test time by setting `ctx.inputs["__stub_network"]`
to a dict — see tests for the convention.

Spec ref: R3 §2.3.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..stack.context import StackContext
from ..stack.step import BaseStep, StepManifest, StepResult


# =============================================================================
# Common helpers
# =============================================================================


def _tag(name: str, confidence_bps: int) -> dict[str, Any]:
    """Build a tag in the canonical UPPER_SNAKE_CASE form (sha256 id)."""
    return {
        "tag_name": name,
        "tag_id": hashlib.sha256(name.encode("utf-8")).hexdigest(),
        "confidence_bps": min(max(confidence_bps, 0), 9_999),
    }


def _gather_text(input_results: Mapping[str, Any]) -> str:
    """Pull text out of upstream step outputs."""
    parts: list[str] = []
    for v in input_results.values():
        if isinstance(v, dict) and v.get("text"):
            parts.append(str(v["text"]))
        elif isinstance(v, str):
            parts.append(v)
    return " ".join(parts).strip()


def _gather_tags(input_results: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Gather tags from upstream values.

    Accepts three shapes for any value in `input_results`:
      1. dict with a "tags" key whose value is a list of tag dicts
         (e.g. {"tags": [...], "text": "..."})
      2. list of tag dicts directly (e.g. transcribe_tags = [...])
      3. dict that itself looks like a single tag (has tag_name)
    """
    out: list[dict[str, Any]] = []
    for v in input_results.values():
        if isinstance(v, list):
            out.extend(t for t in v if isinstance(t, dict) and "tag_name" in t)
        elif isinstance(v, dict):
            if "tag_name" in v:
                out.append(v)
            else:
                tags = v.get("tags", [])
                if isinstance(tags, list):
                    out.extend(t for t in tags if isinstance(t, dict))
    return out


def _input_results_from_ctx(ctx: StackContext) -> dict[str, Any]:
    """Combine ctx.inputs and ctx.outputs into a single 'upstream' map."""
    merged: dict[str, Any] = {}
    merged.update(getattr(ctx, "inputs", {}) or {})
    merged.update(getattr(ctx, "outputs", {}) or {})
    return merged


# =============================================================================
# WebSearchStep
# =============================================================================


class WebSearchStep(BaseStep):
    """Web search via an injected fetch callback.

    The original executor called the Brave Search API directly. We don't
    pin a network client here — callers inject `fetch_fn(query, count) ->
    list[{"title","url","snippet"}]`. Pass `requires_external=True` so the
    step is gated by privacy mode.
    """

    name = "web_search"

    def __init__(
        self,
        fetch_fn: Callable[[str, int], list[dict[str, Any]]] | None = None,
        *,
        default_query: str = "",
        result_count: int = 5,
    ) -> None:
        self._fetch_fn = fetch_fn
        self._default_query = default_query
        self._result_count = result_count

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_optional=("query", "market_question", "resolution_source"),
            outputs=("search_results", "search_confidence_bps"),
            requires_external=True,
            description="Web search for resolution evidence.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        query = (
            merged.get("query")
            or " ".join(
                str(v) for v in (merged.get("market_question"), merged.get("resolution_source")) if v
            )[:200].strip()
            or self._default_query
        )
        if not query:
            return StepResult(success=False, error="no search query")

        if self._fetch_fn is None:
            # Stub path: caller didn't inject a fetcher → return empty
            return StepResult(
                success=True,
                outputs={"search_results": [], "search_confidence_bps": 0},
                metadata={"stubbed": True, "query": query},
            )

        try:
            raw = self._fetch_fn(query, self._result_count)
        except Exception as e:  # noqa: BLE001
            return StepResult(success=False, error=f"{type(e).__name__}: {e}")

        results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "snippet": str(r.get("snippet", "")),
            }
            for r in (raw or [])
        ]
        return StepResult(
            success=True,
            outputs={
                "search_results": results,
                "search_confidence_bps": 7_000 if results else 0,
            },
            metadata={"query": query, "result_count": len(results)},
        )


# =============================================================================
# DocVerifyStep
# =============================================================================


class DocVerifyStep(BaseStep):
    """Document verification — pattern matches against upstream OCR text."""

    name = "doc_verify"

    def __init__(self, *, text_pattern: str = "") -> None:
        self._text_pattern = text_pattern

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_optional=("text", "transcript", "text_pattern"),
            outputs=("doc_tags", "doc_text", "doc_confidence_bps"),
            description="Document presence/text-match check.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        text = merged.get("text") or merged.get("transcript") or _gather_text(merged)
        if not text:
            return StepResult(success=False, error="no text for doc verification")

        pattern = merged.get("text_pattern") or self._text_pattern
        doc_tag = _tag("DOCUMENT_PRESENT", 7_500)
        if pattern:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    doc_tag = _tag("TEXT_MATCH", 8_500)
            except re.error:
                # bad pattern → keep DOCUMENT_PRESENT, flag in metadata
                return StepResult(
                    success=False,
                    error=f"invalid pattern: {pattern!r}",
                )
        return StepResult(
            success=True,
            outputs={
                "doc_tags": [doc_tag],
                "doc_text": text,
                "doc_confidence_bps": doc_tag["confidence_bps"],
            },
        )


# =============================================================================
# MultimodalStep
# =============================================================================


class MultimodalStep(BaseStep):
    """Cross-modal consistency check.

    Reads the union of upstream tags, infers modality from tag-name
    prefixes, and emits MODALITY_CONSISTENCY / WITNESS_CORROBORATION when
    >= 2 modalities are present.
    """

    name = "multimodal"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            outputs=("multimodal_tags", "multimodal_confidence_bps"),
            description="Cross-modal consistency tags.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        all_tags = list(_gather_tags(merged))
        modalities: set[str] = set()
        for t in all_tags:
            name = str(t.get("tag_name", "")).upper()
            if name.startswith(("AUDIO", "SPEECH")):
                modalities.add("audio")
            elif name.startswith(("PERSON", "VISUAL", "VIDEO")):
                modalities.add("video")
            elif name.startswith(("GPS", "ENV")):
                modalities.add("sensor")

        out_tags = list(all_tags)
        if len(modalities) >= 2:
            out_tags.append(_tag("MODALITY_CONSISTENCY", min(len(modalities) * 3_000, 9_000)))
            out_tags.append(_tag("WITNESS_CORROBORATION", 7_500))

        confidence_bps = max((t.get("confidence_bps", 0) for t in out_tags), default=0)
        return StepResult(
            success=True,
            outputs={
                "multimodal_tags": out_tags,
                "multimodal_confidence_bps": confidence_bps,
            },
            metadata={"modalities": sorted(modalities)},
        )


# =============================================================================
# VisualClassifyStep
# =============================================================================


@dataclass
class _VisualEvidence:
    """Stub of `summary.verified[i]` from the original executor."""

    content_type: int = 0
    tags: list[dict[str, Any]] = field(default_factory=list)


class VisualClassifyStep(BaseStep):
    """Visual classification — aggregates tags from already-verified
    video evidence in the evidence summary.
    """

    name = "visual_classify"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_optional=("evidence_summary",),
            outputs=("visual_tags", "visual_confidence_bps"),
            description="Visual-evidence tag aggregation.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        summary = merged.get("evidence_summary")
        if summary is None:
            return StepResult(success=False, error="no evidence summary")
        verified = getattr(summary, "verified", None) or []
        # 1 == video content_type per the on-chain enum
        video_evidence = [ve for ve in verified if getattr(ve, "content_type", 0) == 1]
        if not video_evidence:
            return StepResult(success=False, error="no video evidence")

        all_tags: list[dict[str, Any]] = []
        for ve in video_evidence:
            for t in getattr(ve, "tags", []) or []:
                # accept either dataclass-style or dict-style
                if isinstance(t, dict):
                    all_tags.append(t)
                else:
                    all_tags.append(
                        {
                            "tag_name": getattr(t, "tag_name", ""),
                            "tag_id": getattr(t, "tag_id", ""),
                            "confidence_bps": getattr(t, "confidence_bps", 0),
                        }
                    )
        confidence_bps = (
            int(sum(t.get("confidence_bps", 0) for t in all_tags) / len(all_tags))
            if all_tags
            else 0
        )
        return StepResult(
            success=bool(all_tags),
            outputs={
                "visual_tags": all_tags,
                "visual_confidence_bps": confidence_bps,
            },
        )


# =============================================================================
# ClassifyStep
# =============================================================================


class ClassifyStep(BaseStep):
    """Feature-vector → tags classifier.

    Caller injects `model_fn(feature_vector, route, params) -> list[tag]`.
    No raw audio handling here — feature extraction happens upstream
    (TranscribeStep) so RESOLVE/FORENSICS modes never touch raw audio.
    """

    name = "classify"

    def __init__(
        self,
        model_fn: Callable[..., list[dict[str, Any]]] | None = None,
        *,
        model_route: str = "default",
        params: dict[str, Any] | None = None,
    ) -> None:
        self._model_fn = model_fn
        self._model_route = model_route
        self._params = dict(params or {})

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_optional=("feature_vector",),
            outputs=("classify_tags", "classify_feature_vector", "classify_confidence_bps"),
            description="Feature-vector → tag classifier.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        fv = merged.get("feature_vector")
        # accept upstream feature_vector under any of these output names
        for k in ("feature_vector", "classify_feature_vector", "transcribe_feature_vector"):
            if fv is None and k in merged:
                fv = merged[k]
        if fv is None:
            return StepResult(success=False, error="no feature vector")

        if self._model_fn is None:
            return StepResult(
                success=True,
                outputs={
                    "classify_tags": [],
                    "classify_feature_vector": fv,
                    "classify_confidence_bps": 0,
                },
                metadata={"stubbed": True},
            )

        try:
            tags = self._model_fn(
                fv, self._model_route, self._params
            )
        except Exception as e:  # noqa: BLE001
            return StepResult(success=False, error=f"{type(e).__name__}: {e}")

        confidence_bps = max((t.get("confidence_bps", 0) for t in tags), default=0)
        return StepResult(
            success=True,
            outputs={
                "classify_tags": list(tags),
                "classify_feature_vector": fv,
                "classify_confidence_bps": confidence_bps,
            },
        )


# =============================================================================
# TranscribeStep
# =============================================================================


# Canonical tag names — must match keccak hashes committed on-chain. Imported
# from the original spec; SHA-256 is fine here, the on-chain side computes
# its own.
_SPEECH_DETECTED = "SPEECH_DETECTED"
_MULTI_SPEAKER = "MULTI_SPEAKER"
_BACKGROUND_NOISE = "BACKGROUND_NOISE"
_AUDIO_PRESENT = "AUDIO_PRESENT"
_SILENCE_DETECTED = "SILENCE_DETECTED"


def _prosody_to_tags(prosody: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    speech_ratio = float(prosody.get("speech_ratio", 0.0))
    if speech_ratio > 0.05:
        out.append(_tag(_SPEECH_DETECTED, int(speech_ratio * 10_000)))
    else:
        out.append(_tag(_SILENCE_DETECTED, int((1 - speech_ratio) * 9_000)))

    speaker_count = int(prosody.get("speaker_count", 1))
    if speaker_count > 1:
        out.append(_tag(_MULTI_SPEAKER, min(int(speaker_count / 3 * 10_000), 9_000)))

    snr = float(prosody.get("snr_db", 30.0))
    if snr < 10.0:
        out.append(_tag(_BACKGROUND_NOISE, int((10.0 - snr) / 10.0 * 8_000)))

    energy = float(prosody.get("energy_mean", 0.5))
    if energy > 0.01:
        out.append(_tag(_AUDIO_PRESENT, min(int(energy * 10_000), 9_500)))

    return out


class TranscribeStep(BaseStep):
    """Whisper-like transcription + prosody → tags.

    Caller injects `transcribe_fn(audio_bytes) -> (transcript, prosody, feature_vector)`.
    Audio bytes themselves are read from ctx.inputs["audio"]. The audio
    never leaves the device — this step's manifest does NOT set
    `requires_external` since transcription runs locally.
    """

    name = "transcribe"

    def __init__(
        self,
        transcribe_fn: Callable[
            [bytes],
            tuple[str, Mapping[str, Any], list[float]],
        ] | None = None,
    ) -> None:
        self._transcribe_fn = transcribe_fn

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("audio",),
            outputs=(
                "transcribe_tags",
                "transcribe_text",
                "transcribe_feature_vector",
                "transcribe_confidence_bps",
            ),
            description="Local transcription + prosody-derived tags.",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        merged = _input_results_from_ctx(ctx)
        audio = merged.get("audio")
        if not audio:
            return StepResult(success=False, error="no audio data for transcription")

        if self._transcribe_fn is None:
            # Stub path: empty transcript + minimal AUDIO_PRESENT tag
            return StepResult(
                success=True,
                outputs={
                    "transcribe_tags": [_tag(_AUDIO_PRESENT, 5_000)],
                    "transcribe_text": "",
                    "transcribe_feature_vector": [],
                    "transcribe_confidence_bps": 5_000,
                },
                metadata={"stubbed": True},
            )

        try:
            transcript, prosody, fv = self._transcribe_fn(audio)
        except Exception as e:  # noqa: BLE001
            return StepResult(success=False, error=f"{type(e).__name__}: {e}")

        tags = _prosody_to_tags(prosody)
        confidence_bps = max((t.get("confidence_bps", 0) for t in tags), default=0)
        return StepResult(
            success=True,
            outputs={
                "transcribe_tags": tags,
                "transcribe_text": transcript or "",
                "transcribe_feature_vector": list(fv or []),
                "transcribe_confidence_bps": confidence_bps,
            },
        )


# =============================================================================
# Convenience factory
# =============================================================================


def make_default_inference_stack() -> list[BaseStep]:
    """Return a default ordered list of inference steps suitable for
    most resolution stacks. Caller can prepend/append other Steps."""
    return [
        TranscribeStep(),
        ClassifyStep(),
        VisualClassifyStep(),
        DocVerifyStep(),
        MultimodalStep(),
        WebSearchStep(),
    ]


__all__ = [
    "ClassifyStep",
    "DocVerifyStep",
    "MultimodalStep",
    "TranscribeStep",
    "VisualClassifyStep",
    "WebSearchStep",
    "make_default_inference_stack",
]
