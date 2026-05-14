"""
Derived-representation registry.

Holds the set of active derived representations. The registry is what
the multiplexer (Part 10) ticks: every cycle it rotates through the
registered reps and ticks each by some budget.

Each rep declares its name, its owner (which subsystem owns it), its
invalidation policy (when does state become stale?), and exposes its
DerivedRepresentation instance.

Spec ref: Part 8.1, Part 10.2.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from .base import DerivedRepresentation


class InvalidationPolicy(str, Enum):
    """When does a rep's state become stale?

    EVENT_DRIVEN  : state updates only on subscribed events.
    TIME_DECAY    : state has time-based fading; needs periodic recompute.
    HYBRID        : both event-driven + time-decay.
    """

    EVENT_DRIVEN = "event_driven"
    TIME_DECAY = "time_decay"
    HYBRID = "hybrid"


@dataclass
class DerivedRepEntry:
    """One entry in the registry."""

    name: str
    owner: str
    invalidation: InvalidationPolicy
    instance: DerivedRepresentation
    description: str = ""


class DerivedRegistry:
    """Holds all DerivedRepresentations available in the palace."""

    def __init__(self) -> None:
        self._entries: dict[str, DerivedRepEntry] = {}
        self._lock = threading.Lock()

    def register(
        self,
        rep: DerivedRepresentation,
        *,
        owner: str,
        invalidation: InvalidationPolicy = InvalidationPolicy.EVENT_DRIVEN,
        description: str = "",
        auto_start: bool = True,
    ) -> None:
        entry = DerivedRepEntry(
            name=rep.name,
            owner=owner,
            invalidation=invalidation,
            instance=rep,
            description=description,
        )
        with self._lock:
            self._entries[rep.name] = entry
        if auto_start:
            rep.start()

    def get(self, name: str) -> DerivedRepEntry | None:
        with self._lock:
            return self._entries.get(name)

    def list_entries(self) -> list[DerivedRepEntry]:
        with self._lock:
            return list(self._entries.values())

    def tick_all(self) -> dict[str, int]:
        """Tick each registered representation once. Returns events
        processed per rep."""
        with self._lock:
            entries = list(self._entries.values())
        results: dict[str, int] = {}
        for entry in entries:
            try:
                results[entry.name] = entry.instance.tick()
            except Exception as e:
                results[entry.name] = -1  # signal error
        return results

    def reset(self) -> None:
        with self._lock:
            for entry in self._entries.values():
                try:
                    entry.instance.stop()
                except Exception:
                    pass
            self._entries.clear()


# =============================================================================
# Module-level singleton
# =============================================================================


_REGISTRY = DerivedRegistry()


def get_derived_registry() -> DerivedRegistry:
    return _REGISTRY


__all__ = [
    "DerivedRegistry",
    "DerivedRepEntry",
    "InvalidationPolicy",
    "get_derived_registry",
]
