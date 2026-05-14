"""
Per-(miner-output, consumer-type) feedback ledger.

Per Part 10.5: the miner doesn't optimize a single loss. It optimizes a
weighted combination of per-consumer losses:

  - Retrieval-utility loss     (weakest, available immediately)
  - User-confirmation loss     (medium)
  - Downstream-feedback loss   (strongest, accumulating)

Different artifact types feed back at different cadences. The ledger
preserves the granularity so the miner can train against a weighted
combination, with weights shifting as stronger signals accumulate.

This module owns:

  - FeedbackKind enum
  - FeedbackEvent dataclass
  - FeedbackLedger: store of feedback events keyed by (proposal_id, consumer_type)
  - aggregate_loss(): convenience to summarize per-consumer feedback
  - LOSS_WEIGHTS: the default per-consumer-class weights

Spec ref: Part 10.5.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum


# =============================================================================
# Feedback taxonomy
# =============================================================================


class FeedbackKind(str, Enum):
    """The kinds of feedback a miner output can receive."""

    RETRIEVAL_USED = "retrieval_used"
    RETRIEVAL_IGNORED = "retrieval_ignored"
    USER_CONFIRMED = "user_confirmed"
    USER_EDITED = "user_edited"
    USER_REJECTED = "user_rejected"
    MONTAGE_KEPT = "montage_kept"
    MONTAGE_DROPPED = "montage_dropped"
    DRAFT_COMPLETED = "draft_completed"
    DRAFT_ABANDONED = "draft_abandoned"
    MATCH_OUTCOME_POSITIVE = "match_outcome_positive"
    MATCH_OUTCOME_NEGATIVE = "match_outcome_negative"


# Map each FeedbackKind to a coarse consumer type
FEEDBACK_CONSUMER: dict[FeedbackKind, str] = {
    FeedbackKind.RETRIEVAL_USED: "retrieval",
    FeedbackKind.RETRIEVAL_IGNORED: "retrieval",
    FeedbackKind.USER_CONFIRMED: "user_confirmation",
    FeedbackKind.USER_EDITED: "user_confirmation",
    FeedbackKind.USER_REJECTED: "user_confirmation",
    FeedbackKind.MONTAGE_KEPT: "downstream",
    FeedbackKind.MONTAGE_DROPPED: "downstream",
    FeedbackKind.DRAFT_COMPLETED: "downstream",
    FeedbackKind.DRAFT_ABANDONED: "downstream",
    FeedbackKind.MATCH_OUTCOME_POSITIVE: "downstream",
    FeedbackKind.MATCH_OUTCOME_NEGATIVE: "downstream",
}

# Map each FeedbackKind to a signed reward in [-1, 1]
FEEDBACK_VALENCE: dict[FeedbackKind, float] = {
    FeedbackKind.RETRIEVAL_USED: +0.3,
    FeedbackKind.RETRIEVAL_IGNORED: -0.1,
    FeedbackKind.USER_CONFIRMED: +1.0,
    FeedbackKind.USER_EDITED: +0.5,
    FeedbackKind.USER_REJECTED: -1.0,
    FeedbackKind.MONTAGE_KEPT: +0.6,
    FeedbackKind.MONTAGE_DROPPED: -0.6,
    FeedbackKind.DRAFT_COMPLETED: +0.7,
    FeedbackKind.DRAFT_ABANDONED: -0.5,
    FeedbackKind.MATCH_OUTCOME_POSITIVE: +1.0,
    FeedbackKind.MATCH_OUTCOME_NEGATIVE: -1.0,
}

# Per Part 10.5: weight on each consumer class. Shifts as stronger
# signals accumulate.
LOSS_WEIGHTS: dict[str, float] = {
    "retrieval": 0.2,
    "user_confirmation": 0.3,
    "downstream": 0.5,
}


# =============================================================================
# FeedbackEvent + Ledger
# =============================================================================


@dataclass
class FeedbackEvent:
    """A single piece of feedback about a miner proposal."""

    proposal_id: str
    feedback_kind: FeedbackKind
    consumer_type: str
    valence: float
    recorded_at_ms: int
    artifact_id: str = ""                     # the downstream artifact, if any
    interpretation_versions: dict[str, str] = field(default_factory=dict)


class FeedbackLedger:
    """Per-(proposal, consumer) feedback ledger."""

    def __init__(self) -> None:
        # proposal_id -> list of events
        self._by_proposal: dict[str, list[FeedbackEvent]] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        proposal_id: str,
        feedback_kind: FeedbackKind,
        artifact_id: str = "",
        interpretation_versions: dict[str, str] | None = None,
        now_ms: int | None = None,
    ) -> FeedbackEvent:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        consumer = FEEDBACK_CONSUMER.get(feedback_kind, "downstream")
        valence = FEEDBACK_VALENCE.get(feedback_kind, 0.0)
        event = FeedbackEvent(
            proposal_id=proposal_id,
            feedback_kind=feedback_kind,
            consumer_type=consumer,
            valence=valence,
            recorded_at_ms=now_ms,
            artifact_id=artifact_id,
            interpretation_versions=dict(interpretation_versions or {}),
        )
        with self._lock:
            self._by_proposal.setdefault(proposal_id, []).append(event)
        return event

    def for_proposal(self, proposal_id: str) -> list[FeedbackEvent]:
        with self._lock:
            return list(self._by_proposal.get(proposal_id, []))

    def all(self) -> list[FeedbackEvent]:
        with self._lock:
            out: list[FeedbackEvent] = []
            for events in self._by_proposal.values():
                out.extend(events)
            return out

    def size(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._by_proposal.values())


# =============================================================================
# Loss aggregation
# =============================================================================


@dataclass
class AggregatedLoss:
    """The miner's training signal for one proposal."""

    proposal_id: str
    overall: float
    by_consumer: dict[str, float] = field(default_factory=dict)
    sample_count_by_consumer: dict[str, int] = field(default_factory=dict)


