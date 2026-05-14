"""
Stack composition and execution.

A Stack is an ordered sequence of Steps. validate() checks the dependency
graph; execute() runs steps in order, threading the context.

Per R3 §1.2, when stacks combine outputs from multiple steps (especially
rankers), combination is performed by a *trusted aggregator* in the
daemon's core, not by any individual step. This Stack class is the trusted
core; aggregation happens here, not in user-supplied step code.

Spec ref: R3 §1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .context import PrivacyMode, StackContext
from .step import Step, StepManifest, StepResult


# =============================================================================
# Validation errors
# =============================================================================


@dataclass
class ValidationError:
    step_name: str
    message: str


# =============================================================================
# Stack result
# =============================================================================


@dataclass
class StackResult:
    """Outcome of Stack.execute()."""

    success: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    step_results: list[StepResult] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0


# =============================================================================
# Stack
# =============================================================================


class Stack:
    """Ordered composition of steps."""

    def __init__(
        self,
        plan: list[Step],
        *,
        name: str = "stack",
        stop_on_failure: bool = True,
    ) -> None:
        self.plan = list(plan)
        self.name = name
        self.stop_on_failure = stop_on_failure

    # ---- validation ---------------------------------------------------------

    def validate(self, *, initial_keys: list[str] | None = None) -> list[ValidationError]:
        """Check that each step's required inputs are provided either by
        a prior step's output or by the initial context.
        """
        errors: list[ValidationError] = []
        provided: set[str] = set(initial_keys or [])
        seen_step_names: set[str] = set()

        for step in self.plan:
            mf = step.declares()
            if mf.name in seen_step_names:
                errors.append(ValidationError(
                    step_name=mf.name,
                    message=f"duplicate step name in plan",
                ))
            seen_step_names.add(mf.name)

            for required in mf.inputs_required:
                if required not in provided:
                    errors.append(ValidationError(
                        step_name=mf.name,
                        message=f"required input '{required}' not provided by prior step or initial context",
                    ))

            for output in mf.outputs:
                provided.add(output)

        return errors

    # ---- privacy gating -----------------------------------------------------

    def _check_privacy(self, ctx: StackContext) -> ValidationError | None:
        for step in self.plan:
            mf = step.declares()
            if mf.requires_external and ctx.privacy_mode != PrivacyMode.EXTERNAL:
                return ValidationError(
                    step_name=mf.name,
                    message=f"requires EXTERNAL privacy mode, got {ctx.privacy_mode.value}",
                )
            if mf.requires_sandbox and ctx.privacy_mode != PrivacyMode.SANDBOX:
                return ValidationError(
                    step_name=mf.name,
                    message=f"requires SANDBOX privacy mode, got {ctx.privacy_mode.value}",
                )
            # LOCAL_ONLY rejects external steps
            if mf.requires_external and ctx.privacy_mode == PrivacyMode.LOCAL_ONLY:
                return ValidationError(
                    step_name=mf.name,
                    message="LOCAL_ONLY mode rejects step requiring external resources",
                )
        return None

    # ---- execution ----------------------------------------------------------

    async def execute(self, ctx: StackContext) -> StackResult:
        """Run steps in sequence."""
        t0 = time.monotonic()

        # Privacy gate
        privacy_error = self._check_privacy(ctx)
        if privacy_error is not None:
            return StackResult(
                success=False,
                error=f"privacy check failed at {privacy_error.step_name}: {privacy_error.message}",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )

        step_results: list[StepResult] = []
        for step in self.plan:
            mf = step.declares()

            # Check required inputs are present
            missing = [k for k in mf.inputs_required if (
                k not in ctx.inputs and k not in ctx.outputs
            )]
            if missing:
                step_result = StepResult(
                    success=False,
                    error=f"missing inputs: {missing}",
                )
                step_results.append(step_result)
                if self.stop_on_failure:
                    return StackResult(
                        success=False,
                        outputs=dict(ctx.outputs),
                        step_results=step_results,
                        error=f"step {mf.name}: missing inputs {missing}",
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                    )
                continue

            # Run
            step_result = await step.run(ctx)
            step_results.append(step_result)

            # Merge outputs (the trusted aggregator point — Stack is trusted core)
            if step_result.success:
                ctx.merge_outputs(step_result.outputs)

            if not step_result.success and self.stop_on_failure:
                return StackResult(
                    success=False,
                    outputs=dict(ctx.outputs),
                    step_results=step_results,
                    error=f"step {mf.name} failed: {step_result.error}",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )

        return StackResult(
            success=all(s.success for s in step_results),
            outputs=dict(ctx.outputs),
            step_results=step_results,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


__all__ = ["Stack", "StackResult", "ValidationError"]
