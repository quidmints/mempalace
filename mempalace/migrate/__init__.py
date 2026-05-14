"""
mempalace.migrate — offline migration from legacy stores.

Per Part 11.2: one-shot conversion of the old SQLite + ChromaDB
MemPalace into the new event-log substrate, with post-migration
invariant validation.

Submodules:

  converter   — synthesizes events from legacy DB rows
  invariants  — post-migration invariant suite

Spec ref: Part 11.2.
"""

from .converter import (
    Converter,
    LegacyDrawer,
    LegacyPeriod,
    LegacyTheme,
    LegacyTriple,
    MigrationReport,
    synth_drawer_event,
    synth_period_event,
    synth_theme_event,
)
from .invariants import (
    InvariantReport,
    Violation,
    run_all,
)

__all__ = [
    "Converter",
    "InvariantReport",
    "LegacyDrawer",
    "LegacyPeriod",
    "LegacyTheme",
    "LegacyTriple",
    "MigrationReport",
    "Violation",
    "run_all",
    "synth_drawer_event",
    "synth_period_event",
    "synth_theme_event",
]
