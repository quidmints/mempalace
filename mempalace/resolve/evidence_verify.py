"""
Evidence verification — hardware attestation chain.

Per R3 §2.2: hardware-backed signatures on evidence. StrongBox + Secure
Enclave on mobile; TPM on desktop. Becomes a Step in the resolution
stack: invalid attestation → stack short-circuits to INDETERMINATE.

Reuses `mempalace.federate.attest` for chain verification primitives;
this module wraps that with the Step protocol used by the resolution
stack.

Spec ref: R3 §2.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..federate.attest import (
    AttestationChain,
    verify_attestation_chain,
)
from ..log.client import LogClient
from ..stack.step import BaseStep, StepManifest, StepResult
from ..stack.context import StackContext


# =============================================================================
# Verification result
# =============================================================================


@dataclass
class AttestedEvidence:
    """An evidence record with its attestation chain."""

    evidence_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    attestation_chain: AttestationChain | None = None
    verified: bool = False
    failure_reason: str = ""


# =============================================================================
# Verifier
# =============================================================================


class EvidenceVerifier:
    """Verifies attestation chains on evidence records.

    Delegates the actual chain verification to
    `mempalace.federate.attest.verify_attestation_chain`, which itself
    emits `AttestationChainBroken` on failure.
    """

    def __init__(self, *, log: LogClient | None = None) -> None:
        self._log = log

    def verify(self, evidence: AttestedEvidence) -> AttestedEvidence:
        if evidence.attestation_chain is None:
            evidence.verified = False
            evidence.failure_reason = "no attestation chain"
            return evidence

        result = verify_attestation_chain(
            evidence.attestation_chain,
            log_client=self._log,
        )
        evidence.verified = result.success
        if not result.success:
            evidence.failure_reason = result.reason or "verification failed"
        return evidence


# =============================================================================
# Step wrapper for the resolution stack
# =============================================================================


class EvidenceVerifyStep(BaseStep):
    """Resolution-stack step that verifies all evidence in ctx.inputs.

    Inputs:
      - ctx.inputs["evidence_records"]: list[AttestedEvidence]
    Outputs:
      - "verified_evidence": list[AttestedEvidence] (verified=True only)
      - "rejected_evidence": list[AttestedEvidence]
    """

    name = "resolve.evidence_verify"

    def __init__(self, *, log: LogClient | None = None) -> None:
        self._verifier = EvidenceVerifier(log=log)

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("evidence_records",),
            outputs=("verified_evidence", "rejected_evidence"),
        )

    async def run(self, ctx: StackContext) -> StepResult:
        records: list[AttestedEvidence] = ctx.inputs.get("evidence_records", [])
        verified: list[AttestedEvidence] = []
        rejected: list[AttestedEvidence] = []
        for record in records:
            self._verifier.verify(record)
            (verified if record.verified else rejected).append(record)
        return StepResult(
            success=True,
            outputs={
                "verified_evidence": verified,
                "rejected_evidence": rejected,
            },
        )


__all__ = [
    "AttestedEvidence",
    "EvidenceVerifier",
    "EvidenceVerifyStep",
]
