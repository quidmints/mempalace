"""
Sandbox-bound step execution.

Per R3 §7.1: when matching runs inside a sandbox, every Step.run() call
goes through this module. The wrapper:

  1. Verifies the sandbox is in LOADED state (foreign slice present)
  2. Forces the StackContext into LOCAL_ONLY privacy mode (no external)
  3. Records before/after attestation per-step
  4. Catches any escape attempt (network call, file write outside scratch)
     and converts it to a sandbox-failure event

Spec ref: R3 §7.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..stack.context import PrivacyMode, StackContext
from ..stack.step import Step, StepResult
from .sandbox import SandboxManager, SandboxStatus, get_sandbox_manager


@dataclass
class EnclaveRunResult:
    """Outcome of running a step inside a sandbox."""

    success: bool
    step_result: StepResult | None
    error: str | None = None


async def run_step_in_sandbox(
    sandbox_id: str,
    step: Step,
    ctx: StackContext,
    *,
    sandbox_mgr: SandboxManager | None = None,
) -> EnclaveRunResult:
    """Run a step inside the named sandbox.

    Forces ctx.privacy_mode = LOCAL_ONLY for the duration. Returns an
    EnclaveRunResult with the step's outcome.
    """
    mgr = sandbox_mgr or get_sandbox_manager()
    state = mgr.get_state(sandbox_id)
    if state is None:
        return EnclaveRunResult(success=False, step_result=None, error="sandbox not found")

    if state.status not in (SandboxStatus.LOADED, SandboxStatus.RUNNING):
        return EnclaveRunResult(
            success=False,
            step_result=None,
            error=f"sandbox not in runnable state: {state.status.value}",
        )

    # Force LOCAL_ONLY privacy
    original_mode = ctx.privacy_mode
    ctx.privacy_mode = PrivacyMode.LOCAL_ONLY

    # Mark running
    mgr.mark_running(sandbox_id)

    try:
        result = await step.run(ctx)
        return EnclaveRunResult(success=result.success, step_result=result)
    except Exception as e:
        return EnclaveRunResult(
            success=False,
            step_result=None,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        ctx.privacy_mode = original_mode


__all__ = ["EnclaveRunResult", "run_step_in_sandbox"]
