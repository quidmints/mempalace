"""
Phone-off graceful degradation — R3 §7.6.

# What this addresses

The daemon operates only as long as the phone provides fresh
attestations and the decryption keys it received from the phone are
within their TTL. When either condition fails, the daemon must
degrade gracefully rather than crash, refuse all queries, or worse
— continue operating with stale/expired keys.

R3 §7.6 specifies a three-state machine:

  - ONLINE: heartbeat fresh AND keys not TTL-expired. Full operation
    — read, write, heavy compute.
  - READ_ONLY: heartbeat stale (≥1 hour since last) OR keys expired.
    Serve queries from already-decrypted state; no log writes; no
    heavy operations. Recovers when phone reconnects with a fresh
    heartbeat (which also refreshes keys).
  - LOCKED_OUT: 3 consecutive missed heartbeats (≥3 hours). Daemon
    stops; on-chain `trigger_app_integrity_lockout` instruction
    callable by the contract; user must re-enroll to resume.

The state machine is the daemon's single source of truth for
"what's allowed right now."

# Configuration defaults

  - Heartbeat cadence: 1 hour
  - Heartbeat grace before READ_ONLY: 1 hour beyond cadence (so 2 hours
    since last seen)
  - Missed-heartbeat threshold for LOCKOUT: 3 (so 3 hours since last
    seen → LOCKOUT)
  - Decryption key TTL: 24 hours

All four are user-configurable per device.

# Where this fits

  - The daemon's main loop calls `state_machine.tick(now_ms)` on a
    timer (or on each request, whichever comes first).
  - Crypto operations check `state_machine.is_write_allowed()`
    before proceeding.
  - The on-chain `trigger_app_integrity_lockout` instruction
    consumes `IntegrityLockout` events emitted from this module
    when transitioning to LOCKED_OUT.

Spec ref: integration_appendix_r3.md §7.6 + §7.7.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    HeartbeatReceived,
    IntegrityLockout,
    PhoneOffModeChanged,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Tunables (user-configurable per device)
# =============================================================================


DEFAULT_HEARTBEAT_CADENCE_MS = 60 * 60 * 1000      # 1 hour
"""Expected interval between phone heartbeats."""

DEFAULT_HEARTBEAT_GRACE_MS = 60 * 60 * 1000        # 1 hour grace
"""How much past the cadence before we transition to READ_ONLY.
Combined with cadence, READ_ONLY triggers at 2 hours since last seen."""

DEFAULT_MISSED_HEARTBEATS_LOCKOUT = 3
"""Consecutive missed heartbeats before LOCKED_OUT. With the default
1-hour cadence, this is 3 hours of silence → lockout."""

DEFAULT_KEY_TTL_MS = 24 * 60 * 60 * 1000           # 24 hours
"""How long a session bundle's keys are valid after issue."""


# =============================================================================
# Mode enum
# =============================================================================


class PhoneOffMode(str, Enum):
    """Current operational mode of the daemon w.r.t. phone presence."""

    ONLINE = "online"
    """Heartbeat fresh AND keys valid. Full operation."""

    READ_ONLY = "read_only"
    """Heartbeat stale OR keys expired. Reads OK; writes blocked;
    heavy ops blocked. Recoverable with fresh heartbeat."""

    LOCKED_OUT = "locked_out"
    """3+ missed heartbeats. Daemon stops; on-chain lockout
    triggered. Requires re-enrollment to recover."""


# =============================================================================
# Configuration + state
# =============================================================================


@dataclass(frozen=True)
class PhoneOffConfig:
    """Tunables for the state machine."""

    heartbeat_cadence_ms: int = DEFAULT_HEARTBEAT_CADENCE_MS
    heartbeat_grace_ms: int = DEFAULT_HEARTBEAT_GRACE_MS
    missed_heartbeats_lockout: int = DEFAULT_MISSED_HEARTBEATS_LOCKOUT
    key_ttl_ms: int = DEFAULT_KEY_TTL_MS

    @property
    def read_only_threshold_ms(self) -> int:
        """Time since last heartbeat after which READ_ONLY triggers."""
        return self.heartbeat_cadence_ms + self.heartbeat_grace_ms

    @property
    def lockout_threshold_ms(self) -> int:
        """Time since last heartbeat after which LOCKED_OUT triggers."""
        return self.heartbeat_cadence_ms * self.missed_heartbeats_lockout


