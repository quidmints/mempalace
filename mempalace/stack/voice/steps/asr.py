"""
ASR step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 1. Real implementation runs Whisper
or equivalent locally. The stub returns canned tokens from a
fixture-supplied transcription so the rest of the stack can run
end-to-end without a real model.

Inputs (read from StackContext):
  - "audio_bytes" or "audio_blob_ref": the audio to transcribe
  - "fixture_transcription" (stub-only): pre-computed
    [(token, onset_ms, offset_ms), ...] tuples

Outputs (written to StackContext):
  - "tokens": list[TokenFeatures] — one per recognized token, with
    only `token`, `onset_ms`, `offset_ms`, and `produced_by_model_pass`
    populated. Later steps fill in the rest.
"""

from __future__ import annotations

from typing import Any

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import TokenFeatures


class ASRStep(BaseStep):
    """ASR step — stub implementation.

    Production version binds to a local Whisper-class model. The
    contract is what matters here: same inputs, same outputs.
    """

    name: str = "voice.asr"
    model_pass_version: str = "stub-asr@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=(),
            inputs_optional=(
                "audio_bytes",
                "audio_blob_ref",
                "fixture_transcription",
            ),
            outputs=("tokens",),
            requires_attestation=True,
            description="Local ASR (stub) → token stream with timing",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        # Stub path: read fixture_transcription if provided. Real
        # impl would read audio bytes and run inference.
        fixture: list[tuple[str, int, int]] = ctx.get_input(
            "fixture_transcription", []
        )
        if not fixture and not ctx.get_input("audio_bytes"):
            # No audio and no fixture → empty result.
            return StepResult(success=True, outputs={"tokens": []})

        tokens: list[TokenFeatures] = [
            TokenFeatures(
                token=token,
                onset_ms=onset,
                offset_ms=offset,
                produced_by_model_pass={"tokens": self.model_pass_version},
            )
            for (token, onset, offset) in fixture
        ]

        return StepResult(
            success=True,
            outputs={"tokens": tokens},
            metadata={"token_count": len(tokens)},
        )


__all__ = ["ASRStep"]
