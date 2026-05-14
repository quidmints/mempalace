"""
Paralinguistic event step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 6. Detects laughter, sighs, breath,
code-switching as first-class events. The stub returns canned events
from a fixture.

Inputs:
  - "tokens": list[TokenFeatures]
  - "segments": list[DrawerSegment] (optional)
  - "fixture_paralinguistic_events" (stub-only): list of dicts
    `{segment_id, event_kind, onset_ms, offset_ms, confidence}`

Outputs:
  - "paralinguistic_events": list of dicts the caller writes as
    `paralinguistic_event_at` edges to first-class event nodes.

# Why first-class

A drawer with code-switching is itself a useful query target: "show
me drawers where I code-switched between languages" is a natural
question. Modeling these as first-class events with edges into the
DAG (rather than as fields on TokenFeatures) lets retrieval traverse
through them.
"""

from __future__ import annotations

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import DrawerSegment, TokenFeatures


VALID_EVENT_KINDS = frozenset({
    "laughter",
    "sigh",
    "breath",
    "code_switch",
    "pause",
    "filler_um",
    "filler_uh",
})


class ParalinguisticStep(BaseStep):
    name: str = "voice.paralinguistic"
    model_pass_version: str = "stub-paralinguistic@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=("tokens",),
            inputs_optional=(
                "segments",
                "fixture_paralinguistic_events",
            ),
            outputs=("paralinguistic_events",),
            requires_attestation=True,
            description="Paralinguistic event detection (stub)",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        # tokens not strictly used by the stub but real impl would
        # condition events on them
        _tokens: list[TokenFeatures] = (
            ctx.get_output("tokens") or ctx.get_input("tokens") or []
        )
        _segments: list[DrawerSegment] = (
            ctx.get_output("segments") or ctx.get_input("segments") or []
        )
        fixture_events: list[dict] = ctx.get_input(
            "fixture_paralinguistic_events", []
        )

        # Filter to recognized event kinds; tag each with our pass id
        events: list[dict] = []
        for evt in fixture_events:
            if evt.get("event_kind") not in VALID_EVENT_KINDS:
                continue
            stamped = dict(evt)
            stamped["produced_by_model_pass"] = self.model_pass_version
            events.append(stamped)

        return StepResult(
            success=True,
            outputs={"paralinguistic_events": events},
            metadata={"event_count": len(events)},
        )


__all__ = ["VALID_EVENT_KINDS", "ParalinguisticStep"]
