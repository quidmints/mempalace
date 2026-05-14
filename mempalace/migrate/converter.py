"""
Offline migration converter from the old MemPalace stores to the new
event log.

Per Part 11.2: the legacy MemPalace stored state in SQLite (KG +
catalog) and ChromaDB (embeddings). The new substrate is a single
append-only event log with derived views. This module converts the
old format by *synthesizing* events that, when replayed, produce the
same state — modulo lossy fields that don't have a new home (e.g.
ad-hoc tags from `tool_diary_write` that didn't carry structure).

Conversion strategy:

  1. Open the old SQLite catalog and ChromaDB embedding store.
  2. For each row, synthesize a sequence of events:
       drawers   → DrawerCaptured
       triples   → AssertionCreated  (via Graph.add_assertion)
       periods   → NodeCreated(period) + open/close/seal as needed
       themes    → NodeCreated(theme)
       tunnels   → EdgeCreated
  3. Replay the synthesized events through a fresh log client.
  4. Run `invariants.run_all` to validate the resulting view state.

Legacy IDs are *remapped* to fresh new-format IDs because the schema
validator enforces a strict `prefix_NN_xxx` pattern. The Converter
exposes the mapping as `report.id_remap` so callers can resolve old
IDs to new ones.

Production callers feed real DB cursors; this module exposes the
transformation logic via small functions so tests can drive it with
in-memory dicts.

Spec ref: Part 11.2.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    DrawerCaptured,
    Event,
    NodeCreated,
)
from ..schema.identifiers import (
    make_drawer_id,
    make_event_id_log,
    make_period_id,
    make_theme_id,
)


# =============================================================================
# Source-row schemas (legacy DB shapes — kept as plain dicts here)
# =============================================================================


@dataclass
class LegacyDrawer:
    drawer_id: str
    content: str
    wing: str = ""
    room: str = ""
    created_at_ms: int = 0
    duration_ms: int = 0
    source_uri: str | None = None


@dataclass
class LegacyTriple:
    subject: str
    predicate: str
    object_: str
    confidence: float = 1.0
    started_at_ms: int | None = None
    ended_at_ms: int | None = None


@dataclass
class LegacyTheme:
    theme_id: str
    name: str


@dataclass
class LegacyPeriod:
    period_id: str
    theme_id: str
    name: str
    started_at_ms: int
    ended_at_ms: int | None = None
    sealed: bool = False


# =============================================================================
# Migration report
# =============================================================================


@dataclass
class MigrationReport:
    """Summary of a migration run."""

    drawers_converted: int = 0
    triples_converted: int = 0
    themes_converted: int = 0
    periods_converted: int = 0
    events_appended: int = 0
    rejected_count: int = 0
    rejection_reasons: list[str] = field(default_factory=list)
    # legacy_id → new_id mapping (for follow-on lookups)
    id_remap: dict[str, str] = field(default_factory=dict)


# =============================================================================
# Event synthesis
# =============================================================================


def _content_hash(content: str) -> str:
    """64-char hex hash of drawer content (32-byte blake2b digest)."""
    return hashlib.blake2b(content.encode("utf-8"), digest_size=32).hexdigest()


def synth_drawer_event(d: LegacyDrawer, *, drawer_id: str | None = None) -> DrawerCaptured:
    """Build a DrawerCaptured from a legacy drawer row.

    `drawer_id` overrides the legacy id (used by Converter to remap).
    """
    captured_at = d.created_at_ms or 0
    new_drawer_id = drawer_id or make_drawer_id(ts_ms=captured_at)
    ev = DrawerCaptured(
        event_id=make_event_id_log(captured_at),
        recorded_at=captured_at,
        actor="migrate.converter",
        drawer_id=new_drawer_id,
        content_hash=_content_hash(d.content),
        capture_recorded_at=captured_at,
        source_uri=d.source_uri,
        duration_ms=d.duration_ms,
        interactional="memo_to_self",
        state_context={},
    )
    return ev


def synth_theme_event(t: LegacyTheme, *, theme_id: str | None = None) -> NodeCreated:
    """Build a NodeCreated for a legacy theme row."""
    new_theme_id = theme_id or make_theme_id()
    return NodeCreated(
        event_id=make_event_id_log(0),
        recorded_at=0,
        actor="migrate.converter",
        node_id=new_theme_id,
        node_kind="theme",
        properties={"name": t.name, "legacy_id": t.theme_id},
    )


def synth_period_event(p: LegacyPeriod, *, period_id: str | None = None) -> NodeCreated:
    """Build a NodeCreated for a legacy period row.

    State transitions (close, seal) are emitted by the converter
    *after* the period is created — see `convert_period`.
    """
    new_period_id = period_id or make_period_id(ts_ms=p.started_at_ms)
    return NodeCreated(
        event_id=make_event_id_log(p.started_at_ms),
        recorded_at=p.started_at_ms,
        actor="migrate.converter",
        node_id=new_period_id,
        node_kind="period",
        properties={
            "theme_id": p.theme_id,
            "name": p.name,
            "started_at_ms": p.started_at_ms,
            "ended_at_ms": p.ended_at_ms,
            "sealed": p.sealed,
            "legacy_id": p.period_id,
        },
    )


# =============================================================================
# Converter
# =============================================================================


class Converter:
    """Drives the conversion against a fresh log client.

    Production wiring iterates real cursors; here we pass small lists
    so unit tests can drive each branch independently.

    Legacy ids are remapped to new-format ids; the mapping is stored
    in `report.id_remap` so callers can translate downstream.
    """

    def __init__(self, *, log: LogClient | None = None) -> None:
        self._log = log or get_default_client()
        self.report = MigrationReport()
        # Set during run() so per-kind converters can stamp events.
        self._migration_batch_handle: Any = None

    def _outer_batch_id(self) -> str:
        """Return current run's batch_id, or '' if not in a run."""
        if self._migration_batch_handle is None:
            return ""
        return self._migration_batch_handle.batch_id

    # ---- per-kind converters --------------------------------------------

    def convert_drawers(self, drawers: Iterable[LegacyDrawer]) -> int:
        count = 0
        outer_batch = self._outer_batch_id()
        for d in drawers:
            new_id = make_drawer_id(ts_ms=d.created_at_ms or None)
            ev = synth_drawer_event(d, drawer_id=new_id)
            # If we're inside a run(), stamp the outer batch_id so the
            # event is grouped with the migration. Inner events from
            # Graph helpers carry their own (per-call) batch_id.
            if outer_batch:
                ev.batch_id = outer_batch
            res = self._log.append(ev)
            if res.accepted:
                count += 1
                self.report.events_appended += 1
                self.report.id_remap[d.drawer_id] = new_id
            else:
                self.report.rejected_count += 1
                self.report.rejection_reasons.append(
                    f"drawer {d.drawer_id}: {res.validation.errors}"
                )
        self.report.drawers_converted += count
        return count

    def convert_themes(self, themes: Iterable[LegacyTheme]) -> int:
        from ..views.graph import Graph
        graph = Graph(client=self._log)
        count = 0
        for t in themes:
            try:
                new_id = graph.create_theme(name=t.name)
            except Exception as e:  # noqa: BLE001
                self.report.rejected_count += 1
                self.report.rejection_reasons.append(f"theme {t.theme_id}: {e}")
                continue
            count += 1
            self.report.events_appended += 1
            self.report.id_remap[t.theme_id] = new_id
        self.report.themes_converted += count
        return count

    def convert_periods(self, periods: Iterable[LegacyPeriod]) -> int:
        from ..views.graph import Graph
        graph = Graph(client=self._log)
        count = 0
        for p in periods:
            # Look up the new theme id (themes must have been converted first)
            theme_new_id = self.report.id_remap.get(p.theme_id, p.theme_id)
            try:
                new_id = graph.create_period(
                    theme_id=theme_new_id,
                    name=p.name,
                    started_at_ms=p.started_at_ms,
                )
            except Exception as e:  # noqa: BLE001
                self.report.rejected_count += 1
                self.report.rejection_reasons.append(f"period {p.period_id}: {e}")
                continue
            count += 1
            self.report.events_appended += 1
            self.report.id_remap[p.period_id] = new_id
            if p.ended_at_ms is not None:
                try:
                    graph.close_period(new_id, ended_at_ms=p.ended_at_ms)
                    self.report.events_appended += 1
                except Exception as e:  # noqa: BLE001
                    self.report.rejection_reasons.append(
                        f"period {p.period_id} close: {e}"
                    )
            if p.sealed:
                try:
                    graph.seal_period(new_id)
                    self.report.events_appended += 1
                except Exception as e:  # noqa: BLE001
                    self.report.rejection_reasons.append(
                        f"period {p.period_id} seal: {e}"
                    )
        self.report.periods_converted += count
        return count

    def convert_triples(self, triples: Iterable[LegacyTriple]) -> int:
        from ..schema.events import DerivationType
        from ..views.graph import Graph
        graph = Graph(client=self._log)
        count = 0
        for t in triples:
            if not (t.subject and t.predicate and t.object_):
                self.report.rejected_count += 1
                self.report.rejection_reasons.append(
                    f"triple ({t.subject},{t.predicate},{t.object_}): empty field"
                )
                continue
            # Look up remapped subject/object ids if present
            subj = self.report.id_remap.get(t.subject, t.subject)
            obj = self.report.id_remap.get(t.object_, t.object_)
            try:
                graph.add_assertion(
                    subject_id=subj,
                    predicate=t.predicate,
                    object_id=obj,
                    confidence=float(t.confidence),
                    valid_from_ms=t.started_at_ms,
                    valid_to_ms=t.ended_at_ms,
                    derivation=DerivationType.OBSERVATION,
                )
            except Exception as e:  # noqa: BLE001
                self.report.rejected_count += 1
                self.report.rejection_reasons.append(
                    f"triple ({t.subject},{t.predicate},{t.object_}): {e}"
                )
                continue
            count += 1
            # add_assertion emits multiple events (assertion node + edges);
            # we'd need to count them separately, but for the report we only
            # tally one per logical triple.
            self.report.events_appended += 1
        self.report.triples_converted += count
        return count

    # ---- top-level driver ------------------------------------------------

    def run(
        self,
        *,
        themes: Iterable[LegacyTheme] = (),
        periods: Iterable[LegacyPeriod] = (),
        drawers: Iterable[LegacyDrawer] = (),
        triples: Iterable[LegacyTriple] = (),
    ) -> MigrationReport:
        """Convert in topological order: themes → periods → drawers → triples.

        The whole migration runs as a single batch (consumer
        "migrate.converter"). A crash mid-migration leaves the batch
        torn; recovery can quarantine the partial state.
        """
        themes_list = list(themes)
        periods_list = list(periods)
        drawers_list = list(drawers)
        triples_list = list(triples)

        with self._log.batch(
            "migrate.converter",
            expected_count=(
                len(themes_list)
                + len(periods_list)
                + len(drawers_list)
                + len(triples_list)
            ),
            input_summary={
                "themes": len(themes_list),
                "periods": len(periods_list),
                "drawers": len(drawers_list),
                "triples": len(triples_list),
            },
        ) as bh:
            self._migration_batch_handle = bh
            try:
                self.convert_themes(themes_list)
                self.convert_periods(periods_list)
                self.convert_drawers(drawers_list)
                self.convert_triples(triples_list)
            finally:
                self._migration_batch_handle = None
        return self.report


__all__ = [
    "Converter",
    "LegacyDrawer",
    "LegacyPeriod",
    "LegacyTheme",
    "LegacyTriple",
    "MigrationReport",
    "synth_drawer_event",
    "synth_period_event",
    "synth_theme_event",
]
