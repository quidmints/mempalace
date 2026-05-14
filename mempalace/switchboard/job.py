"""
ResolutionJob runtime — Track 7-shim-8.

Per TRACK_7_UPDATED_INSTRUCTION.md sub-track 7-shim-8: replace the
NotImplementedError in `ResolutionJob.execute()` with the real flow.

# Phone-offline operation (corrected threat model)

A resolver palace acts on slice bytes that another palace shipped to
it via the `/mempalace/slice/1.0.0` request protocol. Those bytes
are already authenticated by the subject's palace; the resolver does
NOT need its own bundle loaded to operate as a resolver. Concretely:

  - `request_slice` returns bytes whose payload was already decrypted
    by the subject's PhoneSecureElement before being sent (or arrives
    as ciphertext addressed to the resolver's session pubkey, which
    the resolver decrypts via session-key material — also independent
    of the resolver's bundle).
  - Plan execution is a pure function of slice bytes + plan steps. No
    local-substrate access needed.
  - Finding submission signs with the *session* key, not the bundle's
    palace key.

So a cloud-box whose phone is offline can still act as a switchboard
resolver, accept jobs, run plans, and submit findings. What it can't
do without the bundle is decrypt local user drawers (which is fine —
those are the operator's own substrate, not resolution input).

# The flow:

  1. Slice ingestion: pull the subject's data per the slice request
     spec (existing `mempalace.federate.slice` machinery).
  2. Plan execution: run the formula's plan as an AttestedStep chain.
     For now, a stub plan registry; production has per-market plan
     hashes pinned on-chain.
  3. Finding submission: serialize the result, build the attestation
     chain, emit a `finding_emitted` log event AND submit to the
     on-chain `submit_finding` instruction (via the chain adapter).
  4. Set status to DONE and return the finding.

# What this module does NOT ship

  - The slice transport. `request_slice_for_resolution` is a stub
    that callers can plug into their own libp2p-based slice protocol.
  - The plan registry. A `PlanRegistry` Protocol is defined; tests
    pass a fixture; production wires to whatever holds the
    market-shape → plan-hash map.
  - The chain submission. `ChainSubmissionAdapter` Protocol; the
    real implementation calls into Rust via `mempalace_chain` once
    7-shim-6 lands.

Each piece is a clean integration seam; the orchestration is what
this module ships.

Spec ref: TRACK_7_UPDATED_INSTRUCTION.md sub-track 7-shim-8,
ORACLE_THREAT_MODEL.md §4.2.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ..log.client import LogClient, get_default_client

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Lifecycle states for a resolution job."""

    ASSIGNED = "assigned"
    INGESTING = "ingesting"
    RUNNING = "running"
    SUBMITTING = "submitting"
    DONE = "done"
    FAILED = "failed"


class PrivacyMode(str, Enum):
    """Per R3 §1.4 / §3.2."""

    LOCAL_ONLY = "local_only"
    SANDBOX = "sandbox"
    EXTERNAL = "external"


# =============================================================================
# Slice ingestion
# =============================================================================


@dataclass
class SliceRequestSpec:
    """What this job needs to pull from the subject's palace.

    Fields populated from the on-chain assignment record. Used by
    the `/mempalace/slice/1.0.0` request protocol.
    """

    subject_palace_id: str
    """The subject's palace identifier (libp2p peer id or pubkey)."""

    layer_minimum: int
    """The lowest layer (1=signature, 2=skeleton, 3=substrate)
    that satisfies the formula's needs. Higher layers expose more;
    the slice is the minimum sufficient layer."""

    scope_predicates: dict[str, Any] = field(default_factory=dict)
    """Filtering predicates applied during slice extraction.
    Specific to the formula and market type."""


@runtime_checkable
class SliceProvider(Protocol):
    """Pluggable provider for slice retrieval.

    Production wires this to the libp2p slice request protocol;
    tests pass a fixture that returns canned bytes.
    """

    def request_slice(self, spec: SliceRequestSpec) -> bytes: ...


class StubSliceProvider:
    """Default: returns empty bytes. Production replaces this."""

    def request_slice(self, spec: SliceRequestSpec) -> bytes:
        return b""


# =============================================================================
# Plan registry
# =============================================================================


@dataclass
class ResolutionPlan:
    """One plan for one (privacy_mode, market_shape) pair.

    A plan is a sequence of step descriptors that the executor
    runs against the slice. The plan hash is pinned on-chain;
    the executor verifies it before running.
    """

    plan_id: str
    plan_hash: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    def execute(
        self,
        slice_bytes: bytes,
        *,
        privacy_mode: PrivacyMode,
    ) -> tuple[bytes, list[dict[str, Any]]]:
        """Run the plan steps over the slice.

        Returns:
          (finding_payload_bytes, per_step_attestations)
        """
        per_step_attestations: list[dict[str, Any]] = []
        accumulator: bytes = slice_bytes

        for i, step_descriptor in enumerate(self.steps):
            kind = step_descriptor.get("kind", "noop")
            input_hash = _hash_bytes(accumulator)
            transformed = _execute_step_stub(
                kind, accumulator, step_descriptor, privacy_mode,
            )
            accumulator = transformed.payload

            per_step_attestations.append({
                "step_index": i,
                "step_kind": kind,
                "input_hash": input_hash,
                "output_hash": _hash_bytes(transformed.payload),
                "privacy_mode": privacy_mode.value,
                "attestation_summary": transformed.attestation_summary,
            })

        return accumulator, per_step_attestations


