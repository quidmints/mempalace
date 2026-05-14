"""
Resolution feedback emission.

Per R3 §2.2 (and Part 11): emits `feedback_recorded` events keyed to
the interpretation versions active at resolution time. This is the
**root** of the credit-assignment chain — downstream consumers (the
miner, the embedder, the canonicalizer) all walk back from these
events to the interpretation versions that contributed.

The lifecycle:

  resolution_completes
    → emit feedback_recorded with versions snapshot
    → consumers read by interpretation_version
    → consumers update their per-version weights / training signals

Spec ref: R3 §2.2, Part 11.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import FeedbackRecorded
from ..schema.identifiers import make_event_id_log


# =============================================================================
# Feedback record
# =============================================================================


@dataclass
class ResolutionFeedback:
    """A single feedback record about a market resolution."""

    artifact_id: str                              # market_id or sub_artifact id
    consumer: str                                 # "matching" | "montage" | "resolve" | etc.
    feedback_kind: str                            # "kept" | "discarded" | "match_outcome" | ...
    feedback_value: Any = None
    interpretation_versions: dict[str, str] = field(default_factory=dict)
    recorded_at_ms: int = 0


# =============================================================================
# Emitter
# =============================================================================


class ResolutionFeedbackEmitter:
    """Emits `feedback_recorded` events for downstream credit assignment."""

    def __init__(self, *, log: LogClient | None = None) -> None:
        self._log = log

    def emit(self, fb: ResolutionFeedback) -> None:
        """Emit a feedback_recorded event."""
        log = self._log or get_default_client()
        now = fb.recorded_at_ms or int(time.time() * 1000)
        log.append(FeedbackRecorded(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="resolve.feedback",
            consumer=fb.consumer,
            artifact_id=fb.artifact_id,
            feedback_kind=fb.feedback_kind,
            feedback_value=fb.feedback_value,
            interpretation_versions=dict(fb.interpretation_versions),
        ))

    def emit_batch(self, fbs: Iterable[ResolutionFeedback]) -> int:
        count = 0
        for fb in fbs:
            self.emit(fb)
            count += 1
        return count


# =============================================================================
# Helpers
# =============================================================================


def snapshot_interpretation_versions(
    *,
    embed_version: str = "",
    canon_version: str = "",
    miner_version: str = "",
    classifier_version: str = "",
    ranker_version: str = "",
) -> dict[str, str]:
    """Build a versions dict to attach to a feedback record.

    R3: every interpretation that produced a credited artifact is
    versioned; consumers route feedback through these versions.
    """
    out: dict[str, str] = {}
    if embed_version:
        out["embed"] = embed_version
    if canon_version:
        out["canonicalizer"] = canon_version
    if miner_version:
        out["miner"] = miner_version
    if classifier_version:
        out["classifier"] = classifier_version
    if ranker_version:
        out["ranker"] = ranker_version
    return out


__all__ = [
    "ResolutionFeedback",
    "ResolutionFeedbackEmitter",
    "snapshot_interpretation_versions",
]
