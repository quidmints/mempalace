"""
Speaker matching step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 3. Real impl uses speaker-embedding
model + cosine similarity against reference voiceprints stored as
substrate entities. The stub maps speaker labels to canned entity
references via fixture.

Inputs:
  - "tokens": list[TokenFeatures]
  - "segments": list[DrawerSegment] (optional; if absent, the step
    operates on the whole drawer as one segment)
  - "fixture_speaker_to_entity" (stub-only): dict mapping
    `speaker_label` → list of `(entity_id, confidence)` tuples.

Outputs:
  - "voice_match_candidates": list of dicts with shape
    `{segment_id, target_entity_id, confidence}` — the caller writes
    these to substrate as `voice_matches_reference` edges.
"""

from __future__ import annotations

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import DrawerSegment, TokenFeatures


class SpeakerMatchStep(BaseStep):
    name: str = "voice.speaker_match"
    model_pass_version: str = "stub-speaker-match@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=("tokens",),
            inputs_optional=(
                "segments",
                "fixture_speaker_to_entity",
            ),
            outputs=("voice_match_candidates",),
            requires_attestation=True,
            description="Speaker matching against reference voiceprints (stub)",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        tokens: list[TokenFeatures] = (
            ctx.get_output("tokens") or ctx.get_input("tokens") or []
        )
        segments: list[DrawerSegment] = (
            ctx.get_output("segments") or ctx.get_input("segments") or []
        )
        fixture: dict[str, list[tuple[str, float]]] = ctx.get_input(
            "fixture_speaker_to_entity", {}
        )

        # Build segment_id → speaker_label index. If we have explicit
        # segments, use their dominant_speaker_label; otherwise group
        # by the diarization output.
        segment_speakers: dict[str, str] = {}
        if segments:
            for seg in segments:
                if seg.dominant_speaker_label:
                    segment_speakers[seg.segment_id] = seg.dominant_speaker_label
        else:
            # Whole drawer as one segment; pick the speaker most frequent
            speaker_counts: dict[str, int] = {}
            for tk in tokens:
                if tk.speaker_label:
                    speaker_counts[tk.speaker_label] = (
                        speaker_counts.get(tk.speaker_label, 0) + 1
                    )
            if speaker_counts:
                dominant = max(speaker_counts.items(), key=lambda kv: kv[1])[0]
                segment_speakers["__whole__"] = dominant

        candidates: list[dict] = []
        for seg_id, speaker_label in segment_speakers.items():
            for entity_id, confidence in fixture.get(speaker_label, []):
                candidates.append({
                    "segment_id": seg_id,
                    "target_entity_id": entity_id,
                    "confidence": confidence,
                    "speaker_label": speaker_label,
                })

        return StepResult(
            success=True,
            outputs={"voice_match_candidates": candidates},
            metadata={"candidate_count": len(candidates)},
        )


__all__ = ["SpeakerMatchStep"]
