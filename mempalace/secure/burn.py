"""
Burn-palace cloud-box-side handling — Track 6E.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Tier 3 — Burn the palace":

  Different from Tier 2: not "delete this data" but "destroy the
  entire palace." Triple-confirmation flow on the phone destroys the
  Phone Master Key. Cloud-box session bundles can no longer be
  refreshed; on TTL expiry they idle-zero. All ciphertext on disk
  becomes unrecoverable. On-chain `IntegrityLockout` PDA fires per
  R3 §7.6 and stake is returned.

# What this module ships

The cloud-box-side machinery for burn detection + lockout:

  - `BurnDetector` watches the cloud-box key manager. When bundle
    refresh fails (after the configured retry window), it signals
    integrity-lockout state.
  - `signal_burn(reason)` is the user-initiated entry point. The
    phone signals to the cloud-box that a burn happened (via a
    separate channel from the encryption flow); the cloud-box
    emits `IntegrityLockout` and zeros its bundle.
  - `IntegrityLockoutGate` rejects new operations once burn fires.

# What this module does NOT ship

  - The phone-side Phone Master Key destruction. Hardware-dependent.
  - The on-chain `IntegrityLockout` PDA write. That's a Solana RPC
    call wired in via Track 7's switchboard layer; this module
    emits the local event that Track 7 picks up.
  - The actual session-bundle expiry. The existing key_manager's
    idle-zero handles that; we just trigger it via `signal_burn`.

# Why a separate module from key_manager.py

`key_manager.py` is the encryption-substrate machinery: sessions,
DEKs, attestation. Burn is a user-initiated business event that
*uses* the key manager but isn't part of the encryption protocol
itself. Keeping them separate means the encryption layer stays
narrow and the burn layer can evolve (e.g., add hardware-attestation
checks, integrate with chain-side events) without churning
key_manager.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Tier 3 — Burn the palace",
ENCRYPTION_AT_EDGE_DESIGN.md v2 §"SecureElement interface (revised)",
R3 §7.6.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..log.client import LogClient, get_default_client
from ..schema.events import IntegrityLockout
from ..schema.identifiers import make_event_id_log

if TYPE_CHECKING:
    from .key_manager import CloudBoxKeyManager

logger = logging.getLogger(__name__)


# =============================================================================
# Burn reasons
# =============================================================================


# Why was the cloud-box locked out? Used as the `reason` field in
# IntegrityLockout. Defines the canonical set; new reasons can be
# added but should be discoverable here.
REASON_BURN_PALACE = "burn_palace"
"""User triple-confirmed the burn flow on the phone."""

REASON_BUNDLE_REFRESH_FAILED = "bundle_refresh_failed"
"""Cloud-box tried to refresh its session bundle and failed N times.
Either the phone is gone (silently equivalent to a burn) or the
network can't reach the phone."""

REASON_ATTESTATION_CHAIN_BROKEN = "attestation_chain_broken"
"""Daemon attestation chain failed verification — signals the
cloud-box has been tampered with. Operator-side concern."""

REASON_BUNDLE_VERIFICATION_FAILED = "bundle_verification_failed"
"""A loaded bundle failed signature verification. Suggests the
phone is producing invalid bundles, or the daemon binary changed
without the phone re-attesting."""

REASON_OPERATOR_INITIATED = "operator_initiated"
"""Operator manually flipped the kill switch. Maintenance window,
investigation, etc."""


# =============================================================================
# IntegrityLockoutGate
# =============================================================================


class LockoutError(Exception):
    """Raised when an operation is attempted on a locked-out
    palace."""


