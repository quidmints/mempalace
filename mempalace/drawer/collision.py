"""
Content-hash collision detection.

Architectural commitment from the spec: identical content is a *glitch*, not
a deduplication opportunity. When two drawers share the same content_hash,
that's a meaningful signal — it could be a duplicate capture from a flaky
client, a deliberately repeated phrase, or a clue that the user is in a
ruminative loop. Either way, both drawers are kept; a `DrawerHashCollision`
event records the fact for the recurrence-cluster miner to interpret.

This module:
  - Maintains an in-memory hash → drawer_id index (a derived view).
  - Subscribes to `drawer_captured` events to keep the index live.
  - Provides `check(content_hash)` for capture-time queries.
  - Provides `record_collision(...)` to emit the collision event.

The hash → drawer_id index is rebuilt by replaying the log on startup; the
rebuild is fast because we only need the (drawer_id, content_hash) tuples.

Spec ref: Part 4 ("identical content is a glitch"), Part 8 (recurrence
clusters consume collision events).
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

from ..log.client import LogClient, get_default_client
from ..log.subscriber import get_default_registry
from ..schema.events import DrawerHashCollision
from ..schema.identifiers import make_event_id_log


# =============================================================================
# Hash computation
# =============================================================================


def compute_content_hash(transcript: str) -> str:
    """Canonical content hash for collision detection.

    The hash covers only the verbatim transcript — not facets, not
    metadata. Two captures with identical text but different state-context
    or interactional kind still collide; that's the right behavior because
    the *content* is what's repeated.
    """
    normalized = transcript.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# =============================================================================
# Collision index — derived view
# =============================================================================


@dataclass
class CollisionIndex:
    """Maps content_hash → list of drawer_ids that have produced it.

    A list rather than a single ID because we keep all colliding drawers;
    each addition past the first is a collision event.
    """

    _by_hash: dict[str, list[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _consumer_id: str = "drawer_collision_index"

    def lookup(self, content_hash: str) -> list[str]:
        """Return all drawer_ids that share this hash. Empty list = no
        prior occurrence."""
        with self._lock:
            return list(self._by_hash.get(content_hash, []))

    def add(self, content_hash: str, drawer_id: str) -> list[str]:
        """Record (hash, drawer_id) and return prior drawer_ids that
        shared the hash. Used directly when applying via subscription."""
        with self._lock:
            prior = list(self._by_hash.get(content_hash, []))
            self._by_hash.setdefault(content_hash, []).append(drawer_id)
            return prior

    def reset(self) -> None:
        with self._lock:
            self._by_hash.clear()

    # ---- subscriber interface ------------------------------------------------

    def handle(self, offset: int, kind: str, payload: dict) -> None:
        """Subscriber handler — called with (offset, kind, payload).

        Idempotent: if the drawer is already in the index for this hash,
        skip. This matters because the eager add in
        `check_and_record_collision` already inserted; the subscriber
        replay should not double-insert.
        """
        if kind == "drawer_captured":
            content_hash = payload.get("content_hash")
            drawer_id = payload.get("drawer_id")
            if content_hash and drawer_id:
                with self._lock:
                    existing = self._by_hash.setdefault(content_hash, [])
                    if drawer_id not in existing:
                        existing.append(drawer_id)


# =============================================================================
# Module-level singleton + auto-subscription
# =============================================================================

_INDEX = CollisionIndex()


def get_collision_index() -> CollisionIndex:
    return _INDEX


# Register the index as a log subscriber so it stays live.
def _register_subscription() -> None:
    registry = get_default_registry()
    if registry.get(_INDEX._consumer_id) is not None:
        return
    registry.register(
        consumer_id=_INDEX._consumer_id,
        kinds=["drawer_captured"],
        handler=_INDEX.handle,
    )


_register_subscription()


# =============================================================================
# Capture-time helper
# =============================================================================


def check_and_record_collision(
    *,
    incoming_drawer_id: str,
    content_hash: str,
    actor: str = "system",
    client: LogClient | None = None,
) -> list[str]:
    """Check whether `content_hash` has been seen before; if so, emit a
    `DrawerHashCollision` event for each prior drawer and return the list
    of prior drawer_ids.

    Called by `drawer.capture.capture_drawer` BEFORE appending the
    `drawer_captured` event so collisions are recorded against the
    already-existing prior drawers.

    Eagerly adds the incoming drawer to the index so subsequent captures
    in the same process see it as a prior occurrence — without waiting for
    the subscriber tick. The subscriber-driven update remains correct for
    cross-process and replay scenarios; this just removes the lag.

    Returns the list of prior drawer_ids (empty if no collision).
    """
    log = client or get_default_client()
    prior = _INDEX.add(content_hash, incoming_drawer_id)
    now_ms = int(time.time() * 1000)
    for existing_drawer_id in prior:
        ev = DrawerHashCollision(
            event_id=make_event_id_log(now_ms),
            recorded_at=now_ms,
            actor=actor,
            incoming_drawer_id=incoming_drawer_id,
            existing_drawer_id=existing_drawer_id,
            content_hash=content_hash,
        )
        log.append(ev)
    return prior


__all__ = [
    "CollisionIndex",
    "check_and_record_collision",
    "compute_content_hash",
    "get_collision_index",
]