def aggregate_loss(
    proposal_id: str,
    events: Iterable[FeedbackEvent],
    *,
    weights: dict[str, float] | None = None,
) -> AggregatedLoss:
    """Aggregate a sequence of feedback events into a single training signal.

    The result is a weighted average of per-consumer mean valence, using
    LOSS_WEIGHTS by default.
    """
    weights = weights or LOSS_WEIGHTS
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for ev in events:
        sums[ev.consumer_type] = sums.get(ev.consumer_type, 0.0) + ev.valence
        counts[ev.consumer_type] = counts.get(ev.consumer_type, 0) + 1
    by_consumer: dict[str, float] = {}
    for consumer, total in sums.items():
        n = counts.get(consumer, 0)
        if n > 0:
            by_consumer[consumer] = total / n
    if not by_consumer:
        return AggregatedLoss(proposal_id=proposal_id, overall=0.0)
    weighted_sum = 0.0
    weight_total = 0.0
    for consumer, mean in by_consumer.items():
        w = weights.get(consumer, 0.0)
        weighted_sum += w * mean
        weight_total += w
    overall = weighted_sum / weight_total if weight_total > 0 else 0.0
    return AggregatedLoss(
        proposal_id=proposal_id,
        overall=overall,
        by_consumer=by_consumer,
        sample_count_by_consumer=dict(counts),
    )


# =============================================================================
# Module-level singleton
# =============================================================================


_LEDGER: FeedbackLedger | None = None
_LEDGER_LOCK = threading.Lock()


def get_feedback_ledger() -> FeedbackLedger:
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = FeedbackLedger()
        return _LEDGER


def set_feedback_ledger(ledger: FeedbackLedger) -> None:
    global _LEDGER
    with _LEDGER_LOCK:
        _LEDGER = ledger


__all__ = [
    "AggregatedLoss",
    "FEEDBACK_CONSUMER",
    "FEEDBACK_VALENCE",
    "FeedbackEvent",
    "FeedbackKind",
    "FeedbackLedger",
    "LOSS_WEIGHTS",
    "aggregate_loss",
    "get_feedback_ledger",
    "set_feedback_ledger",
]