@dataclass
class IntegrityLockoutGate:
    """Process-wide gate that rejects operations once burn has fired.

    Production wires this in front of every endpoint that touches
    user data. Tests construct directly.

    Idempotent. Once `trip()` has been called, all future `check()`s
    raise `LockoutError`. There is no untrip.
    """

    log_client: LogClient | None = None
    _tripped: bool = False
    _reason: str = ""
    _tripped_at_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.log_client is None:
            try:
                self.log_client = get_default_client()
            except Exception:
                self.log_client = None

    def check(self) -> None:
        """Raise LockoutError if the gate is tripped. Otherwise no-op.

        Production calls this at the top of every cloud-box endpoint.
        """
        with self._lock:
            if self._tripped:
                raise LockoutError(
                    f"palace is in integrity lockout (reason={self._reason})"
                )

    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def reason(self) -> str:
        """Why the gate was tripped. Empty string if not tripped."""
        with self._lock:
            return self._reason

    def trip(
        self,
        reason: str,
        *,
        triggered_by: str = "phone",
        now_ms: int | None = None,
    ) -> bool:
        """Trip the gate. Idempotent — second call is a no-op.

        Returns True if this call performed the trip; False if it
        was already tripped.

        Emits `IntegrityLockout` event the first time only.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        with self._lock:
            if self._tripped:
                return False
            self._tripped = True
            self._reason = reason
            self._tripped_at_ms = now_ms

        # Emit event outside the lock to avoid log-append re-entry
        self._emit_lockout_event(reason, triggered_by, now_ms)

        logger.warning(
            "IntegrityLockoutGate tripped reason=%s triggered_by=%s",
            reason, triggered_by,
        )
        return True

    def _emit_lockout_event(
        self, reason: str, triggered_by: str, now_ms: int,
    ) -> None:
        if self.log_client is None:
            return
        evt = IntegrityLockout(
            event_id=make_event_id_log(),
            recorded_at=now_ms,
            actor="burn_handler",
            reason=reason,
            triggered_by=triggered_by,
            locked_out_at_ms=now_ms,
        )
        try:
            self.log_client.append(evt)
        except Exception as e:
            logger.error("IntegrityLockout append failed: %s", e)


# =============================================================================
# BurnDetector
# =============================================================================


@dataclass
class BurnDetector:
    """Watches the cloud-box key manager for bundle-refresh failures.

    A real production implementation polls the key manager state and
    counts consecutive refresh failures. After the configured
    threshold, trips the lockout gate with
    `REASON_BUNDLE_REFRESH_FAILED`.

    For now this is a thin wrapper that operators can call manually
    on observed failure; the polling loop is environment-specific
    and lives in the daemon, not here.
    """

    gate: IntegrityLockoutGate
    failure_threshold: int = 5
    """How many consecutive refresh failures before locking out."""

    _consecutive_failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_refresh_success(self) -> None:
        """Reset the failure counter."""
        with self._lock:
            self._consecutive_failures = 0

    def record_refresh_failure(
        self, *, now_ms: int | None = None,
    ) -> bool:
        """Increment the failure counter; trip the gate if threshold
        exceeded.

        Returns True if the gate just tripped (this call).
        """
        with self._lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures

        if count >= self.failure_threshold:
            return self.gate.trip(
                REASON_BUNDLE_REFRESH_FAILED,
                triggered_by="daemon",
                now_ms=now_ms,
            )
        return False

    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures


# =============================================================================
# signal_burn — phone-initiated entry point
# =============================================================================


def signal_burn(
    *,
    gate: IntegrityLockoutGate,
    key_manager: "CloudBoxKeyManager | None" = None,
    now_ms: int | None = None,
) -> bool:
    """Phone signals burn. Cloud-box trips the gate + zeros the bundle.

    The phone calls this via whatever out-of-band channel exists
    (typically a privileged API endpoint that requires a phone
    attestation). The phone has already destroyed its Master Key
    locally; this just brings the cloud-box state into agreement.

    Returns True if this call performed the trip; False if already
    tripped.
    """
    just_tripped = gate.trip(
        REASON_BURN_PALACE,
        triggered_by="phone",
        now_ms=now_ms,
    )

    # Zero the bundle so any in-flight decryption fails fast
    if key_manager is not None:
        try:
            key_manager.idle_zero()
        except Exception as e:
            logger.error("Bundle idle_zero failed during burn: %s", e)

    return just_tripped


# =============================================================================
# Process-wide singleton
# =============================================================================


_GATE: IntegrityLockoutGate | None = None
_GATE_LOCK = threading.Lock()


def get_default_gate() -> IntegrityLockoutGate:
    """Return the process-wide gate, creating one if needed."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = IntegrityLockoutGate()
        return _GATE


def set_default_gate(gate: IntegrityLockoutGate | None) -> None:
    global _GATE
    with _GATE_LOCK:
        _GATE = gate


def reset_default_gate() -> None:
    set_default_gate(None)


__all__ = [
    "REASON_ATTESTATION_CHAIN_BROKEN",
    "REASON_BUNDLE_REFRESH_FAILED",
    "REASON_BUNDLE_VERIFICATION_FAILED",
    "REASON_BURN_PALACE",
    "REASON_OPERATOR_INITIATED",
    "BurnDetector",
    "IntegrityLockoutGate",
    "LockoutError",
    "get_default_gate",
    "reset_default_gate",
    "set_default_gate",
    "signal_burn",
]
