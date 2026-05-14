"""Tests for the switchboard SDK skeleton.

The skeleton only commits to module shape, not behavior. These
tests verify:

  - The package imports cleanly.
  - Class shapes (ResolutionJob, ResolutionFinding, SliceRequestSpec)
    have the documented fields.
  - The opt-in flag (MEMPALACE_SWITCHBOARD_ENABLED) controls
    is_switchboard_node_enabled correctly.
  - The listener can be constructed, started, stopped without
    crashing.

Behavioral tests (actual job execution, on-chain dispatch) land
when the on-chain instructions are written.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestSwitchboardImports(unittest.TestCase):
    def test_package_imports(self) -> None:
        import mempalace.switchboard  # noqa: F401

    def test_top_level_exports(self) -> None:
        import mempalace.switchboard as sb
        for name in [
            "JobStatus",
            "ResolutionFinding",
            "ResolutionJob",
            "SliceRequestSpec",
            "SwitchboardNodeListener",
            "is_switchboard_node_enabled",
        ]:
            self.assertTrue(hasattr(sb, name), f"missing export: {name}")


class TestJobStatus(unittest.TestCase):
    def test_lifecycle_states_defined(self) -> None:
        from mempalace.switchboard import JobStatus
        for state in [
            "ASSIGNED",
            "INGESTING",
            "RUNNING",
            "SUBMITTING",
            "DONE",
            "FAILED",
        ]:
            self.assertTrue(
                hasattr(JobStatus, state),
                f"missing JobStatus.{state}",
            )


class TestSliceRequestSpec(unittest.TestCase):
    def test_construct(self) -> None:
        from mempalace.switchboard import SliceRequestSpec
        spec = SliceRequestSpec(
            subject_palace_id="palace_subj_xyz",
            layer_minimum=2,
            scope_predicates={"theme_id": "th_career"},
        )
        self.assertEqual(spec.subject_palace_id, "palace_subj_xyz")
        self.assertEqual(spec.layer_minimum, 2)
        self.assertEqual(spec.scope_predicates["theme_id"], "th_career")

    def test_scope_predicates_default_empty(self) -> None:
        from mempalace.switchboard import SliceRequestSpec
        spec = SliceRequestSpec(
            subject_palace_id="palace_subj_xyz",
            layer_minimum=1,
        )
        self.assertEqual(spec.scope_predicates, {})


class TestResolutionFinding(unittest.TestCase):
    def test_construct(self) -> None:
        from mempalace.switchboard import ResolutionFinding
        f = ResolutionFinding(
            market_id="mkt_x",
            finding_payload=b'{"answer": "yes"}',
            attestation_chain=b"\x00\x01\x02",
        )
        self.assertEqual(f.market_id, "mkt_x")
        self.assertIsNone(f.derivation_seed)

    def test_derivation_seed_optional(self) -> None:
        from mempalace.switchboard import ResolutionFinding
        f = ResolutionFinding(
            market_id="mkt_x",
            finding_payload=b"",
            attestation_chain=b"",
            derivation_seed=b"\xab" * 32,
        )
        self.assertEqual(f.derivation_seed, b"\xab" * 32)


class TestResolutionJob(unittest.TestCase):
    def test_construct(self) -> None:
        from mempalace.switchboard import ResolutionJob, SliceRequestSpec, JobStatus
        from mempalace.switchboard.job import PrivacyMode
        spec = SliceRequestSpec(
            subject_palace_id="palace_subj_xyz",
            layer_minimum=2,
        )
        job = ResolutionJob(
            market_id="mkt_x",
            privacy_mode=PrivacyMode.SANDBOX,
            slice_request=spec,
        )
        self.assertEqual(job.status, JobStatus.ASSIGNED)
        self.assertIsNone(job.finding)
        self.assertIsNone(job.error)

    def test_execute_returns_finding_with_default_stubs(self) -> None:
        """Track 7-shim-8: execute() now runs the full pipeline using
        default stubs. This test replaces the prior NotImplementedError
        sentinel; behavioral coverage is in test_track7_shim8.py.
        """
        from mempalace.switchboard import ResolutionJob, SliceRequestSpec
        from mempalace.switchboard.job import (
            JobStatus, PrivacyMode, ResolutionFinding,
        )
        spec = SliceRequestSpec(
            subject_palace_id="palace_subj_xyz",
            layer_minimum=2,
        )
        job = ResolutionJob(
            market_id="mkt_x",
            privacy_mode=PrivacyMode.SANDBOX,
            slice_request=spec,
        )
        finding = job.execute()
        self.assertIsInstance(finding, ResolutionFinding)
        self.assertEqual(job.status, JobStatus.DONE)


class TestNodeListenerEnabled(unittest.TestCase):
    def setUp(self) -> None:
        self._original_env = os.environ.get("MEMPALACE_SWITCHBOARD_ENABLED")
        os.environ.pop("MEMPALACE_SWITCHBOARD_ENABLED", None)

    def tearDown(self) -> None:
        os.environ.pop("MEMPALACE_SWITCHBOARD_ENABLED", None)
        if self._original_env is not None:
            os.environ["MEMPALACE_SWITCHBOARD_ENABLED"] = self._original_env

    def test_disabled_by_default(self) -> None:
        from mempalace.switchboard import is_switchboard_node_enabled
        self.assertFalse(is_switchboard_node_enabled())

    def test_enabled_when_env_set(self) -> None:
        from mempalace.switchboard import is_switchboard_node_enabled
        os.environ["MEMPALACE_SWITCHBOARD_ENABLED"] = "1"
        self.assertTrue(is_switchboard_node_enabled())

    def test_disabled_when_env_zero(self) -> None:
        from mempalace.switchboard import is_switchboard_node_enabled
        os.environ["MEMPALACE_SWITCHBOARD_ENABLED"] = "0"
        self.assertFalse(is_switchboard_node_enabled())


class TestNodeListenerLifecycle(unittest.TestCase):
    """The listener can be started and stopped without crashing.
    Polling is a no-op skeleton, so this just exercises the
    threading boilerplate."""

    def test_start_stop(self) -> None:
        from mempalace.switchboard import SwitchboardNodeListener
        listener = SwitchboardNodeListener(
            resolver_pubkey="pk_resolver_a",
            poll_interval_sec=0.05,  # fast for test
        )
        listener.start()
        # Let the loop tick once
        import time
        time.sleep(0.15)
        listener.stop(timeout=1.0)

    def test_double_start_warns_not_crashes(self) -> None:
        from mempalace.switchboard import SwitchboardNodeListener
        listener = SwitchboardNodeListener(
            resolver_pubkey="pk_resolver_a",
            poll_interval_sec=1.0,
        )
        listener.start()
        listener.start()  # should warn but not crash
        listener.stop()


if __name__ == "__main__":
    unittest.main()
