"""
Cache projection — Track 4B.

Per HANDLES_DESIGN.md v2 §"Cluster-pattern caching":

  - Default-distinct cache (Track 4A) keys on
    `(query_hash, ranker_name, cluster_signature)`.
  - Projection mechanism (Track 4B) observes when two distinct keys
    produce equivalent outputs over time. After K consistent
    equivalence observations within a window, promotes to a shared
    key with `cluster_signature` replaced by the
    `PROJECTED_CLUSTER_SIGNATURE` sentinel.
  - Demotion: any divergence within the demotion window demotes
    back to distinct keys. Records `cache_projection_demoted` event.
  - Stability cap: after N promote/demote cycles within a year,
    flagged as unstable. No further auto-promotion. Records
    `cache_projection_unstable` event.

Same online-learning shape as R3 §8.3 signature-triage feedback:
cooldowns, caps, reversibility, audit events.

# Why a separate module

Track 4A's `RankerOutputCache` is the cache itself. Track 4B is
the *learning loop on top of it*. Keeping them separate means:

  - Producers that don't want projection (most ranker calls during
    initial development) don't pay the equivalence-test overhead.
  - The projection logic can evolve (e.g., custom equivalence
    tests per ranker) without churning the cache.

# Integration with Track 4A

The cache notifies the projection on every `put()`:

    projection.observe_put(key, value, now_ms=...)

The projection accumulates equivalence observations. When K matches
accumulate within the observation window, the projection:

  1. Calls `cache.put(projected_key, value)` to seed the shared bucket.
  2. Records a `CacheProjectionPromoted` event.
  3. Toggles the projection state to PROMOTED.

Subsequent `cache.get(key)` callers check first whether the (qh, rn)
pair is promoted; if so, they look up the projected key. The lookup
helper `lookup_with_projection(cache, projection, key)` encapsulates
this.

# Why not modify Track 4A's get() directly

Two reasons:
  1. Backward compat: existing tests + callers use the cache
     directly without projection.
  2. Track 4A doesn't know about clusters of equivalent keys; it
     stores discrete entries. The projection layer maintains the
     equivalence graph.

Spec ref: HANDLES_DESIGN.md v2 §"Cluster-pattern caching",
IMPLEMENTATION_ROADMAP.md §"Track 4B".
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    CacheProjectionDemoted,
    CacheProjectionPromoted,
    CacheProjectionUnstable,
)
from ..schema.identifiers import make_event_id_log
from .ranker_cache import (
    PROJECTED_CLUSTER_SIGNATURE,
    RankerOutputCache,
    RankerOutputCacheKey,
    get_default_cache,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration constants
# =============================================================================


PROMOTION_THRESHOLD = 10
"""K — consecutive equivalence observations required to promote."""

PROMOTION_WINDOW_MS = 7 * 24 * 3600 * 1000
"""7 days in milliseconds — observations older than this drop out
of the K-count."""

DEMOTION_WINDOW_MS = 30 * 24 * 3600 * 1000
"""30 days in milliseconds — divergence within this window of last
promotion triggers demotion."""

PROMOTION_COOLDOWN_MS = 30 * 24 * 3600 * 1000
"""30 days — a key pair can't be re-promoted within this window
after a demotion. Defense against rapid promote ↔ demote churn."""

INSTABILITY_CAP = 3
"""N — max promote/demote cycles within INSTABILITY_WINDOW_MS before
the pair is flagged unstable."""

INSTABILITY_WINDOW_MS = 365 * 24 * 3600 * 1000
"""1 year — window over which the cycle cap applies."""


# =============================================================================
# Projection state
# =============================================================================


class ProjectionStatus(str, enum.Enum):
    NOT_PROMOTED = "not_promoted"
    """Initial state. Distinct cluster_signatures cache distinctly."""

    PROMOTED = "promoted"
    """K consistent equivalence observations accumulated; entries now
    share a single projected key."""

    UNSTABLE = "unstable"
    """Hit the promote/demote cycle cap. No further auto-promotion."""


@dataclass
class _ObservationRecord:
    """One equivalence observation between two cluster_signatures
    for the same (query_hash, ranker_name) pair."""

    sig_a: str
    sig_b: str
    timestamp_ms: int
    matched: bool
    """True for equivalence observed; False for divergence."""


@dataclass
class _CycleEvent:
    """A promotion or demotion event in the cycle history."""

    timestamp_ms: int
    is_promotion: bool


@dataclass
class _ProjectionState:
    """Per-(query_hash, ranker_name) state carried by the projection."""

    query_hash: str
    ranker_name: str
    status: ProjectionStatus = ProjectionStatus.NOT_PROMOTED
    observations: list[_ObservationRecord] = field(default_factory=list)
    cycle_history: list[_CycleEvent] = field(default_factory=list)
    last_promotion_ms: int | None = None
    last_demotion_ms: int | None = None
    promoted_signatures: set[str] = field(default_factory=set)
    """Cluster signatures that were merged at most-recent promotion."""

    def prune_observations(self, *, before_ms: int) -> None:
        """Drop observations older than the cutoff. Keeps the K-count
        accurate against the rolling window."""
        self.observations = [
            o for o in self.observations if o.timestamp_ms >= before_ms
        ]

    def cycle_count_within(self, *, since_ms: int) -> int:
        """Number of (promotion, demotion) cycles in the recent
        window. A pair counts as one cycle when both events are in
        the window."""
        recent = [c for c in self.cycle_history if c.timestamp_ms >= since_ms]
        promotions = sum(1 for c in recent if c.is_promotion)
        return promotions


# =============================================================================
# Equivalence tests
# =============================================================================


EquivalenceFn = Callable[[Any, Any], bool]
"""Signature for a custom equivalence test. Receives two cache
values; returns True if they're equivalent for projection purposes."""


