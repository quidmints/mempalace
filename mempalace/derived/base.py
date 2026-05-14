"""
DerivedRepresentation base class.

Per Part 8: master views are consumer-agnostic; derived representations
are consumer-optimized projections built from the master log. Each
representation:

  - subscribes to a set of event kinds
  - holds an in-memory (or on-disk) state
  - updates state on each event via apply()
  - emits `view_offset_advanced` to make its progress visible
  - exposes a typed query API to consumers

The pattern is the same as the master views — a SubscriberRegistry-driven
tick loop — but each derived representation carries its own concerns
(e.g., transition_cache caches pairwise drawer coherences;
realtime_index pre-warms hot queries).

Spec ref: Part 8.1, Part 2.2.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..log.subscriber import SubscriberRegistry, get_default_registry


@dataclass
class DerivedRepStats:
    """Per-representation runtime stats."""

    name: str
    events_consumed: int = 0
    last_offset: int = 0
    last_tick_at_ms: int = 0
    avg_tick_elapsed_us: float = 0.0


class DerivedRepresentation:
    """Base class for a derived representation.

    Subclasses:
      - declare `name` (consumer_id), `subscribed_kinds`
      - implement `apply(offset, kind, payload)` to update state
      - optionally implement `on_compaction()` for log-compaction events

    Subscription is registered against the default registry on `start()`.
    """

    name: str = "derived_unnamed"
    subscribed_kinds: tuple[str, ...] = ()  # empty = all kinds

    def __init__(
        self,
        *,
        log_client: LogClient | None = None,
        registry: SubscriberRegistry | None = None,
        max_batch_size: int = 1024,
    ) -> None:
        self._client = log_client or get_default_client()
        self._registry = registry or get_default_registry()
        self._max_batch_size = max_batch_size
        self._stats = DerivedRepStats(name=self.name)
        self._lock = threading.Lock()
        self._started = False
        # Subclasses can hold their own state in instance attrs.

    # ---- subclass API -------------------------------------------------------

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        """Update state for one event. Override in subclass."""
        raise NotImplementedError

    def reset_state(self) -> None:
        """Clear all derived state. Override in subclass if needed."""
        return

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Register subscription. Idempotent."""
        if self._started:
            return
        if self._registry.get(self.name) is not None:
            self._started = True
            return
        self._registry.register(
            consumer_id=self.name,
            kinds=list(self.subscribed_kinds),
            handler=self._handle,
            max_batch_size=self._max_batch_size,
        )
        self._started = True

    def stop(self) -> None:
        """Unregister and reset state."""
        # SubscriberRegistry has no unregister in the public API yet; in
        # practice we rely on registry resets between tests.
        self._started = False
        self.reset_state()

    def tick(self) -> int:
        """Advance our subscription one batch. Returns events processed."""
        if not self._started:
            self.start()
        return self._registry.tick_one(self.name)

    # ---- internal -----------------------------------------------------------

    def _handle(self, offset: int, kind: str, payload: dict) -> None:
        t0 = time.monotonic()
        self.apply(offset, kind, payload)
        elapsed_us = int((time.monotonic() - t0) * 1_000_000)
        with self._lock:
            n = self._stats.events_consumed + 1
            # Running average update
            self._stats.avg_tick_elapsed_us = (
                (self._stats.avg_tick_elapsed_us * self._stats.events_consumed + elapsed_us) / n
            )
            self._stats.events_consumed = n
            self._stats.last_offset = offset
            self._stats.last_tick_at_ms = int(time.time() * 1000)

    # ---- introspection ------------------------------------------------------

    def get_stats(self) -> DerivedRepStats:
        with self._lock:
            return DerivedRepStats(
                name=self._stats.name,
                events_consumed=self._stats.events_consumed,
                last_offset=self._stats.last_offset,
                last_tick_at_ms=self._stats.last_tick_at_ms,
                avg_tick_elapsed_us=self._stats.avg_tick_elapsed_us,
            )


__all__ = ["DerivedRepStats", "DerivedRepresentation"]
