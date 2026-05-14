"""
Feature persistence.

Computed features land in the master log as `feature_computed` events;
a derived view subscribes to those events and materializes a typed
feature store keyed by (feature_name, subject_id, version, log_offset).

Persistence is decoupled from compute so that:

  - Tests can compute without writing.
  - Recompute-and-overwrite is explicit (emit a new event with a higher
    offset; the materialized store replaces).
  - Multiple feature versions can coexist; consumers read by versioned
    name when they want stability.

The materialized feature store is held in `FeatureStore` below. In
production, a Rust DDflow operator owns this; this Python class is the
fallback used in dev/test and as the typed accessor regardless of who
owns the storage.

Spec ref: Part 7.1, Part 8 (derived representations).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..log.subscriber import get_default_registry
from ..schema.identifiers import make_event_id_log
from .compute import compute as compute_feature
from .registry import FeatureDef, get_registry


# =============================================================================
# Materialized feature store (derived view)
# =============================================================================


@dataclass
class FeatureValue:
    """One stored feature value with provenance."""

    feature_name: str
    feature_version: str
    subject_id: str
    value: Any
    computed_at_ms: int
    computed_at_offset: int = 0


@dataclass
class FeatureStore:
    """Derived view: maps (feature_name, subject_id) → latest FeatureValue.

    Versioned values can be retrieved via `get_by_version`.
    """

    _by_name_and_subject: dict[tuple[str, str], FeatureValue] = field(default_factory=dict)
    _versioned: dict[tuple[str, str, str], FeatureValue] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _consumer_id: str = "feature_store"

    def get(self, feature_name: str, subject_id: str) -> FeatureValue | None:
        with self._lock:
            return self._by_name_and_subject.get((feature_name, subject_id))

    def get_by_version(
        self, feature_name: str, feature_version: str, subject_id: str
    ) -> FeatureValue | None:
        with self._lock:
            return self._versioned.get((feature_name, feature_version, subject_id))

    def all_for_subject(self, subject_id: str) -> dict[str, FeatureValue]:
        """All latest features for a subject keyed by feature_name."""
        with self._lock:
            return {
                fname: fv
                for (fname, sid), fv in self._by_name_and_subject.items()
                if sid == subject_id
            }

    def all_for_feature(self, feature_name: str) -> dict[str, FeatureValue]:
        """All subjects' latest values for a feature."""
        with self._lock:
            return {
                sid: fv
                for (fname, sid), fv in self._by_name_and_subject.items()
                if fname == feature_name
            }

    def reset(self) -> None:
        with self._lock:
            self._by_name_and_subject.clear()
            self._versioned.clear()

    # ---- subscriber interface ------------------------------------------------

    def handle(self, offset: int, kind: str, payload: dict) -> None:
        """Subscriber: materialize feature_computed events into the store."""
        if kind != "feature_computed":
            return
        fname = payload.get("feature_name")
        fver = payload.get("feature_version")
        sid = payload.get("subject_id")
        value = payload.get("value")
        computed_at_ms = payload.get("computed_at_ms", 0)
        if not (fname and fver and sid):
            return
        fv = FeatureValue(
            feature_name=fname,
            feature_version=fver,
            subject_id=sid,
            value=value,
            computed_at_ms=computed_at_ms,
            computed_at_offset=offset,
        )
        with self._lock:
            existing = self._by_name_and_subject.get((fname, sid))
            if existing is None or offset >= existing.computed_at_offset:
                self._by_name_and_subject[(fname, sid)] = fv
            self._versioned[(fname, fver, sid)] = fv


# =============================================================================
# Module-level singleton + auto-subscription
# =============================================================================

_STORE = FeatureStore()


def get_feature_store() -> FeatureStore:
    return _STORE


def _register_subscription() -> None:
    registry = get_default_registry()
    if registry.get(_STORE._consumer_id) is not None:
        return
    registry.register(
        consumer_id=_STORE._consumer_id,
        kinds=["feature_computed"],
        handler=_STORE.handle,
    )


_register_subscription()


# =============================================================================
# persist() — compute and write
# =============================================================================


def persist(
    feature_name: str,
    subject_id: str,
    *,
    value: Any | None = None,
    stance: Any | None = None,
    now_ms: int | None = None,
    client: LogClient | None = None,
) -> FeatureValue:
    """Compute (if value not provided) and persist a feature.

    Emits a `feature_computed` event. The event payload carries the
    (name, version, subject, value, computed_at). The feature store
    subscriber materializes it into FeatureStore.

    Eagerly writes to the in-process store as well, so callers don't
    need to wait for subscriber tick.
    """
    reg = get_registry()
    fdef = reg.get(feature_name)
    if fdef is None:
        raise KeyError(f"unknown feature: {feature_name}")

    if value is None:
        value = compute_feature(
            feature_name, subject_id, stance=stance, now_ms=now_ms
        )
    else:
        fdef.validate_value(value)

    log = client or get_default_client()
    now = now_ms or int(time.time() * 1000)

    # No FeatureComputed event class exists yet in schema — emit a generic
    # event-shaped payload via the log's append (the log accepts dataclass
    # events; for now we use a minimal wrapper).
    payload = {
        "feature_name": feature_name,
        "feature_version": fdef.version,
        "subject_id": subject_id,
        "value": value,
        "computed_at_ms": now,
    }
    # Backend-level append takes (kind, payload) directly; some clients
    # also accept Event dataclasses. Use the lower-level path here since
    # FeatureComputed isn't a registered Event class in this batch.
    backend = log._backend  # type: ignore[attr-defined]
    offset = backend.append("feature_computed", payload)

    fv = FeatureValue(
        feature_name=feature_name,
        feature_version=fdef.version,
        subject_id=subject_id,
        value=value,
        computed_at_ms=now,
        computed_at_offset=offset,
    )

    # Eager update so feature is visible without subscriber tick
    with _STORE._lock:
        _STORE._by_name_and_subject[(feature_name, subject_id)] = fv
        _STORE._versioned[(feature_name, fdef.version, subject_id)] = fv

    return fv


def persist_many(
    feature_names: list[str],
    subject_id: str,
    *,
    stance: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, FeatureValue]:
    """Persist several features for a subject."""
    return {
        name: persist(name, subject_id, stance=stance, now_ms=now_ms)
        for name in feature_names
    }


__all__ = [
    "FeatureStore",
    "FeatureValue",
    "get_feature_store",
    "persist",
    "persist_many",
]
