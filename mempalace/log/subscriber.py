"""
Subscription helper for derived representations.

A consumer (signature store, transition cache, FOYER renderer, miner pass,
etc.) registers as a subscriber, declares which event kinds it cares about,
and pulls forward through the log at its own pace. Each subscriber has its
own offset; the `subscriber.tick()` method advances the consumer's view.

This pattern is what gives us:
  - Many readers, no contention (each consumer has its own offset).
  - Snapshot consistency (a consumer at offset K sees a coherent view).
  - Backpressure detection (consumers falling behind log head are visible).

Spec ref: Part 2.2, Part 10.2
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .client import LogClient, get_default_client


# =============================================================================
# Subscriber protocol
# =============================================================================

class EventHandler(Protocol):
    """Signature for a consumer's per-event handler.

    Receives (offset, kind, payload). Should be deterministic and idempotent
    when possible — replay during recovery may invoke handlers more than once.
    """
    def __call__(self, offset: int, kind: str, payload: dict) -> None: ...


@dataclass
class Subscription:
    """A single consumer's subscription state.

    Attributes:
        consumer_id: opaque ID for this consumer (e.g., "miner_class1",
            "signature_store"). Used in `view_offset_advanced` events.
        kinds: set of event kinds the consumer subscribes to. Empty set means
            "all kinds" (used by the audit log subscriber).
        handler: the function to call for matching events.
        current_offset: the highest log offset this consumer has processed.
        max_batch_size: tick() processes at most this many events per call.
    """
    consumer_id: str
    kinds: frozenset[str]
    handler: EventHandler
    current_offset: int = 0
    max_batch_size: int = 256


# =============================================================================
# Subscriber registry
# =============================================================================

class SubscriberRegistry:
    """Registry of subscribers + a tick() method that advances them all.

    The registry doesn't run a background thread by default; callers (the
    multiplexer, in production) drive ticking. Tests can call tick() directly.
    """

    def __init__(self, log_client: LogClient | None = None) -> None:
        self._client = log_client or get_default_client()
        self._subscriptions: dict[str, Subscription] = {}
        self._lock = threading.Lock()

    def register(
        self,
        consumer_id: str,
        kinds: Iterable[str],
        handler: EventHandler,
        max_batch_size: int = 256,
    ) -> Subscription:
        """Register a new subscription.

        If consumer_id already exists, the prior subscription is replaced.
        The new subscription starts at offset 0 (replays from log start).
        """
        with self._lock:
            sub = Subscription(
                consumer_id=consumer_id,
                kinds=frozenset(kinds),
                handler=handler,
                current_offset=0,
                max_batch_size=max_batch_size,
            )
            self._subscriptions[consumer_id] = sub
            return sub

    def unregister(self, consumer_id: str) -> None:
        with self._lock:
            self._subscriptions.pop(consumer_id, None)

    def get(self, consumer_id: str) -> Subscription | None:
        with self._lock:
            return self._subscriptions.get(consumer_id)

    def all(self) -> list[Subscription]:
        with self._lock:
            return list(self._subscriptions.values())

    def tick_one(self, consumer_id: str) -> int:
        """Advance one consumer's view by up to max_batch_size events.

        Returns the number of events processed.

        Note: does not emit `view_offset_advanced` itself — that would
        require LogClient access for the consumer's downstream events,
        which creates a circular pattern in tests. Callers (the
        multiplexer) emit these events explicitly when needed.
        """
        with self._lock:
            sub = self._subscriptions.get(consumer_id)
            if sub is None:
                return 0
            start = sub.current_offset + 1
            end = min(
                self._client.current_offset() + 1,
                start + sub.max_batch_size,
            )
            if start >= end:
                return 0

        # Read outside the lock to avoid blocking other registrations.
        processed = 0
        for offset, kind, payload in self._client.read_range(start, end):
            if not sub.kinds or kind in sub.kinds:
                try:
                    sub.handler(offset, kind, payload)
                except Exception:
                    # Don't let one consumer's failure block others.
                    # Production: log the exception via the audit log.
                    # For now we swallow and keep advancing; consumers
                    # are responsible for their own error handling.
                    pass
            processed += 1
            sub.current_offset = offset

        return processed

    def tick_all(self) -> dict[str, int]:
        """Advance every registered consumer.

        Returns map of consumer_id → events processed in this tick.
        """
        with self._lock:
            consumer_ids = list(self._subscriptions.keys())
        return {cid: self.tick_one(cid) for cid in consumer_ids}

    # =========================================================================
    # Backpressure detection
    # =========================================================================

    def lagging_consumers(self, threshold: int = 1000) -> list[str]:
        """Return consumer_ids whose offset trails log head by at least threshold.

        Used by the multiplexer (Part 10.2) to detect consumers falling
        behind. Below threshold, fine; above, the multiplexer can decide
        to allocate more compute, drop the consumer, or alert the operator.
        """
        with self._lock:
            head = self._client.current_offset()
            return [
                sub.consumer_id
                for sub in self._subscriptions.values()
                if (head - sub.current_offset) >= threshold
            ]


# =============================================================================
# Module-level singleton
# =============================================================================

_default_registry: SubscriberRegistry | None = None
_default_lock = threading.Lock()


def get_default_registry() -> SubscriberRegistry:
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = SubscriberRegistry()
        return _default_registry


def set_default_registry(registry: SubscriberRegistry) -> None:
    global _default_registry
    with _default_lock:
        _default_registry = registry
