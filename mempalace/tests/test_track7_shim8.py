"""Tests for Track 7-shim-8 — ResolutionJob.execute() implementation.

Covers:
  - Happy path: ResolutionJob with default stubs → finding produced,
    status DONE, attestation_chain populated, on_chain_finding_id set.
  - Custom plan: register plan in PlanRegistry → plan executed; step
    attestations carry plan id + hash.
  - Slice provider feeds bytes into plan: payload reaches plan.execute.
  - Chain submission failure → status FAILED, error populated, finding
    None.
  - FindingEmitted log event appears on success.
  - Step kinds (compute_strength, extract_signature, noop) produce
    expected outputs.
  - derivation_seed (Case C) propagates through to chain submission.
  - Slice provider raising → status FAILED.
"""

from __future__ import annotations

import unittest

from mempalace.switchboard.job import (
    ChainSubmissionResult,
    FailingChainSubmissionAdapter,
    JobStatus,
    PrivacyMode,
    ResolutionFinding,
    ResolutionJob,
    ResolutionPlan,
    SliceRequestSpec,
    StubChainSubmissionAdapter,
    StubPlanRegistry,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state


# =============================================================================
# Helpers
# =============================================================================


class _CannedSliceProvider:
    """SliceProvider that returns a fixed payload."""

    def __init__(self, payload: bytes = b"slice-bytes") -> None:
        self.payload = payload
        self.requested: list[SliceRequestSpec] = []

    def request_slice(self, spec: SliceRequestSpec) -> bytes:
        self.requested.append(spec)
        return self.payload


class _RaisingSliceProvider:
    def request_slice(self, spec: SliceRequestSpec) -> bytes:
        raise RuntimeError("slice fetch failed")


def _build_job(
    *,
    market_id: str = "mkt_test",
    privacy_mode: PrivacyMode = PrivacyMode.SANDBOX,
    subject_palace_id: str = "palace_subj",
    slice_provider=None,
    plan_registry=None,
    chain_adapter=None,
    log_client=None,
    derivation_seed: bytes | None = None,
) -> ResolutionJob:
    spec = SliceRequestSpec(
        subject_palace_id=subject_palace_id,
        layer_minimum=2,
    )
    return ResolutionJob(
        market_id=market_id,
        privacy_mode=privacy_mode,
        slice_request=spec,
        slice_provider=slice_provider,
        plan_registry=plan_registry,
        chain_adapter=chain_adapter,
        log_client=log_client,
        derivation_seed=derivation_seed,
    )


# =============================================================================
# Happy path
# =============================================================================


class TestHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_default_stubs_produce_finding(self) -> None:
        """With all defaults, execute() returns a finding and reaches DONE."""
        job = _build_job(log_client=self.p["log"])
        finding = job.execute()

        self.assertIsNotNone(finding)
        self.assertIsInstance(finding, ResolutionFinding)
        self.assertEqual(job.status, JobStatus.DONE)
        self.assertIsNone(job.error)

    def test_finding_has_market_id(self) -> None:
        job = _build_job(market_id="mkt_specific", log_client=self.p["log"])
        finding = job.execute()
        assert finding is not None
        self.assertEqual(finding.market_id, "mkt_specific")

    def test_finding_has_attestation_chain(self) -> None:
        job = _build_job(log_client=self.p["log"])
        finding = job.execute()
        assert finding is not None
        self.assertIsInstance(finding.attestation_chain, bytes)
        self.assertGreater(len(finding.attestation_chain), 0)

        # Chain is JSON-encoded; should parse
        import json
        chain = json.loads(finding.attestation_chain.decode("utf-8"))
        self.assertIn("plan_id", chain)
        self.assertIn("plan_hash", chain)
        self.assertIn("steps", chain)

    def test_chain_submission_records_finding(self) -> None:
        adapter = StubChainSubmissionAdapter()
        job = _build_job(
            chain_adapter=adapter, log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None
        self.assertEqual(len(adapter.submitted), 1)
        record = adapter.submitted[0]
        self.assertEqual(record["market_id"], "mkt_test")
        self.assertEqual(record["finding_payload"], finding.finding_payload)

    def test_on_chain_finding_id_propagates(self) -> None:
        """The id returned by chain submission is set on the finding."""
        adapter = StubChainSubmissionAdapter()
        job = _build_job(
            chain_adapter=adapter, log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None
        self.assertEqual(finding.on_chain_finding_id, "finding_00000000")


# =============================================================================
# Slice ingestion
# =============================================================================


class TestSliceIngestion(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_slice_provider_called_with_spec(self) -> None:
        provider = _CannedSliceProvider(payload=b"some-slice")
        job = _build_job(
            slice_provider=provider,
            subject_palace_id="palace_xyz",
            log_client=self.p["log"],
        )
        job.execute()

        self.assertEqual(len(provider.requested), 1)
        spec = provider.requested[0]
        self.assertEqual(spec.subject_palace_id, "palace_xyz")

    def test_slice_provider_failure_fails_job(self) -> None:
        job = _build_job(
            slice_provider=_RaisingSliceProvider(),
            log_client=self.p["log"],
        )
        finding = job.execute()
        self.assertIsNone(finding)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIsNotNone(job.error)
        self.assertIn("slice fetch failed", job.error or "")


# =============================================================================
# Custom plans
# =============================================================================


class TestCustomPlans(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_custom_plan_executed(self) -> None:
        registry = StubPlanRegistry()
        plan = ResolutionPlan(
            plan_id="my_plan",
            plan_hash="abc123",
            steps=[
                {"kind": "extract_signature"},
                {"kind": "compute_strength"},
            ],
        )
        registry.register(PrivacyMode.SANDBOX, "mkt_x", plan)

        job = _build_job(
            market_id="mkt_x",
            privacy_mode=PrivacyMode.SANDBOX,
            plan_registry=registry,
            log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None

        # Step attestations should reflect the custom plan
        steps = job.step_attestations
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["step_kind"], "extract_signature")
        self.assertEqual(steps[1]["step_kind"], "compute_strength")

        # Finding payload should be the compute_strength output
        # (last step's output)
        self.assertEqual(finding.finding_payload, b'{"mood":0.7,"theme":0.3}')

    def test_attestation_chain_includes_plan_hash(self) -> None:
        registry = StubPlanRegistry()
        plan = ResolutionPlan(
            plan_id="plan_v2",
            plan_hash="hash_of_plan_v2",
            steps=[{"kind": "noop"}],
        )
        registry.register(PrivacyMode.SANDBOX, "mkt_y", plan)
        job = _build_job(
            market_id="mkt_y",
            plan_registry=registry,
            log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None

        import json
        chain = json.loads(finding.attestation_chain.decode("utf-8"))
        self.assertEqual(chain["plan_id"], "plan_v2")
        self.assertEqual(chain["plan_hash"], "hash_of_plan_v2")
        self.assertEqual(chain["privacy_mode"], "sandbox")

    def test_empty_plan_passes_slice_through(self) -> None:
        """Plan with no steps: finding_payload = slice_bytes."""
        registry = StubPlanRegistry()
        plan = ResolutionPlan(
            plan_id="empty",
            plan_hash="0",
            steps=[],
        )
        registry.register(PrivacyMode.LOCAL_ONLY, "mkt_e", plan)

        provider = _CannedSliceProvider(payload=b"raw-slice-bytes")
        job = _build_job(
            market_id="mkt_e",
            privacy_mode=PrivacyMode.LOCAL_ONLY,
            slice_provider=provider,
            plan_registry=registry,
            log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None
        self.assertEqual(finding.finding_payload, b"raw-slice-bytes")


# =============================================================================
# Chain submission failure
# =============================================================================


class TestChainFailure(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_failing_adapter_fails_job(self) -> None:
        adapter = FailingChainSubmissionAdapter(error="rpc timeout")
        job = _build_job(
            chain_adapter=adapter, log_client=self.p["log"],
        )
        finding = job.execute()

        self.assertIsNone(finding)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("chain submission failed", job.error or "")
        self.assertIn("rpc timeout", job.error or "")
        self.assertEqual(adapter.attempted, 1)

    def test_failed_job_emits_no_finding_event(self) -> None:
        """When chain submission fails, no FindingEmitted is logged."""
        adapter = FailingChainSubmissionAdapter()
        job = _build_job(
            chain_adapter=adapter, log_client=self.p["log"],
        )
        job.execute()

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        finding_evts = [
            payload for _o, kind, payload in rows
            if kind == "finding_emitted"
        ]
        self.assertEqual(len(finding_evts), 0)


# =============================================================================
# Audit log emission
# =============================================================================


class TestAuditEmission(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_finding_emitted_appears_in_log(self) -> None:
        job = _build_job(market_id="mkt_audit", log_client=self.p["log"])
        finding = job.execute()
        assert finding is not None

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        finding_evts = [
            payload for _o, kind, payload in rows
            if kind == "finding_emitted"
        ]
        self.assertEqual(len(finding_evts), 1)
        evt = finding_evts[0]
        self.assertEqual(evt["match_id"], "mkt_audit")
        self.assertEqual(evt["topology"], "resolution")
        self.assertEqual(evt["target"], "mempalace_federation")


# =============================================================================
# Subject-blind path (Case C)
# =============================================================================


class TestSubjectBlind(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_derivation_seed_propagates(self) -> None:
        """Case C: derivation_seed is included in chain submission for
        future challenge replay."""
        adapter = StubChainSubmissionAdapter()
        seed = b"derivation-seed-xyz"
        job = _build_job(
            chain_adapter=adapter,
            derivation_seed=seed,
            log_client=self.p["log"],
        )
        finding = job.execute()
        assert finding is not None
        self.assertEqual(finding.derivation_seed, seed)
        self.assertEqual(adapter.submitted[0]["derivation_seed"], seed)


# =============================================================================
# Status transitions
# =============================================================================


class TestStatusTransitions(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_status_starts_assigned(self) -> None:
        job = _build_job(log_client=self.p["log"])
        self.assertEqual(job.status, JobStatus.ASSIGNED)

    def test_status_done_on_success(self) -> None:
        job = _build_job(log_client=self.p["log"])
        job.execute()
        self.assertEqual(job.status, JobStatus.DONE)

    def test_status_failed_on_slice_error(self) -> None:
        job = _build_job(
            slice_provider=_RaisingSliceProvider(),
            log_client=self.p["log"],
        )
        job.execute()
        self.assertEqual(job.status, JobStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