def deep_equality(a: Any, b: Any) -> bool:
    """Default equivalence: structural equality. Adequate for
    simple cached lists. Replace with `top_k_node_ids_match` or
    similar for ScoredCandidate-typed caches."""
    return a == b


def top_k_node_ids_match(k: int = 5) -> EquivalenceFn:
    """Equivalence: top-K cached candidates have matching node_ids
    in order. Use with caches that store `list[ScoredCandidate]`.

    Two cached lists are equivalent iff:
      - Both have at least min(k, len(a), len(b)) entries.
      - The first K entries' node_ids match in order.

    Scores aren't compared — equivalent rankings under different
    cluster patterns can have slightly different absolute scores
    but the same ordering, which is what drives retrieval.
    """

    def _check(a: Any, b: Any) -> bool:
        if not isinstance(a, list) or not isinstance(b, list):
            return False
        if len(a) == 0 and len(b) == 0:
            return True
        n = min(k, len(a), len(b))
        if n == 0:
            return False
        for i in range(n):
            ai_id = _candidate_node_id(a[i])
            bi_id = _candidate_node_id(b[i])
            if ai_id is None or bi_id is None or ai_id != bi_id:
                return False
        return True

    return _check


def _candidate_node_id(c: Any) -> str | None:
    """Try to extract a node_id from a `ScoredCandidate`-shaped
    object. Returns None if the shape doesn't match."""
    # ScoredCandidate has .candidate.node_id
    cand = getattr(c, "candidate", None)
    if cand is not None:
        node_id = getattr(cand, "node_id", None)
        if isinstance(node_id, str):
            return node_id
    # Plain dict shape
    if isinstance(c, dict):
        if "node_id" in c:
            return c["node_id"]
        if "candidate" in c and isinstance(c["candidate"], dict):
            return c["candidate"].get("node_id")
    # Plain string fallback (test fixtures)
    if isinstance(c, str):
        return c
    return None


# =============================================================================
# Projection registry
# =============================================================================