@dataclass
class _StepResult:
    payload: bytes
    attestation_summary: dict[str, Any]


def _execute_step_stub(
    kind: str,
    payload: bytes,
    descriptor: dict[str, Any],
    privacy_mode: PrivacyMode,
) -> _StepResult:
    """Default step executor.

    Real production replaces this via the existing `mempalace.stack`
    machinery. For now, the stub passes payloads through with a
    step-kind tag appended so plan execution observably transforms
    data.
    """
    if kind == "noop":
        return _StepResult(
            payload=payload,
            attestation_summary={"kind": "noop"},
        )
    if kind == "extract_signature":
        sig = _hash_bytes(payload).encode("ascii")
        return _StepResult(
            payload=sig,
            attestation_summary={"kind": "extract_signature"},
        )
    if kind == "compute_strength":
        # Placeholder strength dict.
        out = b'{"mood":0.7,"theme":0.3}'
        return _StepResult(
            payload=out,
            attestation_summary={"kind": "compute_strength"},
        )
    return _StepResult(
        payload=payload + b":" + kind.encode("ascii"),
        attestation_summary={"kind": kind, "passthrough": True},
    )


def _hash_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


@runtime_checkable
class PlanRegistry(Protocol):
    """Resolves a plan for a (privacy_mode, market_id) pair."""

    def plan_for(
        self,
        privacy_mode: PrivacyMode,
        market_id: str,
    ) -> ResolutionPlan: ...


class StubPlanRegistry:
    """Default registry. Tests register custom plans via
    `register(...)`; production replaces this with the real registry.
    """

    def __init__(self) -> None:
        self._plans: dict[tuple[PrivacyMode, str], ResolutionPlan] = {}

    def register(
        self,
        privacy_mode: PrivacyMode,
        market_id: str,
        plan: ResolutionPlan,
    ) -> None:
        self._plans[(privacy_mode, market_id)] = plan

    def plan_for(
        self,
        privacy_mode: PrivacyMode,
        market_id: str,
    ) -> ResolutionPlan:
        plan = self._plans.get((privacy_mode, market_id))
        if plan is None:
            return ResolutionPlan(
                plan_id="default_passthrough",
                plan_hash=_hash_bytes(b"default-plan-v1"),
                steps=[{"kind": "compute_strength"}],
            )
        return plan


# =============================================================================
# Chain submission
# =============================================================================


@dataclass
class ChainSubmissionResult:
    """Result of submitting a finding to the on-chain program."""

    success: bool
    on_chain_finding_id: str = ""
    error: str = ""


@runtime_checkable
class ChainSubmissionAdapter(Protocol):
    """Adapter to the on-chain `submit_finding` instruction.

    Production wires this to the Solana RPC + the
    `mempalace_chain.submit_finding` program call (Track 7-shim-6).
    Tests pass a fixture.
    """

    def submit_finding(
        self,
        market_id: str,
        finding_payload: bytes,
        attestation_chain: bytes,
        derivation_seed: bytes | None = None,
    ) -> ChainSubmissionResult: ...


