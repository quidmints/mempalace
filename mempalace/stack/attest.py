"""
Attested step decorator.

Per R3 §1.4 + §3.3, any step running ML inference can be wrapped with
attestation discipline. Before execution: emit `model_loaded` event.
After execution: emit `model_inference_completed` with input/output hashes
+ a hardware-bound signature.

For LOCAL_ONLY privacy mode, all steps must be AttestedStep.

The signature is hardware-bound in production (StrongBox / Secure Enclave);
in dev/test the signature is empty and only the hashing chain is verified.

Spec ref: R3 §1.4, §3.3.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from ..log.client import LogClient, get_default_client
from ..schema.events import ModelInferenceCompleted, ModelLoaded
from .context import AttestationRecord, StackContext
from .step import Step, StepManifest, StepResult


# =============================================================================
# Hashing helpers
# =============================================================================


def _hash_value(value) -> str:
    """Stable SHA-256 of a value's repr."""
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _hash_inputs(ctx: StackContext) -> str:
    """Hash the relevant context inputs for attestation."""
    relevant = {**ctx.inputs, **ctx.outputs}
    items = sorted(relevant.items())
    return hashlib.sha256(
        b"\x00".join(f"{k}={v!r}".encode("utf-8") for k, v in items)
    ).hexdigest()


def _hash_outputs(outputs: dict) -> str:
    items = sorted(outputs.items())
    return hashlib.sha256(
        b"\x00".join(f"{k}={v!r}".encode("utf-8") for k, v in items)
    ).hexdigest()


# =============================================================================
# AttestedStep wrapper
# =============================================================================


class AttestedStep:
    """Wraps another Step with attestation."""

    def __init__(
        self,
        inner: Step,
        *,
        model_id: str,
        weights_hash: str = "",
        signing_pubkey: str = "",
        log_client: LogClient | None = None,
    ) -> None:
        self._inner = inner
        self._model_id = model_id
        self._weights_hash = weights_hash
        self._signing_pubkey = signing_pubkey
        self._client = log_client or get_default_client()
        self.name = f"attested:{inner.name}"

    def declares(self) -> StepManifest:
        inner_mf = self._inner.declares()
        return StepManifest(
            name=self.name,
            version=inner_mf.version,
            inputs_required=inner_mf.inputs_required,
            inputs_optional=inner_mf.inputs_optional,
            outputs=inner_mf.outputs,
            requires_attestation=True,
            requires_external=inner_mf.requires_external,
            requires_sandbox=inner_mf.requires_sandbox,
            description=f"[ATTESTED] {inner_mf.description}",
        )

    async def run(self, ctx: StackContext) -> StepResult:
        # Pre-execution: model_loaded event
        loaded_event = ModelLoaded(
            model_id=self._model_id,
            weights_hash=self._weights_hash,
            signing_pubkey=self._signing_pubkey,
            enrollment_signature="",
        )
        self._client.append(loaded_event)

        # Compute input hash before run
        input_hash = _hash_inputs(ctx)

        # Run inner
        result = await self._inner.run(ctx)

        # Post-execution: emit attestation
        output_hash = _hash_outputs(result.outputs) if result.success else "ERROR"
        ts = int(time.time() * 1000)
        attestation_event = ModelInferenceCompleted(
            model_id=self._model_id,
            weights_hash=self._weights_hash,
            step_id=self.name,
            input_hash=input_hash,
            output_hash=output_hash,
            attestation_signature="",  # populated by hardware key in production
        )
        self._client.append(attestation_event)

        # Record in the context's attestation handle
        ctx.attestation.record(AttestationRecord(
            step_id=self.name,
            step_name=self._inner.name,
            model_id=self._model_id,
            weights_hash=self._weights_hash,
            input_hash=input_hash,
            output_hash=output_hash,
            timestamp_ms=ts,
        ))

        return result


__all__ = ["AttestedStep"]