@dataclass
class CacheProjection:
    """The projection mechanism.

    Construction:
      proj = CacheProjection(cache=my_cache, equivalence_fn=...)
      proj = CacheProjection()   # uses default cache + deep equality

    Hook into Track 4A:
      proj.observe_put(key, value)        # called on every cache.put()
      hit = proj.lookup(key)              # uses projected entry if promoted
    """

    cache: RankerOutputCache | None = None
    """The Track 4A cache. None → default cache."""

    equivalence_fn: EquivalenceFn = field(default=deep_equality)
    """Test for whether two cached values are equivalent. Defaults
    to deep equality; replace with `top_k_node_ids_match()` for
    ScoredCandidate-typed caches."""

    log_client: LogClient | None = None
    """Where to append projection audit events. None → default log."""

    promotion_threshold: int = PROMOTION_THRESHOLD
    promotion_window_ms: int = PROMOTION_WINDOW_MS
    demotion_window_ms: int = DEMOTION_WINDOW_MS
    promotion_cooldown_ms: int = PROMOTION_COOLDOWN_MS
    instability_cap: int = INSTABILITY_CAP
    instability_window_ms: int = INSTABILITY_WINDOW_MS

    projected_entry_ttl_sec: float = 24 * 3600.0
    """TTL on the projected cache entry. Defaults to 24h — long
    enough that the projected entry isn't constantly being refreshed
    by demotion-due-to-eviction. Track 4A's substrate-change
    invalidation still drops the entry when its dependencies change."""

    _states: dict[tuple[str, str], _ProjectionState] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def observe_put(
        self,
        key: RankerOutputCacheKey,
        value: Any,
        *,
        now_ms: int | None = None,
    ) -> None:
        """Called by integrations after `cache.put(key, value)`.

        Records the observation, tests equivalence against any
        existing entry under a different cluster_signature, and
        triggers promotion / demotion if the right conditions met.

        The skip cases:
          - Promoted-key inserts (cluster_signature ==
            `PROJECTED_CLUSTER_SIGNATURE`) — those are the projection's
            own writes; observing them would create a feedback loop.
          - Promoted state but key's signature is in
            `promoted_signatures` — the key is part of the projection;
            value-write under it shouldn't trigger another observation.
            We still test for divergence and may demote.
        """
        if key.cluster_signature == PROJECTED_CLUSTER_SIGNATURE:
            return

        if now_ms is None:
            now_ms = int(time.time() * 1000)

        with self._lock:
            state = self._states.setdefault(
                (key.query_hash, key.ranker_name),
                _ProjectionState(
                    query_hash=key.query_hash,
                    ranker_name=key.ranker_name,
                ),
            )

            if state.status == ProjectionStatus.UNSTABLE:
                # No further auto-promotion. Observation is dropped.
                return

            if state.status == ProjectionStatus.PROMOTED:
                self._handle_put_while_promoted(state, key, value, now_ms)
                return

            # NOT_PROMOTED — accumulate observations, test for promotion
            self._observe_and_maybe_promote(state, key, value, now_ms)

    def lookup(
        self,
        key: RankerOutputCacheKey,
        *,
        now_ms: int | None = None,
    ) -> Any | None:
        """Read-side helper. If the (query_hash, ranker_name) is
        currently promoted, looks up the projected key first; falls
        back to the signature-specific key.

        Returns the cached value (the bare value, not the entry)
        or None on miss.
        """
        cache = self.cache or get_default_cache()
        now_ms = now_ms or int(time.time() * 1000)

        with self._lock:
            state = self._states.get((key.query_hash, key.ranker_name))
            if state is not None and state.status == ProjectionStatus.PROMOTED:
                if key.cluster_signature in state.promoted_signatures:
                    projected_key = RankerOutputCacheKey(
                        query_hash=key.query_hash,
                        ranker_name=key.ranker_name,
                        cluster_signature=PROJECTED_CLUSTER_SIGNATURE,
                    )
                    hit = cache.get(projected_key, now_ms=now_ms)
                    if hit is not None:
                        return hit
                    # Projected entry vanished (TTL or eviction);
                    # demote and fall through to signature-specific
                    self._demote(
                        state,
                        reason="projected_entry_evicted",
                        now_ms=now_ms,
                    )

        # Non-promoted or fall-through: signature-specific lookup
        return cache.get(key, now_ms=now_ms)

    def status(
        self,
        query_hash: str,
        ranker_name: str,
    ) -> ProjectionStatus:
        """Inspect projection state. Returns NOT_PROMOTED if no
        observations have been recorded for this pair."""
        with self._lock:
            state = self._states.get((query_hash, ranker_name))
            if state is None:
                return ProjectionStatus.NOT_PROMOTED
            return state.status

    def stats(self) -> dict[str, int]:
        """Aggregate stats across all tracked pairs."""
        with self._lock:
            promoted = sum(
                1
                for s in self._states.values()
                if s.status == ProjectionStatus.PROMOTED
            )
            unstable = sum(
                1
                for s in self._states.values()
                if s.status == ProjectionStatus.UNSTABLE
            )
            return {
                "tracked_pairs": len(self._states),
                "promoted": promoted,
                "unstable": unstable,
            }

    def reset(self) -> None:
        """Drop all projection state. Test helper."""
        with self._lock:
            self._states.clear()

    # ------------------------------------------------------------------
    # Internals — promotion / demotion machinery
    # ------------------------------------------------------------------

    def _observe_and_maybe_promote(
        self,
        state: _ProjectionState,
        new_key: RankerOutputCacheKey,
        new_value: Any,
        now_ms: int,
    ) -> None:
        """Compare the new put to existing entries with the same
        (qh, rn) but different cluster_signature."""
        cache = self.cache or get_default_cache()

        # Cooldown after a demotion: don't observe new equivalences
        if state.last_demotion_ms is not None:
            if now_ms - state.last_demotion_ms < self.promotion_cooldown_ms:
                return

        # Find peer entries
        peer_keys = [
            k
            for k in cache.keys()
            if k.query_hash == new_key.query_hash
            and k.ranker_name == new_key.ranker_name
            and k.cluster_signature != new_key.cluster_signature
            and k.cluster_signature != PROJECTED_CLUSTER_SIGNATURE
        ]

        for peer_key in peer_keys:
            peer_value = cache.get(peer_key, now_ms=now_ms)
            if peer_value is None:
                continue
            matched = self._test_equivalence(new_value, peer_value)
            state.observations.append(
                _ObservationRecord(
                    sig_a=new_key.cluster_signature,
                    sig_b=peer_key.cluster_signature,
                    timestamp_ms=now_ms,
                    matched=matched,
                )
            )

        # Prune to window
        cutoff = now_ms - self.promotion_window_ms
        state.prune_observations(before_ms=cutoff)

        # Test for promotion: K consecutive matches in the window,
        # no recent divergence
        recent_matches = sum(1 for o in state.observations if o.matched)
        recent_diverges = sum(1 for o in state.observations if not o.matched)
        if recent_diverges > 0:
            # A divergence resets — projection requires CONSISTENT matches
            return

        if recent_matches >= self.promotion_threshold:
            # Collect signatures involved in matches
            sigs: set[str] = set()
            for o in state.observations:
                if o.matched:
                    sigs.add(o.sig_a)
                    sigs.add(o.sig_b)
            self._promote(state, sigs, new_value, now_ms=now_ms)

    def _handle_put_while_promoted(
        self,
        state: _ProjectionState,
        key: RankerOutputCacheKey,
        value: Any,
        now_ms: int,
    ) -> None:
        """Put while promoted: test divergence against the projected
        entry. If divergent within the demotion window, demote."""
        cache = self.cache or get_default_cache()

        if key.cluster_signature not in state.promoted_signatures:
            # New signature joining the family — observe normally,
            # but don't yet add to the projected family. The next
            # promotion attempt will pick it up.
            return

        # Compare new value to projected value
        projected_key = RankerOutputCacheKey(
            query_hash=state.query_hash,
            ranker_name=state.ranker_name,
            cluster_signature=PROJECTED_CLUSTER_SIGNATURE,
        )
        projected_value = cache.get(projected_key, now_ms=now_ms)
        if projected_value is None:
            # Projected entry missing — demote. (Eviction or TTL.)
            self._demote(state, reason="projected_entry_missing", now_ms=now_ms)
            return

        if self._test_equivalence(value, projected_value):
            # Still equivalent; nothing to do
            return

        # Divergence detected
        if state.last_promotion_ms is not None:
            days_since = (now_ms - state.last_promotion_ms) / (1000 * 86400)
        else:
            days_since = 0.0

        # Demotion window check
        in_demotion_window = (
            state.last_promotion_ms is not None
            and now_ms - state.last_promotion_ms <= self.demotion_window_ms
        )
        if in_demotion_window:
            self._demote(
                state,
                reason=f"divergence_at_signature_{key.cluster_signature}",
                now_ms=now_ms,
                days_since_last_promotion=days_since,
            )
        else:
            # Outside the demotion window — strictly speaking the
            # design says any divergence within 30 days demotes;
            # outside, the projection is considered stable. We log
            # but don't demote.
            logger.debug(
                "Divergence outside demotion window for (%s, %s); ignored",
                state.query_hash,
                state.ranker_name,
            )

    def _promote(
        self,
        state: _ProjectionState,
        signatures: set[str],
        seed_value: Any,
        *,
        now_ms: int,
    ) -> None:
        """Transition NOT_PROMOTED → PROMOTED."""
        cache = self.cache or get_default_cache()

        # Seed the projected entry with the new value (which observed
        # equivalence with peers)
        projected_key = RankerOutputCacheKey(
            query_hash=state.query_hash,
            ranker_name=state.ranker_name,
            cluster_signature=PROJECTED_CLUSTER_SIGNATURE,
        )
        cache.put(
            projected_key,
            seed_value,
            substrate_deps=[],  # TODO(track-4B-deps): inherit deps from peers
            now_ms=now_ms,
            ttl_sec=self.projected_entry_ttl_sec,
        )

        state.status = ProjectionStatus.PROMOTED
        state.promoted_signatures = set(signatures)
        state.last_promotion_ms = now_ms
        state.cycle_history.append(
            _CycleEvent(timestamp_ms=now_ms, is_promotion=True)
        )

        # Audit event
        observation_count = sum(1 for o in state.observations if o.matched)
        observation_window_ms = self.promotion_window_ms

        # Compute cumulative promotion count from cycle history
        promotion_count = sum(1 for c in state.cycle_history if c.is_promotion)

        self._emit_event(
            CacheProjectionPromoted(
                event_id=make_event_id_log(),
                recorded_at=now_ms,
                actor="cache_projection",
                query_hash=state.query_hash,
                ranker_name=state.ranker_name,
                promoted_cluster_signatures=sorted(signatures),
                observation_count=observation_count,
                observation_window_ms=observation_window_ms,
                promotion_count=promotion_count,
            )
        )

        # Clear observations now that they've been consumed by the
        # promotion
        state.observations.clear()

        logger.info(
            "Cache projection PROMOTED (%s, %s) merging %d signatures",
            state.query_hash,
            state.ranker_name,
            len(signatures),
        )

    def _demote(
        self,
        state: _ProjectionState,
        *,
        reason: str,
        now_ms: int,
        days_since_last_promotion: float = 0.0,
    ) -> None:
        """Transition PROMOTED → NOT_PROMOTED (or UNSTABLE if cap hit)."""
        cache = self.cache or get_default_cache()

        # Drop the projected cache entry — distinct keys come back
        projected_key = RankerOutputCacheKey(
            query_hash=state.query_hash,
            ranker_name=state.ranker_name,
            cluster_signature=PROJECTED_CLUSTER_SIGNATURE,
        )
        cache.invalidate(projected_key)

        demoted_sigs = list(state.promoted_signatures)
        state.promoted_signatures.clear()
        state.last_demotion_ms = now_ms
        state.cycle_history.append(
            _CycleEvent(timestamp_ms=now_ms, is_promotion=False)
        )

        # Audit event
        self._emit_event(
            CacheProjectionDemoted(
                event_id=make_event_id_log(),
                recorded_at=now_ms,
                actor="cache_projection",
                query_hash=state.query_hash,
                ranker_name=state.ranker_name,
                demoted_cluster_signatures=sorted(demoted_sigs),
                divergence_detail=reason,
                days_since_last_promotion=days_since_last_promotion,
            )
        )

        # Stability cap check — count promotions in the last year
        cutoff = now_ms - self.instability_window_ms
        cycle_count = state.cycle_count_within(since_ms=cutoff)
        if cycle_count >= self.instability_cap:
            state.status = ProjectionStatus.UNSTABLE
            self._emit_event(
                CacheProjectionUnstable(
                    event_id=make_event_id_log(),
                    recorded_at=now_ms,
                    actor="cache_projection",
                    query_hash=state.query_hash,
                    ranker_name=state.ranker_name,
                    cycle_count=cycle_count,
                    flagged_within_days=self.instability_window_ms / (1000 * 86400),
                )
            )
            logger.warning(
                "Cache projection (%s, %s) flagged UNSTABLE after %d cycles",
                state.query_hash,
                state.ranker_name,
                cycle_count,
            )
        else:
            state.status = ProjectionStatus.NOT_PROMOTED

        # Clear observations on demotion — start fresh
        state.observations.clear()

        logger.info(
            "Cache projection DEMOTED (%s, %s) reason=%s cycle_count=%d",
            state.query_hash,
            state.ranker_name,
            reason,
            cycle_count,
        )

    def _test_equivalence(self, a: Any, b: Any) -> bool:
        """Wrap the equivalence_fn so any test-side exceptions
        become a False return (defensive — a buggy custom equivalence
        shouldn't crash the cache integration)."""
        try:
            return bool(self.equivalence_fn(a, b))
        except Exception as e:
            logger.warning("Equivalence test raised %s; treating as not equivalent", e)
            return False

    def _emit_event(self, event: Any) -> None:
        """Append the projection audit event to the log."""
        log = self.log_client or get_default_client()
        try:
            log.append(event)
        except Exception as e:
            logger.warning("Projection event append failed: %s", e)


# =============================================================================
# Process-wide singleton
# =============================================================================


_PROJECTION: CacheProjection | None = None
_PROJECTION_LOCK = threading.Lock()


def get_default_projection() -> CacheProjection:
    """Return the process-wide default projection."""
    global _PROJECTION
    with _PROJECTION_LOCK:
        if _PROJECTION is None:
            _PROJECTION = CacheProjection()
        return _PROJECTION


def set_default_projection(projection: CacheProjection | None) -> None:
    """Replace the default projection. Test helper."""
    global _PROJECTION
    with _PROJECTION_LOCK:
        _PROJECTION = projection


def reset_default_projection() -> None:
    set_default_projection(None)


__all__ = [
    "DEMOTION_WINDOW_MS",
    "INSTABILITY_CAP",
    "INSTABILITY_WINDOW_MS",
    "PROMOTION_COOLDOWN_MS",
    "PROMOTION_THRESHOLD",
    "PROMOTION_WINDOW_MS",
    "CacheProjection",
    "EquivalenceFn",
    "ProjectionStatus",
    "deep_equality",
    "get_default_projection",
    "reset_default_projection",
    "set_default_projection",
    "top_k_node_ids_match",
]
