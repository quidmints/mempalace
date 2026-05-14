"""
Accent step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 5. Per-segment soft distribution over
accent / origin categories. NOT a hard label — the design commits to
a distribution because accent is rarely categorical.

Inputs:
  - "segments": list[DrawerSegment]
  - "fixture_segment_accent" (stub-only): dict mapping
    `segment_id` → AccentDistribution

Outputs (mutates segments in place):
  - segments[i].accent_distribution populated
"""

from __future__ import annotations

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import AccentDistribution, DrawerSegment


class AccentStep(BaseStep):
    name: str = "voice.accent"
    model_pass_version: str = "stub-accent@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=("segments",),
            inputs_optional=("fixture_segment_accent",),
            outputs=("segments",),
            requires_attestation=True,
            description="Per-segment accent distribution (stub)",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        segments: list[DrawerSegment] = (
            ctx.get_output("segments") or ctx.get_input("segments") or []
        )
        fixture: dict[str, AccentDistribution] = ctx.get_input(
            "fixture_segment_accent", {}
        )

        for seg in segments:
            if seg.segment_id in fixture:
                seg.accent_distribution = fixture[seg.segment_id]

        return StepResult(
            success=True,
            outputs={"segments": segments},
            metadata={"segments_with_accent": sum(
                1 for s in segments if s.accent_distribution is not None
            )},
        )


__all__ = ["AccentStep"]