@dataclass
class PhoneOffState:
    """Mutable runtime state of the state machine."""

    mode: PhoneOffMode = PhoneOffMode.ONLINE
    last_heartbeat_at_ms: int = 0
    keys_issued_at_ms: int = 0
    keys_valid_until_ms: int = 0
    missed_heartbeat_count: int = 0
    locked_out_at_ms: int = 0

    def keys_expired(self, now_ms: int) -> bool:
        if self.keys_valid_until_ms == 0:
            # Never received any keys yet; treat as expired
            return True
        return now_ms > self.keys_valid_until_ms

    def heartbeat_age_ms(self, now_ms: int) -> int:
        if self.last_heartbeat_at_ms == 0:
            # Never received a heartbeat
            return now_ms  # effectively infinite
        return now_ms - self.last_heartbeat_at_ms


# =============================================================================
# State machine
# =============================================================================


class PhoneOffStateMachine:
    """The daemon's phone-presence state machine.

    Threadsafe: all transitions take a lock. The daemon's main loop
    or any caller can `record_heartbeat` / `record_keys_issued` /
    `tick` from any thread.

    The state machine emits log events on transitions:
      - `PhoneOffModeChanged` for every mode change (audit trail).
      - `IntegrityLockout` when transitioning to LOCKED_OUT (the
        downstream on-chain `trigger_app_integrity_lockout` consumes
        this).

    Side effects (transitions, lockout) only happen via `tick(now_ms)`.
    Pure queries (`is_write_allowed`, `current_mode`, etc.) don't
    advance state.
    """

    def __init__(
        self,
        *,
        config: PhoneOffConfig | None = None,
        log_client: LogClient | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._config = config or PhoneOffConfig()
        self._log = log_client or get_default_client()
        self._clock = clock or _default_clock_ms
        self._state = PhoneOffState()
        self._lock = threading.RLock()

    # ---- queries ------------------------------------------------------------

    @property
    def config(self) -> PhoneOffConfig:
        return self._config

    @property
    def state(self) -> PhoneOffState:
        """Snapshot of state. Held under the lock briefly."""
        with self._lock:
            return PhoneOffState(
                mode=self._state.mode,
                last_heartbeat_at_ms=self._state.last_heartbeat_at_ms,
                keys_issued_at_ms=self._state.keys_issued_at_ms,
                keys_valid_until_ms=self._state.keys_valid_until_ms,
                missed_heartbeat_count=self._state.missed_heartbeat_count,
                locked_out_at_ms=self._state.locked_out_at_ms,
            )

    @property
    def current_mode(self) -> PhoneOffMode:
        with self._lock:
            return self._state.mode

    def is_write_allowed(self) -> bool:
        """True iff the daemon may append to the log / run heavy ops.
        False in READ_ONLY and LOCKED_OUT."""
        with self._lock:
            return self._state.mode == PhoneOffMode.ONLINE

    def is_read_allowed(self) -> bool:
        """True iff the daemon may serve queries. False only in
        LOCKED_OUT."""
        with self._lock:
            return self._state.mode != PhoneOffMode.LOCKED_OUT

    def time_until_lockout_ms(self, now_ms: int | None = None) -> int:
        """Milliseconds until LOCKED_OUT given current state. Negative
        if already past threshold (or already in LOCKED_OUT)."""
        now = self._clock() if now_ms is None else now_ms
        with self._lock:
            if self._state.mode == PhoneOffMode.LOCKED_OUT:
                return -1
            if self._state.last_heartbeat_at_ms == 0:
                return -1  # never seen a heartbeat
            elapsed = now - self._state.last_heartbeat_at_ms
            return self._config.lockout_threshold_ms - elapsed

    # ---- input transitions --------------------------------------------------

    def record_heartbeat(
        self,
        device_pubkey: str,
        *,
        slot: int = 0,
        signature: str = "",
        now_ms: int | None = None,
    ) -> None:
        """A fresh heartbeat arrived from the phone.

        Side effects:
          - Updates `last_heartbeat_at_ms` to now.
          - Resets `missed_heartbeat_count` to 0.
          - Emits a `HeartbeatReceived` event to the log.
          - If currently READ_ONLY, transitions back to ONLINE
            (assuming keys also valid; if not, stays READ_ONLY but
            with the heartbeat-side condition cleared).
          - LOCKED_OUT does not recover on heartbeat alone — re-enrollment
            is required (see `force_unlock_after_reenrollment`).
        """
        now = self._clock() if now_ms is None else now_ms
        with self._lock:
            self._state.last_heartbeat_at_ms = now
            self._state.missed_heartbeat_count = 0
            self._log.append(HeartbeatReceived(
                device_pubkey=device_pubkey,
                slot=slot,
                signature=signature,
            ))

            if self._state.mode == PhoneOffMode.READ_ONLY:
                # Try to recover to ONLINE — only if keys also valid
                if not self._state.keys_expired(now):
                    self._transition_locked(
                        PhoneOffMode.ONLINE,
                        reason="phone_reconnected",
                        now_ms=now,
                    )
            elif self._state.mode == PhoneOffMode.LOCKED_OUT:
                # No-op: lockout requires re-enrollment.
                logger.warning(
                    "Heartbeat received in LOCKED_OUT mode; "
                    "ignored. Re-enrollment required.",
                )

    def record_keys_issued(
        self,
        *,
        valid_until_ms: int | None = None,
        issued_at_ms: int | None = None,
    ) -> None:
        """A new session bundle was issued; record the key TTL.

        If `valid_until_ms` is None, defaults to `issued_at_ms +
        config.key_ttl_ms`.
        """
        now = self._clock() if issued_at_ms is None else issued_at_ms
        valid_until = (
            valid_until_ms
            if valid_until_ms is not None
            else now + self._config.key_ttl_ms
        )
        with self._lock:
            self._state.keys_issued_at_ms = now
            self._state.keys_valid_until_ms = valid_until
            # Re-evaluate mode in case we were READ_ONLY due to expired
            # keys but now have fresh ones AND a recent heartbeat.
            if self._state.mode == PhoneOffMode.READ_ONLY:
                hb_age = self._state.heartbeat_age_ms(now)
                if hb_age <= self._config.read_only_threshold_ms:
                    self._transition_locked(
                        PhoneOffMode.ONLINE,
                        reason="keys_refreshed",
                        now_ms=now,
                    )

    def tick(self, now_ms: int | None = None) -> PhoneOffMode:
        """Advance the state machine to reflect time passing.

        The daemon's main loop calls this periodically (e.g., every
        few seconds, or before each request). Returns the resulting
        mode after the tick.

        Logic:
          - From ONLINE: if heartbeat stale OR keys expired → READ_ONLY.
            Plus: if heartbeat past lockout-threshold → LOCKED_OUT.
          - From READ_ONLY: if heartbeat past lockout-threshold →
            LOCKED_OUT.
          - From LOCKED_OUT: terminal.
        """
        now = self._clock() if now_ms is None else now_ms
        with self._lock:
            mode = self._state.mode
            if mode == PhoneOffMode.LOCKED_OUT:
                return mode

            hb_age = self._state.heartbeat_age_ms(now)
            keys_dead = self._state.keys_expired(now)

            # Lockout has highest priority — covers both ONLINE and READ_ONLY
            if hb_age >= self._config.lockout_threshold_ms:
                self._transition_to_lockout_locked(
                    reason="missed_heartbeats_3",
                    now_ms=now,
                )
                return self._state.mode

            # ONLINE → READ_ONLY conditions
            if mode == PhoneOffMode.ONLINE:
                if hb_age >= self._config.read_only_threshold_ms:
                    self._transition_locked(
                        PhoneOffMode.READ_ONLY,
                        reason="heartbeat_stale",
                        now_ms=now,
                    )
                elif keys_dead:
                    self._transition_locked(
                        PhoneOffMode.READ_ONLY,
                        reason="keys_expired",
                        now_ms=now,
                    )

            # Compute missed-heartbeat count for the audit trail
            if self._state.last_heartbeat_at_ms > 0:
                missed = hb_age // self._config.heartbeat_cadence_ms
                self._state.missed_heartbeat_count = max(0, int(missed))

            return self._state.mode

    def force_unlock_after_reenrollment(
        self,
        *,
        now_ms: int | None = None,
    ) -> None:
        """Operator-driven recovery from LOCKED_OUT after the user
        completes re-enrollment.

        This bypasses the normal state machine because the recovery
        path runs out-of-band (re-enrollment requires phone interaction
        the daemon can't initiate). The caller must verify the
        re-enrollment cryptographically before calling this.
        """
        now = self._clock() if now_ms is None else now_ms
        with self._lock:
            if self._state.mode != PhoneOffMode.LOCKED_OUT:
                logger.warning(
                    "force_unlock_after_reenrollment called when not "
                    "LOCKED_OUT (current=%s); ignoring",
                    self._state.mode,
                )
                return
            # Reset state machine to a known-clean ONLINE
            self._state.missed_heartbeat_count = 0
            self._state.locked_out_at_ms = 0
            self._transition_locked(
                PhoneOffMode.ONLINE,
                reason="reenrollment_completed",
                now_ms=now,
            )

    # ---- internal -----------------------------------------------------------

    def _transition_locked(
        self,
        to_mode: PhoneOffMode,
        *,
        reason: str,
        now_ms: int,
    ) -> None:
        """Caller MUST hold self._lock."""
        from_mode = self._state.mode
        if from_mode == to_mode:
            return
        self._state.mode = to_mode
        evt = PhoneOffModeChanged(
            from_mode=from_mode.value,
            to_mode=to_mode.value,
            reason=reason,
            now_ms=now_ms,
            missed_heartbeat_count=self._state.missed_heartbeat_count,
            last_heartbeat_at_ms=self._state.last_heartbeat_at_ms,
        )
        try:
            self._log.append(evt)
        except Exception as e:
            # Best-effort logging — even if append fails, the in-memory
            # transition still happened. Log to stderr.
            logger.error("Failed to append PhoneOffModeChanged: %s", e)

    def _transition_to_lockout_locked(
        self,
        *,
        reason: str,
        now_ms: int,
    ) -> None:
        """Caller MUST hold self._lock. Special-case for LOCKED_OUT
        because we also emit IntegrityLockout."""
        self._transition_locked(
            PhoneOffMode.LOCKED_OUT,
            reason=reason,
            now_ms=now_ms,
        )
        self._state.locked_out_at_ms = now_ms
        try:
            self._log.append(IntegrityLockout(
                reason=reason,
                triggered_by="daemon",
                locked_out_at_ms=now_ms,
            ))
        except Exception as e:
            logger.error("Failed to append IntegrityLockout: %s", e)


# =============================================================================
# Helpers
# =============================================================================


def _default_clock_ms() -> int:
    import time
    return int(time.time() * 1000)


__all__ = [
    "DEFAULT_HEARTBEAT_CADENCE_MS",
    "DEFAULT_HEARTBEAT_GRACE_MS",
    "DEFAULT_KEY_TTL_MS",
    "DEFAULT_MISSED_HEARTBEATS_LOCKOUT",
    "PhoneOffConfig",
    "PhoneOffMode",
    "PhoneOffState",
    "PhoneOffStateMachine",
]
