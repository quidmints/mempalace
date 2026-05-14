"""
Chain-event observer for the oracle SDK.

Turns Anchor on-chain events (`FindingSubmitted`,
`SubjectBlindFindingSubmitted`, `SubjectBlindFindingChallenged`)
into typed Python events that the substrate's dataflow bridge can
consume.

# Where this fits

The on-chain Rust side emits events via Anchor's `emit!` macro;
those events show up in the program's transaction logs. An indexer
(e.g., a Helius webhook subscription, a Geyser plugin, or a
custom RPC poller) decodes them and feeds raw payloads to this
module's `observe_*` functions, which produce typed Python
`mempalace.schema.events` instances.

This module is the *boundary* between on-chain wire format and
the substrate's typed event model. The chain client itself isn't
shipped here — production wires whatever indexer it uses to call
these functions.

# Why three observers, not one

Each observer takes a different on-chain event shape, but all
produce the same Python event taxonomy. Keeping them separate
means each can be unit-tested independently and the wiring stays
explicit. A future fourth observer (e.g., for `ResolverAssigned`)
adds a new function alongside; nothing else needs to change.

Spec ref: ORACLE_THREAT_MODEL.md §4 (the four event kinds), §8
(relationship to the substrate's existing event taxonomy).
"""

from __future__ import annotations

import logging
from typing import Any

from ..log.client import LogClient
from ..schema.events import (
    SubjectBlindDecloakOpened,
    SubjectBlindFindingChallenged,
    SwitchboardFindingSubmitted,
)

logger = logging.getLogger(__name__)


def observe_finding_submitted(
    payload: dict[str, Any],
    *,
    log_client: LogClient,
    on_chain_signature: str = "",
) -> str:
    """Convert a chain `FindingSubmitted` event into the typed
    Python event and append to the log.

    Args:
      payload: Decoded event fields. Expected keys match the Rust
        struct (`assignment`, `market_id`, `resolver`,
        `resolver_index`, `value_i128_micros`, `in_agreement`,
        `submitted_count`, `agreement_count`, `just_resolved`,
        `consensus_value_i128_micros`).
      log_client: Where to append the event.
      on_chain_signature: The Solana tx signature that produced
        this event. Audit reference.

    Returns:
      The event_id of the appended event.
    """
    evt = SwitchboardFindingSubmitted(
        assignment_id=str(payload.get("assignment", "")),
        market_id=str(payload.get("market_id", "")),
        resolver_pubkey=str(payload.get("resolver", "")),
        resolver_index=int(payload.get("resolver_index", 0)),
        value_i128_micros=int(payload.get("value_i128_micros", 0)),
        confidence_interval_micros=int(
            payload.get("confidence_interval_micros", 0),
        ),
        num_samples=int(payload.get("num_samples", 0)),
        in_agreement=bool(payload.get("in_agreement", False)),
        submitted_count=int(payload.get("submitted_count", 0)),
        agreement_count=int(payload.get("agreement_count", 0)),
        just_resolved=bool(payload.get("just_resolved", False)),
        consensus_value_i128_micros=int(
            payload.get("consensus_value_i128_micros", 0),
        ),
        on_chain_signature=on_chain_signature,
    )
    return log_client.append(evt)


def observe_subject_blind_submitted(
    payload: dict[str, Any],
    *,
    log_client: LogClient,
) -> str:
    """Convert a chain `SubjectBlindFindingSubmitted` event into
    the typed Python `SubjectBlindDecloakOpened` event.

    Note the name change: the chain calls it "submitted"; we call
    the Python event "decloak_opened" because what matters
    substrate-side is that the challenge window is now open.
    """
    evt = SubjectBlindDecloakOpened(
        finding_pda=str(payload.get("finding", "")),
        market_id=str(payload.get("market_id", "")),
        resolver_pubkey=str(payload.get("resolver", "")),
        derivation_seed_hash=_hex(payload.get("derivation_seed_hash")),
        slice_hash=_hex(payload.get("slice_hash")),
        finding_hash=_hex(payload.get("finding_hash")),
        submitted_at_slot=int(payload.get("submitted_at_slot", 0)),
        challenge_window_ends_at_slot=int(
            payload.get("challenge_window_ends_at_slot", 0),
        ),
    )
    return log_client.append(evt)


def observe_subject_blind_challenged(
    payload: dict[str, Any],
    *,
    log_client: LogClient,
    integrity_lockout_triggered: bool = False,
) -> str:
    """Convert a chain `SubjectBlindFindingChallenged` event.

    `integrity_lockout_triggered` is False when the observer first
    sees the challenge event; an off-chain finalizer that triggers
    the lockout should re-emit the same event with
    `integrity_lockout_triggered=True` to close the loop. (The
    audit log carries both states; consumers can dedupe on
    `finding_pda` and pick the one with the trigger.)
    """
    evt = SubjectBlindFindingChallenged(
        finding_pda=str(payload.get("finding", "")),
        market_id=str(payload.get("market_id", "")),
        resolver_pubkey=str(payload.get("resolver", "")),
        challenger_pubkey=str(payload.get("challenger", "")),
        challenged_at_slot=int(payload.get("challenged_at_slot", 0)),
        recomputed_finding_hash=_hex(
            payload.get("recomputed_finding_hash"),
        ),
        integrity_lockout_triggered=integrity_lockout_triggered,
    )
    return log_client.append(evt)


def _hex(v: Any) -> str:
    """Coerce a value to a hex string. Accepts bytes / bytearray /
    list of ints / pre-encoded string."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, list):
        try:
            return bytes(v).hex()
        except (TypeError, ValueError):
            return ""
    return str(v)


__all__ = [
    "observe_finding_submitted",
    "observe_subject_blind_challenged",
    "observe_subject_blind_submitted",
]
