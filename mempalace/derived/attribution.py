"""
Match-resolution attribution.

Per Part 8.2: when a match between palaces produces an artifact (an
"insight," a contradiction-finding, a shared assertion), we want to be
able to trace that artifact back to its constituent drawers and
interpretation versions across both palaces.

This is append-only: an attribution record is created once per match
artifact and never mutated. If the underlying drawers later change,
that's recorded as a separate amendment in the audit chain — but the
original attribution stands.

Spec ref: Part 8.2.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .base import DerivedRepresentation


@dataclass(frozen=True)
class AttributionRecord:
    """One immutable attribution record."""

    artifact_id: str           # the produced artifact (e.g., a finding ID)
    artifact_kind: str         # "finding", "shared_assertion", etc.
    match_id: str              # the match session this came from
    drawer_ids: tuple[str, ...]               # source drawers
    interpretation_event_ids: tuple[str, ...]  # specific interpretation versions
    palace_pubkeys: tuple[str, ...]            # which palaces contributed
    derived_at_ms: int
    notes: str = ""


# =============================================================================
# Attribution view
# =============================================================================


class AttributionStore(DerivedRepresentation):
    """Append-only store of match-resolution attributions."""

    name = "derived.attribution"
    subscribed_kinds = ("feedback_recorded", "finding_emitted")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._records: list[AttributionRecord] = []
        self._by_artifact: dict[str, AttributionRecord] = {}
        self._by_drawer: dict[str, list[AttributionRecord]] = {}
        self._cache_lock = threading.Lock()

    def reset_state(self) -> None:
        with self._cache_lock:
            self._records.clear()
            self._by_artifact.clear()
            self._by_drawer.clear()

    # ---- subscriber: extract attribution info from finding_emitted ---------

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        if kind != "finding_emitted":
            return
        artifact_id = payload.get("finding_id") or payload.get("artifact_id")
        match_id = payload.get("match_id", "")
        drawer_ids = tuple(payload.get("source_drawer_ids", []) or [])
        interp_ids = tuple(payload.get("interpretation_event_ids", []) or [])
        pubkeys = tuple(payload.get("palace_pubkeys", []) or [])
        notes = payload.get("notes", "")
        if not artifact_id:
            return

        rec = AttributionRecord(
            artifact_id=artifact_id,
            artifact_kind=payload.get("kind", "finding"),
            match_id=match_id,
            drawer_ids=drawer_ids,
            interpretation_event_ids=interp_ids,
            palace_pubkeys=pubkeys,
            derived_at_ms=payload.get("recorded_at", int(time.time() * 1000)),
            notes=notes,
        )
        self.append(rec)

    # ---- public append API (also for tests / direct calls) -----------------

    def append(self, rec: AttributionRecord) -> None:
        with self._cache_lock:
            self._records.append(rec)
            self._by_artifact[rec.artifact_id] = rec
            for did in rec.drawer_ids:
                self._by_drawer.setdefault(did, []).append(rec)

    # ---- query -------------------------------------------------------------

    def by_artifact(self, artifact_id: str) -> AttributionRecord | None:
        with self._cache_lock:
            return self._by_artifact.get(artifact_id)

    def by_drawer(self, drawer_id: str) -> list[AttributionRecord]:
        with self._cache_lock:
            return list(self._by_drawer.get(drawer_id, []))

    def all_records(self) -> list[AttributionRecord]:
        with self._cache_lock:
            return list(self._records)

    def count(self) -> int:
        with self._cache_lock:
            return len(self._records)


__all__ = ["AttributionRecord", "AttributionStore"]
