"""
Prosody / affect step — stub implementation.

Per VOICE_STACK_DESIGN.md Step 4. Real impl runs a per-token
emotion/affect classifier. The stub fills in canned prosody +
affect from fixtures.

Inputs:
  - "tokens": list[TokenFeatures]
  - "fixture_prosody" (stub-only): dict mapping
    `onset_ms` → ProsodyVector
  - "fixture_affect" (stub-only): dict mapping
    `onset_ms` → AffectDistribution

Outputs (mutates tokens in place):
  - tokens[i].prosody populated
  - tokens[i].affect populated
  - tokens[i].produced_by_model_pass["prosody"] / ["affect"] = our pass id

# Memo override discipline

The voice stack defers to interpretation memos when present. Per
VOICE_STACK_DESIGN.md §"Privacy and attestation" + HANDLES_DESIGN.md
v2 §"Memos as override signals — segment-targeted", a memo on a
segment overrides prosody/affect inference for tokens in that span.

This stub honors the override mechanism: ctx may carry
"memo_overrides" — a list of (start_ms, end_ms, override_dict).
Tokens within an override range get the override values stamped
with `produced_by_model_pass["affect"] = "memo_override"` to make
the override visible in the dependency tracking.
"""

from __future__ import annotations

from typing import Any

from ...context import StackContext
from ...step import BaseStep, StepManifest, StepResult
from ..types import AffectDistribution, ProsodyVector, TokenFeatures


class ProsodyAffectStep(BaseStep):
    name: str = "voice.prosody_affect"
    model_pass_version: str = "stub-prosody-affect@v1"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version=self.model_pass_version,
            inputs_required=("tokens",),
            inputs_optional=(
                "fixture_prosody",
                "fixture_affect",
                "memo_overrides",
            ),
            outputs=("tokens",),
            requires_attestation=True,
            description="Per-token prosody + affect (stub)",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        tokens: list[TokenFeatures] = (
            ctx.get_output("tokens") or ctx.get_input("tokens") or []
        )
        fixture_prosody: dict[int, ProsodyVector] = ctx.get_input(
            "fixture_prosody", {}
        )
        fixture_affect: dict[int, AffectDistribution] = ctx.get_input(
            "fixture_affect", {}
        )
        memo_overrides: list[tuple[int, int, dict[str, Any]]] = ctx.get_input(
            "memo_overrides", []
        )

        for tk in tokens:
            # Default-fill from fixtures
            if tk.onset_ms in fixture_prosody:
                tk.prosody = fixture_prosody[tk.onset_ms]
                tk.produced_by_model_pass["prosody"] = self.model_pass_version
            if tk.onset_ms in fixture_affect:
                tk.affect = fixture_affect[tk.onset_ms]
                tk.produced_by_model_pass["affect"] = self.model_pass_version

            # Memo overrides — last-wins; record the override in
            # produced_by_model_pass so dependency tracking sees it
            for start_ms, end_ms, override in memo_overrides:
                if start_ms <= tk.onset_ms < end_ms:
                    if "prosody" in override:
                        tk.prosody = override["prosody"]
                        tk.produced_by_model_pass["prosody"] = "memo_override"
                    if "affect" in override:
                        tk.affect = override["affect"]
                        tk.produced_by_model_pass["affect"] = "memo_override"

        return StepResult(
            success=True,
            outputs={"tokens": tokens},
            metadata={
                "token_count": len(tokens),
                "memo_overrides_applied": len(memo_overrides),
            },
        )


__all__ = ["ProsodyAffectStep"]
