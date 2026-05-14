"""
Diarization step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 2. Real impl runs pyannote-class
diarization. The stub assigns speaker labels from a fixture or
defaults all tokens to a single speaker.

Inputs:
  - "tokens": list[TokenFeatures] (from ASRStep)
  - "fixture_speaker_labels" (stub-only): dict mapping
    `onset_ms` → `(speaker_label, confidence)`. Tokens at unknown
    onsets get the default label.

Outputs (mutates the same `tokens` list in place — typical for the
voice stack since features accumulate per-token):
  - tokens[i].speaker_label populated
  - tokens[i].speaker_label_confidence populated
  - tokens[i].produced_by_model_pass["speaker_label"] = our pass id
"""

from __future__ import annotations

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import TokenFeatures


DEFAULT_SPEAKER_LABEL = "s0"
DEFAULT_SPEAKER_CONFIDENCE = 0.6


class DiarizationStep(BaseStep):
    name: str = "voice.diarization"
    model_pass_version: str = "stub-diarization@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=("tokens",),
            inputs_optional=("fixture_speaker_labels",),
            outputs=("tokens",),
            requires_attestation=True,
            description="Speaker diarization (stub)",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        # tokens come from ASR via stack outputs (also surfaced as
        # inputs for the next step via StackContext semantics).
        tokens: list[TokenFeatures] = (
            ctx.get_output("tokens") or ctx.get_input("tokens") or []
        )
        fixture: dict[int, tuple[str, float]] = ctx.get_input(
            "fixture_speaker_labels", {}
        )

        for tk in tokens:
            label, conf = fixture.get(
                tk.onset_ms,
                (DEFAULT_SPEAKER_LABEL, DEFAULT_SPEAKER_CONFIDENCE),
            )
            tk.speaker_label = label
            tk.speaker_label_confidence = conf
            tk.produced_by_model_pass["speaker_label"] = self.model_pass_version

        return StepResult(
            success=True,
            outputs={"tokens": tokens},
            metadata={
                "token_count": len(tokens),
                "distinct_speakers": len({tk.speaker_label for tk in tokens}),
            },
        )


__all__ = ["DiarizationStep"]
