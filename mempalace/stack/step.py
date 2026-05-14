"""
Step protocol.

A Step is a single stage in a stack. It declares its input/output contract
via StepManifest and returns a StepResult after execution.

Per R3 §1.1, the framework is small and shared; specialization is in the
Step subclasses (RankerStep, InferenceStep, MinerPass, MatchingLayer,
CompositionStep, WakeUpComponent).

This file defines:
  - Step (Protocol)
  - StepManifest (declared contract)
  - StepResult (return value)
  - BaseStep (convenience class with sensible defaults)

Spec ref: R3 §1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .context import StackContext


# =============================================================================
# Step manifest
# =============================================================================


@dataclass
class StepManifest:
    """Static declaration of a step's contract.

    The framework uses this to validate stacks before execution: every
    step's input keys must be either inputs to the stack or outputs of
    a prior step.
    """

    name: str
    version: str = "0.1.0"
    inputs_required: tuple[str, ...] = ()
    inputs_optional: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    requires_attestation: bool = False
    requires_external: bool = False  # if True, only EXTERNAL privacy_mode runs it
    requires_sandbox: bool = False   # if True, only SANDBOX privacy_mode runs it
    description: str = ""


# =============================================================================
# Step result
# =============================================================================


@dataclass
class StepResult:
    """Return value from Step.run().

    success     : whether the step completed without error.
    outputs     : key-value pairs to merge into ctx.outputs.
    error       : exception or error string if !success.
    elapsed_ms  : how long the step took.
    metadata    : free-form dict the framework records.
    """

    success: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Step protocol
# =============================================================================


@runtime_checkable
class Step(Protocol):
    """Step contract.

    Subclasses implement declares() and run().
    """

    name: str

    def declares(self) -> StepManifest: ...
    async def run(self, ctx: StackContext) -> StepResult: ...


# =============================================================================
# BaseStep — synchronous convenience
# =============================================================================


class BaseStep:
    """Convenience base class for steps with sync logic.

    Subclasses override `_run_sync(ctx) -> StepResult` and the framework
    handles the async wrapping. For genuinely async steps (network calls
    etc.), implement `run` directly per the protocol.
    """

    name: str = "base_step"

    def declares(self) -> StepManifest:
        return StepManifest(name=self.name)

    def _run_sync(self, ctx: StackContext) -> StepResult:
        raise NotImplementedError

    async def run(self, ctx: StackContext) -> StepResult:
        return self._run_sync(ctx)


# =============================================================================
# FunctionStep — wrap a plain function
# =============================================================================


class FunctionStep(BaseStep):
    """Wraps a plain function (sync or async) as a Step."""

    def __init__(
        self,
        name: str,
        fn: Callable[[StackContext], Any] | Callable[[StackContext], Awaitable[Any]],
        *,
        manifest: StepManifest | None = None,
        is_async: bool = False,
    ) -> None:
        self.name = name
        self._fn = fn
        self._is_async = is_async
        self._manifest = manifest or StepManifest(name=name)

    def declares(self) -> StepManifest:
        return self._manifest

    async def run(self, ctx: StackContext) -> StepResult:
        import time as _time
        t0 = _time.monotonic()
        try:
            if self._is_async:
                result = await self._fn(ctx)  # type: ignore[misc]
            else:
                result = self._fn(ctx)
            elapsed = int((_time.monotonic() - t0) * 1000)
            if isinstance(result, StepResult):
                result.elapsed_ms = elapsed
                return result
            outputs = result if isinstance(result, dict) else {"value": result}
            return StepResult(success=True, outputs=outputs, elapsed_ms=elapsed)
        except Exception as e:
            elapsed = int((_time.monotonic() - t0) * 1000)
            return StepResult(
                success=False, error=f"{type(e).__name__}: {e}", elapsed_ms=elapsed
            )


__all__ = [
    "BaseStep",
    "FunctionStep",
    "Step",
    "StepManifest",
    "StepResult",
]
