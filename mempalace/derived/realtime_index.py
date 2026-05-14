"""
Real-time pre-warmed handle index.

Per Part 8.2: certain queries are hot-path — FOYER's "give me the canon
overview," AGENT's "what's velocity-leading right now," CLAUDE_THREAD's
"what's relevant to my current scope." Resolving them on every keystroke
is wasteful. This index pre-warms a small set of recurring (scope, stance)
fingerprints by maintaining cached resolved-handle results.

Invalidation:
  - heat-field changes that touch the cached candidates → invalidate
  - canon-set changes → full re-warm
  - drawer-amend / interpretation-assigned on cached candidates → invalidate
  - TTL: 5 minutes default for hot, configurable

The index does NOT bypass handle protocol semantics — it caches the
output of `mem_resolve(handle_id)` keyed by (scope_fingerprint,
stance_fingerprint). Consumers still call `mem_resolve` and the index
sits in front, returning cached results when valid.

Spec ref: Part 8.2.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..retrieve.fidelity import RenderedCandidate
from ..retrieve.scope import Scope
from ..schema.stance import Stance
from .base import DerivedRepresentation


# =============================================================================
# Cache entries
# =============================================================================


@dataclass
class IndexEntry:
    """One cached result set."""

    fingerprint: str
    rendered: list[RenderedCandidate]
    scope_summary: dict[str, Any]
    stance_summary: dict[str, Any]
    cached_at_ms: int
    ttl_ms: int
    candidate_node_ids: frozenset[str]
    valid: bool = True

    def is_expired(self, now_ms: int) -> bool:
        return now_ms > (self.cached_at_ms + self.ttl_ms)


# =============================================================================
# Fingerprint helpers
# =============================================================================


def _fingerprint(scope: Scope, stance: Stance, fidelity: str) -> str:
    """Stable hash of a (scope, stance, fidelity) tuple."""
    payload = {
        "scope": {
            "node_ids": sorted(scope.node_ids),
            "node_kinds": sorted(k.value for k in scope.node_kinds),
            "period_ids": sorted(scope.period_ids),
            "event_ids": sorted(scope.event_ids),
            "theme_ids": sorted(scope.theme_ids),
            "valid_at_ms": scope.valid_at_ms,
            "time_window_ms": list(scope.time_window_ms) if scope.time_window_ms else None,
            "max_candidates": scope.max_candidates,
            "canonical_only": scope.canonical_only,
        },
        "stance": {
            "consumer_kind": stance.consumer_kind.value,
            "cvc": round(stance.correspondence_vs_coherence, 3),
            "recency_bias": round(stance.recency_bias, 3),
            "canonicality_floor": round(stance.canonicality_floor, 3),
            "exploration_entropy": round(stance.exploration_entropy, 3),
        },
        "fidelity": fidelity,
    }
    s = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# Index
# =============================================================================


class RealtimeIndex(DerivedRepresentation):
    """Pre-warmed handle cache."""

    name = "derived.realtime_index"
    subscribed_kinds = (
        "node_property_set",
        "edge_created",
        "edge_invalidated",
        "drawer_amended",
        "interpretation_assigned",
    )

    DEFAULT_TTL_MS = 5 * 60 * 1000  # 5 minutes

    def __init__(self, *, default_ttl_ms: int = DEFAULT_TTL_MS, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: dict[str, IndexEntry] = {}
        self._cache_lock = threading.Lock()
        self._default_ttl = default_ttl_ms

    def reset_state(self) -> None:
        with self._cache_lock:
            self._entries.clear()

    # ---- subscriber: invalidate on touching events --------------------------

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        affected_id = (
            payload.get("node_id")
            or payload.get("source_node_id")
            or payload.get("target_node_id")
            or payload.get("drawer_id")
        )
        if not affected_id:
            return
        with self._cache_lock:
            for entry in self._entries.values():
                if affected_id in entry.candidate_node_ids:
                    entry.valid = False

    # ---- public ------------------------------------------------------------

    def lookup(
        self, scope: Scope, stance: Stance, fidelity: str
    ) -> IndexEntry | None:
        fp = _fingerprint(scope, stance, fidelity)
        now_ms = int(time.time() * 1000)
        with self._cache_lock:
            entry = self._entries.get(fp)
            if entry is None:
                return None
            if not entry.valid or entry.is_expired(now_ms):
                self._entries.pop(fp, None)
                return None
            return entry

    def store(
        self,
        scope: Scope,
        stance: Stance,
        fidelity: str,
        rendered: list[RenderedCandidate],
        *,
        ttl_ms: int | None = None,
    ) -> str:
        fp = _fingerprint(scope, stance, fidelity)
        now_ms = int(time.time() * 1000)
        candidate_ids = frozenset(r.node_id for r in rendered)
        entry = IndexEntry(
            fingerprint=fp,
            rendered=list(rendered),
            scope_summary={
                "kinds": [k.value for k in scope.node_kinds],
                "period_count": len(scope.period_ids),
                "theme_count": len(scope.theme_ids),
            },
            stance_summary={"consumer": stance.consumer_kind.value},
            cached_at_ms=now_ms,
            ttl_ms=ttl_ms or self._default_ttl,
            candidate_node_ids=candidate_ids,
        )
        with self._cache_lock:
            self._entries[fp] = entry
        return fp

    def invalidate(self, fingerprint: str) -> None:
        with self._cache_lock:
            self._entries.pop(fingerprint, None)

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._entries)

    def reap_expired(self) -> int:
        now_ms = int(time.time() * 1000)
        count = 0
        with self._cache_lock:
            for fp in list(self._entries.keys()):
                e = self._entries[fp]
                if e.is_expired(now_ms) or not e.valid:
                    self._entries.pop(fp, None)
                    count += 1
        return count


__all__ = ["IndexEntry", "RealtimeIndex"]
