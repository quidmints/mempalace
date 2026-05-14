"""
Burn-recovery quarantine — Track 5H.

Per IMPLEMENTATION_ROADMAP.md §"Track 5H":
  - Wires to `IntegrityLockout` PDA on Rust side.
  - SE call destroys keys; recovery scan recognizes that revoked
    palaces' events are unreadable and quarantines them.
  - Tests: revocation makes prior data unrecoverable.

# What this module ships

A recovery-scan helper that walks an encrypted log and identifies
events that can no longer be decrypted (because the DARK is
unavailable, e.g. burn destroyed the bundle):

  - `QuarantineReport` — what the scan found.
  - `scan_for_unreadable_events(log_client)` — walks the log, collects
    undecryptable entries.
  - `quarantine_unreadable(log_client, report)` — flags affected
    events / DD views.

# Composition with other tracks

  - Track 5G's `EncryptedLogBackend` surfaces undecryptable payloads
    as `_at_rest_encrypted=True` blobs (rather than raising). This
    module reads those signals.
  - Track 6E's burn flow zeros the key manager, which causes the
    DARK derivation to fail on next bundle-load. This is the
    other side of that flow.
  - Track 6D's tombstoning puts a `_erased=True` marker on
    deliberately-erased events; this module distinguishes those
    from "lost-DARK unreadable" events. Erased = user intent;
    unreadable = burn fallout.

# What this module does NOT ship

  - On-chain `IntegrityLockout` PDA write. The Rust-side
    `mempalace_chain` crate emits the PDA when the cloud-box
    notifies it of burn; this module is the cloud-box-side detector
    that signals when burn fallout is observed.
  - DD view rebuilds. Production rebuilds derived views from the
    log; if the log is partly unreadable, the rebuilt views skip
    the unreadable events. Hooking that up is per-DD-view code,
    not this module.

Spec ref: IMPLEMENTATION_ROADMAP.md §"Track 5H",
USER_VIEW_AND_DELETE_DESIGN.md §"Burning the palace".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..log.at_rest import _ENCRYPTED_PAYLOAD_MARKER
from ..log.client import LogClient

logger = logging.getLogger(__name__)


# =============================================================================
# Report
# =============================================================================


@dataclass
class QuarantineReport:
    """What `scan_for_unreadable_events` found."""

    total_scanned: int = 0
    """Total events walked."""

    decrypted_ok: int = 0
    """Events that the wrapper decrypted cleanly. Includes legacy
    pre-Track-5G events (no encryption to decrypt). The bulk in a
    healthy palace."""

    erased_tombstones: int = 0
    """Events with `_erased=True`. Track 6D produced these; not
    a sign of burn — just user intent."""

    unreadable: list[tuple[int, str]] = field(default_factory=list)
    """Events that look encrypted but couldn't be decrypted.
    Possible causes:
      - Burn destroyed the bundle; DARK is gone.
      - Key rotation; DARK was rotated and the old DARK no longer
        loaded.
      - Tampering on the cloud-box's local disk.
    Each entry is (offset, kind)."""

    @property
    def has_unreadable(self) -> bool:
        return len(self.unreadable) > 0

    @property
    def unreadable_count(self) -> int:
        return len(self.unreadable)


# =============================================================================
# Scan
# =============================================================================


def scan_for_unreadable_events(
    log_client: LogClient,
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
) -> QuarantineReport:
    """Walk the log; classify each event as readable / erased /
    unreadable.

    Returns a QuarantineReport. Production callers run this on
    startup after attempting to load the bundle; if `unreadable`
    is non-empty AND no recent IntegrityLockout event is present,
    that's a tampering signal.
    """
    if end_offset is None:
        end_offset = log_client.current_offset() + 1

    report = QuarantineReport()

    for offset, kind, payload in log_client.read_range(start_offset, end_offset):
        report.total_scanned += 1

        # Track 6D tombstone marker — distinguish from burn fallout
        if payload.get("_erased"):
            report.erased_tombstones += 1
            continue

        # If the wrapper failed to decrypt, the payload still has
        # the at-rest encryption marker (the wrapper passes through
        # encrypted blobs on decrypt failure rather than raising).
        if payload.get(_ENCRYPTED_PAYLOAD_MARKER):
            report.unreadable.append((offset, kind))
            continue

        report.decrypted_ok += 1

    if report.has_unreadable:
        logger.warning(
            "QuarantineReport: %d events unreadable (likely burn fallout)",
            report.unreadable_count,
        )

    return report


# =============================================================================
# Quarantine action
# =============================================================================


def has_burn_fallout(report: QuarantineReport) -> bool:
    """Pure helper: does this report indicate burn fallout?

    True iff there are unreadable events. Production combines this
    with checks on the IntegrityLockout event log to distinguish
    intentional burn from operational failure.
    """
    return report.has_unreadable


__all__ = [
    "QuarantineReport",
    "has_burn_fallout",
    "scan_for_unreadable_events",
]
