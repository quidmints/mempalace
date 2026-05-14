"""
Tests for R3 §7.6 — phone-off graceful degradation.

Coverage:
  - State transitions: ONLINE → READ_ONLY → LOCKED_OUT
  - Heartbeat recovery: READ_ONLY → ONLINE on fresh heartbeat
  - Key TTL expiry triggers READ_ONLY independently of heartbeat staleness
  - Lockout requires re-enrollment to recover
  - Audit-trail events: PhoneOffModeChanged + IntegrityLockout
  - Threadsafety (basic — no concurrent corruption)
  - Configuration: custom thresholds work
"""

from __future__ import annotations

import unittest
from typing import Any

from mempalace.log.client import get_default_client
from mempalace.schema.events import (
    HeartbeatReceived,
    IntegrityLockout,
    PhoneOffModeChanged,
)
from mempalace.secure.phone_off import (
    DEFAULT_KEY_TTL_MS,
    PhoneOffConfig,
    PhoneOffMode,
    PhoneOffStateMachine,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state


class _ManualClock:
    """Clock the test can advance explicitly."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


HOUR_MS = 60 * 60 * 1000


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.clock = _ManualClock()
        self.sm = PhoneOffStateMachine(clock=self.clock)
        # Start in a clean state: heartbeat just received, keys fresh
        self.sm.record_heartbeat("device_a")
        self.sm.record_keys_issued()

    def test_initial_state_online(self) -> None:
        self.assertEqual(self.sm.current_mode, PhoneOffMode.ONLINE)
        self.assertTrue(self.sm.is_write_allowed())
        self.assertTrue(self.sm.is_read_allowed())

    def test_heartbeat_stale_triggers_readonly(self) -> None:
        # Advance past read_only_threshold (cadence + grace = 2 hours)
        self.clock.advance(2 * HOUR_MS + 1)
        mode = self.sm.tick()
        self.assertEqual(mode, PhoneOffMode.READ_ONLY)
        self.assertFalse(self.sm.is_write_allowed())
        self.assertTrue(self.sm.is_read_allowed())

    def test_keys_expired_triggers_readonly(self) -> None:
        # Heartbeat stays fresh, but keys TTL elapses
        self.clock.advance(DEFAULT_KEY_TTL_MS + 1)
        # Refresh heartbeat at the new "now" so heartbeat stays fresh
        self.sm.record_heartbeat("device_a")
        # but keys are still expired (we only refreshed heartbeat,
        # not keys)
        mode = self.sm.tick()
        self.assertEqual(mode, PhoneOffMode.READ_ONLY)

    def test_three_missed_heartbeats_triggers_lockout(self) -> None:
        # 3 hours since last heartbeat = 3 missed at 1-hour cadence
        self.clock.advance(3 * HOUR_MS + 1)
        mode = self.sm.tick()
        self.assertEqual(mode, PhoneOffMode.LOCKED_OUT)
        self.assertFalse(self.sm.is_write_allowed())
        self.assertFalse(self.sm.is_read_allowed())

    def test_heartbeat_recovers_readonly_to_online(self) -> None:
        # Get to READ_ONLY
        self.clock.advance(2 * HOUR_MS + 1)
        self.sm.tick()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.READ_ONLY)
        # Phone reconnects with a fresh heartbeat — also issues fresh keys
        self.sm.record_keys_issued()
        self.sm.record_heartbeat("device_a")
        self.assertEqual(self.sm.current_mode, PhoneOffMode.ONLINE)

    def test_heartbeat_does_not_recover_lockout(self) -> None:
        # Get to LOCKED_OUT
        self.clock.advance(3 * HOUR_MS + 1)
        self.sm.tick()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.LOCKED_OUT)
        # Heartbeat alone does NOT recover
        self.sm.record_heartbeat("device_a")
        self.assertEqual(self.sm.current_mode, PhoneOffMode.LOCKED_OUT)

    def test_force_unlock_after_reenrollment_recovers(self) -> None:
        # Get to LOCKED_OUT
        self.clock.advance(3 * HOUR_MS + 1)
        self.sm.tick()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.LOCKED_OUT)
        # Re-enroll
        self.sm.force_unlock_after_reenrollment()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.ONLINE)

    def test_force_unlock_when_not_locked_out_is_noop(self) -> None:
        # Currently ONLINE — calling unlock should be a no-op
        self.sm.force_unlock_after_reenrollment()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.ONLINE)

    def test_keys_refresh_recovers_readonly_to_online(self) -> None:
        """READ_ONLY due to keys expired (heartbeat fresh) — refreshing
        keys alone should recover to ONLINE."""
        self.clock.advance(DEFAULT_KEY_TTL_MS + 1)
        self.sm.record_heartbeat("device_a")
        self.sm.tick()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.READ_ONLY)
        self.sm.record_keys_issued()
        self.assertEqual(self.sm.current_mode, PhoneOffMode.ONLINE)

    def test_time_until_lockout(self) -> None:
        # 1 hour after last heartbeat → 2 hours until lockout (3hr - 1hr)
        self.clock.advance(HOUR_MS)
        self.assertAlmostEqual(
            self.sm.time_until_lockout_ms() / HOUR_MS,
            2.0,
            places=2,
        )

    def test_time_until_lockout_negative_when_locked(self) -> None:
        self.clock.advance(3 * HOUR_MS + 1)
        self.sm.tick()
        self.assertEqual(self.sm.time_until_lockout_ms(), -1)


# ---------------------------------------------------------------------------
# Audit-trail events
# ---------------------------------------------------------------------------


class TestAuditTrail(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.clock = _ManualClock()
        self.sm = PhoneOffStateMachine(clock=self.clock)
        self.sm.record_heartbeat("d")
        self.sm.record_keys_issued()
        self.log = get_default_client()

    def _events_of_kind(self, kind: str) -> list[dict[str, Any]]:
        end = self.log.current_offset() + 1
        return [
            payload
            for _, k, payload in self.log.read_range(1, end)
            if k == kind
        ]

    def test_mode_change_emits_event(self) -> None:
        self.clock.advance(2 * HOUR_MS + 1)
        self.sm.tick()
        events = self._events_of_kind("phone_off_mode_changed")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_mode"], "online")
        self.assertEqual(events[0]["to_mode"], "read_only")
        self.assertEqual(events[0]["reason"], "heartbeat_stale")

    def test_lockout_emits_both_events(self) -> None:
        self.clock.advance(3 * HOUR_MS + 1)
        self.sm.tick()
        mode_events = self._events_of_kind("phone_off_mode_changed")
        lockout_events = self._events_of_kind("integrity_lockout")
        # Mode-change to LOCKED_OUT
        self.assertTrue(any(
            e["to_mode"] == "locked_out" for e in mode_events
        ))
        # Plus one IntegrityLockout
        self.assertEqual(len(lockout_events), 1)
        self.assertEqual(lockout_events[0]["triggered_by"], "daemon")

    def test_heartbeat_emits_event(self) -> None:
        # Initial setUp emitted one; record another and count them
        prior = len(self._events_of_kind("heartbeat_received"))
        self.sm.record_heartbeat("d", slot=42)
        after = len(self._events_of_kind("heartbeat_received"))
        self.assertEqual(after, prior + 1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestCustomConfig(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()

    def test_custom_thresholds_change_lockout_timing(self) -> None:
        clock = _ManualClock()
        # Tight config: 10 minute cadence, 5 minute grace, 2 missed = lockout
        cfg = PhoneOffConfig(
            heartbeat_cadence_ms=10 * 60 * 1000,
            heartbeat_grace_ms=5 * 60 * 1000,
            missed_heartbeats_lockout=2,
        )
        sm = PhoneOffStateMachine(config=cfg, clock=clock)
        sm.record_heartbeat("d")
        sm.record_keys_issued()
        # 16 minutes — past 10+5 = 15 min readonly threshold
        clock.advance(16 * 60 * 1000)
        sm.tick()
        self.assertEqual(sm.current_mode, PhoneOffMode.READ_ONLY)
        # 21 minutes total — past 2 * 10 = 20 min lockout threshold
        clock.advance(5 * 60 * 1000)
        sm.tick()
        self.assertEqual(sm.current_mode, PhoneOffMode.LOCKED_OUT)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()

    def test_no_heartbeat_no_keys_starts_online(self) -> None:
        """An uninitialized state machine starts ONLINE — the daemon
        only enters degraded modes when conditions degrade. The caller
        must call record_heartbeat / record_keys_issued before tick to
        avoid immediate transition."""
        clock = _ManualClock()
        sm = PhoneOffStateMachine(clock=clock)
        self.assertEqual(sm.current_mode, PhoneOffMode.ONLINE)

    def test_uninitialized_then_long_wait_goes_to_lockout(self) -> None:
        """If the daemon never received a heartbeat AND substantial
        time has passed, ticking should lock out."""
        clock = _ManualClock(start_ms=10 * HOUR_MS)
        sm = PhoneOffStateMachine(clock=clock)
        sm.tick()
        # heartbeat_age_ms returns now (10 hours), well past
        # lockout_threshold (3 hours).
        self.assertEqual(sm.current_mode, PhoneOffMode.LOCKED_OUT)

    def test_uninitialized_quick_tick_goes_readonly_not_lockout(self) -> None:
        """Right at startup with no heartbeat yet but no time elapsed,
        the state should be READ_ONLY (because keys are unset, treated
        as expired) but not LOCKED_OUT."""
        clock = _ManualClock(start_ms=HOUR_MS)  # 1 hour absolute
        sm = PhoneOffStateMachine(clock=clock)
        sm.tick()
        # heartbeat_age = 1hr, below 3hr lockout threshold
        # but keys_valid_until = 0 → treated as expired → READ_ONLY
        self.assertEqual(sm.current_mode, PhoneOffMode.READ_ONLY)

    def test_state_snapshot_is_a_copy(self) -> None:
        clock = _ManualClock()
        sm = PhoneOffStateMachine(clock=clock)
        sm.record_heartbeat("d")
        snap = sm.state
        original_mode = snap.mode
        # Mutate the snapshot — should not affect internal state
        snap.mode = PhoneOffMode.LOCKED_OUT
        self.assertNotEqual(sm.current_mode, snap.mode)
        self.assertEqual(sm.current_mode, original_mode)


if __name__ == "__main__":
    unittest.main()
