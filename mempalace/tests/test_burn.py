"""Tests for Track 6E — burn-palace cloud-box-side handling.

Covers:
  - IntegrityLockoutGate: starts untripped; trip() flips state; check()
    raises after trip; trip() is idempotent (only one event emitted).
  - signal_burn: phone-initiated burn trips gate, emits IntegrityLockout
    event with reason="burn_palace".
  - signal_burn calls key_manager.idle_zero() if provided.
  - BurnDetector: counts consecutive failures, trips gate at threshold,
    records success resets counter.
  - Lockout reasons: enum-style constants exposed for callers.
"""

from __future__ import annotations

import unittest

from mempalace.secure.burn import (
    REASON_ATTESTATION_CHAIN_BROKEN,
    REASON_BUNDLE_REFRESH_FAILED,
    REASON_BURN_PALACE,
    REASON_OPERATOR_INITIATED,
    BurnDetector,
    IntegrityLockoutGate,
    LockoutError,
    signal_burn,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state


# =============================================================================
# IntegrityLockoutGate
# =============================================================================


class TestIntegrityLockoutGate(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_starts_untripped(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        self.assertFalse(gate.is_tripped())
        # check() doesn't raise
        gate.check()

    def test_trip_flips_state(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        result = gate.trip(REASON_BURN_PALACE)
        self.assertTrue(result)
        self.assertTrue(gate.is_tripped())
        self.assertEqual(gate.reason(), REASON_BURN_PALACE)

    def test_check_raises_after_trip(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        gate.trip(REASON_BURN_PALACE)
        with self.assertRaises(LockoutError):
            gate.check()

    def test_trip_idempotent(self) -> None:
        """A second trip is a no-op; only one event is emitted."""
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        first = gate.trip(REASON_BURN_PALACE)
        second = gate.trip(REASON_OPERATOR_INITIATED)

        self.assertTrue(first)
        self.assertFalse(second)
        # Reason is from the FIRST trip
        self.assertEqual(gate.reason(), REASON_BURN_PALACE)

        # Only one IntegrityLockout event in the log
        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        events = [
            payload
            for _o, kind, payload in rows
            if kind == "integrity_lockout"
        ]
        self.assertEqual(len(events), 1)

    def test_trip_emits_event(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        gate.trip(REASON_BURN_PALACE, triggered_by="phone")

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        events = [
            payload
            for _o, kind, payload in rows
            if kind == "integrity_lockout"
        ]
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["reason"], REASON_BURN_PALACE)
        self.assertEqual(evt["triggered_by"], "phone")
        self.assertGreater(evt["locked_out_at_ms"], 0)


# =============================================================================
# signal_burn
# =============================================================================


class _FakeKeyManager:
    """Minimal stub for tests."""

    def __init__(self) -> None:
        self.idle_zero_calls = 0

    def idle_zero(self) -> None:
        self.idle_zero_calls += 1


class _RaisingKeyManager:
    """Key manager whose idle_zero throws — burn should still complete."""

    def idle_zero(self) -> None:
        raise RuntimeError("idle_zero exploded")


class TestSignalBurn(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_signal_burn_trips_gate(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        result = signal_burn(gate=gate)
        self.assertTrue(result)
        self.assertTrue(gate.is_tripped())
        self.assertEqual(gate.reason(), REASON_BURN_PALACE)

    def test_signal_burn_zeros_bundle_if_provided(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        km = _FakeKeyManager()
        signal_burn(gate=gate, key_manager=km)
        self.assertEqual(km.idle_zero_calls, 1)

    def test_signal_burn_handles_idle_zero_failure(self) -> None:
        """Even if idle_zero throws, the gate still trips."""
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        km = _RaisingKeyManager()
        # Should not raise
        result = signal_burn(gate=gate, key_manager=km)
        self.assertTrue(result)
        self.assertTrue(gate.is_tripped())

    def test_signal_burn_idempotent(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        first = signal_burn(gate=gate)
        second = signal_burn(gate=gate)
        self.assertTrue(first)
        self.assertFalse(second)


# =============================================================================
# BurnDetector
# =============================================================================


class TestBurnDetector(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_records_failures_below_threshold(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        det = BurnDetector(gate=gate, failure_threshold=3)

        for _ in range(2):
            tripped = det.record_refresh_failure()
            self.assertFalse(tripped)
        self.assertFalse(gate.is_tripped())
        self.assertEqual(det.consecutive_failures(), 2)

    def test_threshold_trips_gate(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        det = BurnDetector(gate=gate, failure_threshold=3)

        for i in range(3):
            tripped = det.record_refresh_failure()
            if i < 2:
                self.assertFalse(tripped)
            else:
                self.assertTrue(tripped)

        self.assertTrue(gate.is_tripped())
        self.assertEqual(gate.reason(), REASON_BUNDLE_REFRESH_FAILED)

    def test_success_resets_counter(self) -> None:
        gate = IntegrityLockoutGate(log_client=self.p["log"])
        det = BurnDetector(gate=gate, failure_threshold=3)

        det.record_refresh_failure()
        det.record_refresh_failure()
        self.assertEqual(det.consecutive_failures(), 2)

        det.record_refresh_success()
        self.assertEqual(det.consecutive_failures(), 0)

        # After reset, can fail again without tripping
        det.record_refresh_failure()
        det.record_refresh_failure()
        self.assertFalse(gate.is_tripped())


# =============================================================================
# Reason constants
# =============================================================================


class TestReasonConstants(unittest.TestCase):
    def test_reasons_are_distinct(self) -> None:
        all_reasons = {
            REASON_BURN_PALACE,
            REASON_BUNDLE_REFRESH_FAILED,
            REASON_ATTESTATION_CHAIN_BROKEN,
            REASON_OPERATOR_INITIATED,
        }
        self.assertEqual(len(all_reasons), 4)

    def test_reasons_are_strings(self) -> None:
        for reason in (
            REASON_BURN_PALACE,
            REASON_BUNDLE_REFRESH_FAILED,
            REASON_ATTESTATION_CHAIN_BROKEN,
            REASON_OPERATOR_INITIATED,
        ):
            self.assertIsInstance(reason, str)
            self.assertGreater(len(reason), 0)


if __name__ == "__main__":
    unittest.main()