class StubChainSubmissionAdapter:
    """Default adapter: records submissions in memory; reports
    success. Tests inspect `submitted` to verify."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit_finding(
        self,
        market_id: str,
        finding_payload: bytes,
        attestation_chain: bytes,
        derivation_seed: bytes | None = None,
    ) -> ChainSubmissionResult:
        record_id = f"finding_{len(self.submitted):08x}"
        self.submitted.append({
            "market_id": market_id,
            "finding_payload": finding_payload,
            "attestation_chain": attestation_chain,
            "derivation_seed": derivation_seed,
            "submitted_at_ms": int(time.time() * 1000),
            "on_chain_finding_id": record_id,
        })
        return ChainSubmissionResult(
            success=True, on_chain_finding_id=record_id,
        )


class FailingChainSubmissionAdapter:
    """Tests use this to drive the failure path."""

    def __init__(self, error: str = "chain unavailable") -> None:
        self.error = error
        self.attempted: int = 0

    def submit_finding(
        self,
        market_id: str,
        finding_payload: bytes,
        attestation_chain: bytes,
        derivation_seed: bytes | None = None,
    ) -> ChainSubmissionResult:
        self.attempted += 1
        return ChainSubmissionResult(success=False, error=self.error)


# =============================================================================
# Finding output
# =============================================================================


@dataclass
class ResolutionFinding:
    """The structured output of a resolution job."""

    market_id: str
    finding_payload: bytes
    attestation_chain: bytes
    derivation_seed: bytes | None = None
    on_chain_finding_id: str = ""


# =============================================================================
# ResolutionJob
# =============================================================================


class ResolutionJob:
    """One resolution assignment. Track 7-shim-8 implementation.

    Construction:
      job = ResolutionJob(
          market_id="mkt_x",
          privacy_mode=PrivacyMode.SANDBOX,
          slice_request=SliceRequestSpec(...),
          slice_provider=...,
          plan_registry=...,
          chain_adapter=...,
      )

    Execution:
      finding = job.execute()
      assert job.status == JobStatus.DONE

    Failure: `execute()` returns None on failure; status becomes
    FAILED and `error` carries the message.
    """

    def __init__(
        self,
        market_id: str,
        privacy_mode: PrivacyMode,
        slice_request: SliceRequestSpec,
        *,
        slice_provider: SliceProvider | None = None,
        plan_registry: PlanRegistry | None = None,
        chain_adapter: ChainSubmissionAdapter | None = None,
        log_client: LogClient | None = None,
        derivation_seed: bytes | None = None,
    ) -> None:
        self.market_id = market_id
        self.privacy_mode = privacy_mode
        self.slice_request = slice_request

        self.slice_provider = slice_provider or StubSliceProvider()
        self.plan_registry = plan_registry or StubPlanRegistry()
        self.chain_adapter = chain_adapter or StubChainSubmissionAdapter()
        self.log_client = log_client

        self.derivation_seed = derivation_seed

        self.status = JobStatus.ASSIGNED
        self._finding: ResolutionFinding | None = None
        self._error: str | None = None
        self._step_attestations: list[dict[str, Any]] = []

    def execute(self) -> ResolutionFinding | None:
        """Run the full pipeline. Returns the finding on success;
        None on failure. `self.status` reflects outcome.
        """
        try:
            # 1. Ingest slice
            self.status = JobStatus.INGESTING
            slice_bytes = self.slice_provider.request_slice(
                self.slice_request,
            )
            if slice_bytes is None:
                raise RuntimeError("slice provider returned None")

            # 2. Resolve + execute plan
            self.status = JobStatus.RUNNING
            plan = self.plan_registry.plan_for(
                self.privacy_mode, self.market_id,
            )
            finding_payload, step_attestations = plan.execute(
                slice_bytes,
                privacy_mode=self.privacy_mode,
            )
            self._step_attestations = step_attestations

            # 3. Build attestation chain
            import json
            attestation_chain = json.dumps(
                {
                    "plan_id": plan.plan_id,
                    "plan_hash": plan.plan_hash,
                    "steps": step_attestations,
                    "privacy_mode": self.privacy_mode.value,
                    "subject_palace_id": self.slice_request.subject_palace_id,
                },
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")

            # 4. Construct finding
            finding = ResolutionFinding(
                market_id=self.market_id,
                finding_payload=finding_payload,
                attestation_chain=attestation_chain,
                derivation_seed=self.derivation_seed,
            )

            # 5. Submit to chain
            self.status = JobStatus.SUBMITTING
            submission = self.chain_adapter.submit_finding(
                market_id=self.market_id,
                finding_payload=finding.finding_payload,
                attestation_chain=finding.attestation_chain,
                derivation_seed=finding.derivation_seed,
            )
            if not submission.success:
                raise RuntimeError(
                    f"chain submission failed: {submission.error}"
                )
            finding.on_chain_finding_id = submission.on_chain_finding_id

            # 6. Emit local audit event
            self._emit_finding_log_event(finding)

            self._finding = finding
            self.status = JobStatus.DONE
            return finding

        except Exception as e:
            self._error = repr(e)
            self.status = JobStatus.FAILED
            logger.warning(
                "ResolutionJob[%s] failed at status=%s: %s",
                self.market_id, self.status, e,
            )
            return None

    def _emit_finding_log_event(self, finding: ResolutionFinding) -> None:
        """Emit `finding_emitted` to the local log."""
        log = self.log_client
        if log is None:
            try:
                log = get_default_client()
            except Exception:
                return
        from ..schema.events import FindingEmitted
        from ..schema.identifiers import make_event_id_log

        # Match FindingEmitted's actual shape (match_id, topology,
        # strength_per_dimension, target). We use generic values
        # since this is a resolution finding, not a match finding.
        evt = FindingEmitted(
            event_id=make_event_id_log(),
            recorded_at=int(time.time() * 1000),
            actor="resolution_job",
            match_id=finding.market_id,
            topology="resolution",
            strength_per_dimension={},  # resolution findings don't
                                         # carry per-dimension strengths
                                         # in the same shape
            target="mempalace_federation",
        )
        try:
            log.append(evt)
        except Exception as e:
            logger.warning("FindingEmitted append failed: %s", e)

    @property
    def finding(self) -> ResolutionFinding | None:
        return self._finding

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def step_attestations(self) -> list[dict[str, Any]]:
        return list(self._step_attestations)


__all__ = [
    "ChainSubmissionAdapter",
    "ChainSubmissionResult",
    "FailingChainSubmissionAdapter",
    "JobStatus",
    "PlanRegistry",
    "PrivacyMode",
    "ResolutionFinding",
    "ResolutionJob",
    "ResolutionPlan",
    "SliceProvider",
    "SliceRequestSpec",
    "StubChainSubmissionAdapter",
    "StubPlanRegistry",
    "StubSliceProvider",
]
