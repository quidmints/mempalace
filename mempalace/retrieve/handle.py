"""
Handle protocol.

A handle is a stateful retrieval cursor. It carries:
  - the scope spec
  - the active stance
  - the fidelity tier
  - accumulated refinements (more_like / less_like signals from prior
    resolves)
  - the ranker selection
  - a TTL

Per R3 §9.2 (Conway's iterative retrieval), handles support `mem_refine`:
the consumer calls resolve, looks at results, signals which were relevant
and which weren't, and the handle re-resolves with shifted scope.

Lifecycle events:
  - handle_allocated
  - handle_refined
  - handle_resolved
  - handle_closed

The handle's state is held in the daemon (via HandleManager); consumers
get back an opaque handle_id and call methods on it.

Spec ref: Part 6, R3 §9.2.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    HandleAllocated,
    HandleClosed,
    HandleRefined,
    HandleResolved,
)
from ..schema.identifiers import make_event_id_log, make_handle_id
from ..schema.stance import Stance
from .fidelity import Fidelity, RenderedCandidate, render_all
from .gather import gather
from .scope import Scope


# =============================================================================
# Handle state
# =============================================================================


@dataclass
class RefinementSignal:
    """One refinement signal from a prior resolve.

    Multiple signals accumulate; later refinements compose with earlier.
    """

    more_like: list[str] = field(default_factory=list)
    less_like: list[str] = field(default_factory=list)
    stance_adjustments: dict[str, float] = field(default_factory=dict)
    scope_adjustments: dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = 0


@dataclass
class HandleState:
    """Server-side state of a handle.

    Held in HandleManager; consumers reference by handle_id only.
    """

    handle_id: str
    scope: Scope
    stance: Stance
    fidelity: Fidelity = Fidelity.STANDARD
    ranker_id: str | None = None
    refinements: list[RefinementSignal] = field(default_factory=list)
    last_resolved_at_ms: int = 0
    allocated_at_ms: int = 0
    ttl_ms: int = 60 * 60 * 1000  # 1 hour
    closed: bool = False
    consumer_id: str = "unknown"

    substrate_verification: bool = False
    """Per R3 §9.3: when True, retrieval pipeline runs
    `mempalace.retrieve.substrate_verification.verify_assertions(...)`
    on returned assertions and attaches per-assertion faithfulness
    scores. Off by default — adds work; consumers opt in when they
    need the audit trail (review-mode, contradiction-investigation,
    federation-vetting). When False, retrieval skips the pass."""

    def is_expired(self, now_ms: int | None = None) -> bool:
        now = now_ms or int(time.time() * 1000)
        return now > (self.allocated_at_ms + self.ttl_ms)


# =============================================================================
# HandleManager
# =============================================================================


class HandleManager:
    """In-memory manager of active handles.

    Singleton in production; tests can construct their own.
    """

    def __init__(self, log_client: LogClient | None = None) -> None:
        self._client = log_client or get_default_client()
        self._handles: dict[str, HandleState] = {}
        self._lock = threading.Lock()

    # ---- allocate ------------------------------------------------------------

    def allocate(
        self,
        scope: Scope,
        stance: Stance,
        *,
        fidelity: Fidelity = Fidelity.STANDARD,
        ranker_id: str | None = None,
        ttl_ms: int = 60 * 60 * 1000,
        consumer_id: str = "unknown",
    ) -> str:
        """Allocate a new handle. Returns handle_id."""
        now = int(time.time() * 1000)
        handle_id = make_handle_id(now)
        state = HandleState(
            handle_id=handle_id,
            scope=scope,
            stance=stance,
            fidelity=fidelity,
            ranker_id=ranker_id,
            allocated_at_ms=now,
            ttl_ms=ttl_ms,
            consumer_id=consumer_id,
        )
        with self._lock:
            self._handles[handle_id] = state

        # Open the distributed-lifecycle batch keyed on handle_id.
        # refine/resolve append under this batch; close commits/aborts.
        self._client.open_batch(
            "retrieve.handles",
            handle_id,
            input_summary={"consumer_id": consumer_id},
            actor=consumer_id,
        )
        self._client.append(HandleAllocated(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor=consumer_id,
            handle_id=handle_id,
            query_text="",
            scope_spec={
                "node_ids": list(scope.node_ids),
                "node_kinds": [k.value for k in scope.node_kinds],
                "period_ids": list(scope.period_ids),
                "event_ids": list(scope.event_ids),
                "theme_ids": list(scope.theme_ids),
                "max_candidates": scope.max_candidates,
                "prefer_hierarchical": scope.prefer_hierarchical,
                "canonical_only": scope.canonical_only,
            },
            stance={
                "consumer_kind": stance.consumer_kind.value,
                "correspondence_vs_coherence": stance.correspondence_vs_coherence,
                "recency_bias": stance.recency_bias,
                "canonicality_floor": stance.canonicality_floor,
                "exploration_entropy": stance.exploration_entropy,
            },
            consumer=consumer_id,
            batch_id=handle_id,
        ))
        return handle_id

    # ---- refine --------------------------------------------------------------

    def refine(
        self,
        handle_id: str,
        *,
        more_like: list[str] | None = None,
        less_like: list[str] | None = None,
        stance_adjustments: dict[str, float] | None = None,
        scope_adjustments: dict[str, Any] | None = None,
    ) -> None:
        """Apply a refinement to an existing handle.

        The signal is appended to the handle's refinements list; the next
        resolve picks it up. Refinements compose; we don't merge them.
        """
        with self._lock:
            state = self._handles.get(handle_id)
            if state is None:
                raise KeyError(f"handle not found: {handle_id}")
            if state.closed:
                raise RuntimeError(f"handle closed: {handle_id}")

            now = int(time.time() * 1000)
            sig = RefinementSignal(
                more_like=list(more_like or []),
                less_like=list(less_like or []),
                stance_adjustments=dict(stance_adjustments or {}),
                scope_adjustments=dict(scope_adjustments or {}),
                timestamp_ms=now,
            )
            state.refinements.append(sig)

            # Apply stance adjustments now (they're cheap and additive)
            for dim, delta in sig.stance_adjustments.items():
                if hasattr(state.stance, dim):
                    cur = getattr(state.stance, dim)
                    if isinstance(cur, (int, float)):
                        new_val = max(-1.0, min(1.0, cur + delta))
                        setattr(state.stance, dim, new_val)

        self._client.append(HandleRefined(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor=state.consumer_id,
            handle_id=handle_id,
            more_like_node_ids=list(more_like or []),
            less_like_node_ids=list(less_like or []),
            scope_adjustments=dict(scope_adjustments or {}),
            batch_id=handle_id,
        ))

    # ---- resolve -------------------------------------------------------------

    def resolve(
        self,
        handle_id: str,
        *,
        ranker_fn: Any = None,
        max_results: int = 50,
    ) -> list[RenderedCandidate]:
        """Resolve a handle to ranked, fidelity-rendered results.

        ranker_fn is a callable (candidates, stance) → list[(candidate, score)].
        If None, a default identity ranker is used (score = 1.0 for all).
        Real ranker dispatch lives in batch 6 (rank/).

        Each refinement applies an inhibition / promotion at scoring time
        based on its more_like / less_like. The default ranker honors
        these. Custom rankers should also.
        """
        with self._lock:
            state = self._handles.get(handle_id)
            if state is None:
                raise KeyError(f"handle not found: {handle_id}")
            if state.closed:
                raise RuntimeError(f"handle closed: {handle_id}")
            if state.is_expired():
                raise RuntimeError(f"handle expired: {handle_id}")

            scope = state.scope
            stance = state.stance
            fidelity = state.fidelity
            refinements = list(state.refinements)

        # 1. Gather candidates
        result = gather(scope, stance=stance)

        # 2. Score candidates (default ranker if none provided)
        if ranker_fn is None:
            scored = [(c, 1.0) for c in result.candidates]
        else:
            scored = ranker_fn(result.candidates, stance)

        # 3. Apply refinement signals: more_like boosts, less_like dampens
        if refinements:
            promoted: set[str] = set()
            inhibited: set[str] = set()
            for sig in refinements:
                promoted |= set(sig.more_like)
                inhibited |= set(sig.less_like)
            adjusted: list[tuple[Any, float]] = []
            for cand, score in scored:
                if cand.node_id in inhibited:
                    score = score * 0.1
                if cand.node_id in promoted:
                    score = score * 1.5
                adjusted.append((cand, score))
            scored = adjusted

        # 4. Sort and cap
        scored.sort(key=lambda cs: cs[1], reverse=True)
        scored = scored[:max_results]

        # 5. Render at fidelity
        rendered = render_all(scored, fidelity)

        # 6. Update state and emit event
        now = int(time.time() * 1000)
        with self._lock:
            state = self._handles[handle_id]
            state.last_resolved_at_ms = now

        self._client.append(HandleResolved(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor=state.consumer_id,
            handle_id=handle_id,
            fidelity={"tier": fidelity.value},
            result_count=len(rendered),
            elapsed_ms=0,
            batch_id=handle_id,
        ))

        return rendered

    # ---- close ---------------------------------------------------------------

    def close(self, handle_id: str) -> None:
        """Close a handle. Frees state, emits close event, commits batch."""
        with self._lock:
            state = self._handles.pop(handle_id, None)
            if state is None:
                return  # idempotent
            state.closed = True

        now = int(time.time() * 1000)
        self._client.append(HandleClosed(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor=state.consumer_id,
            handle_id=handle_id,
            reason="explicit_close",
            batch_id=handle_id,
        ))
        # Close the distributed-lifecycle batch.
        self._client.close_batch(
            "retrieve.handles", handle_id, actor=state.consumer_id,
        )

    # ---- introspection -------------------------------------------------------

    def get_state(self, handle_id: str) -> HandleState | None:
        with self._lock:
            return self._handles.get(handle_id)

    def list_active(self) -> list[str]:
        with self._lock:
            return [hid for hid, s in self._handles.items() if not s.closed]

    def reap_expired(self) -> int:
        """Close all expired handles. Returns count closed."""
        now = int(time.time() * 1000)
        expired: list[str] = []
        with self._lock:
            for hid, state in self._handles.items():
                if state.is_expired(now):
                    expired.append(hid)
        for hid in expired:
            self.close(hid)
        return len(expired)


# =============================================================================
# Module-level singleton
# =============================================================================

_MANAGER: HandleManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_handle_manager() -> HandleManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = HandleManager()
        return _MANAGER


def set_handle_manager(manager: HandleManager) -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = manager


# =============================================================================
# Public functional API (matches Part 6 protocol)
# =============================================================================


def mem_allocate(
    scope: Scope,
    stance: Stance,
    *,
    fidelity: Fidelity = Fidelity.STANDARD,
    ranker_id: str | None = None,
    ttl_ms: int = 60 * 60 * 1000,
    consumer_id: str = "unknown",
) -> str:
    return get_handle_manager().allocate(
        scope, stance,
        fidelity=fidelity,
        ranker_id=ranker_id,
        ttl_ms=ttl_ms,
        consumer_id=consumer_id,
    )


def mem_refine(
    handle_id: str,
    *,
    more_like: list[str] | None = None,
    less_like: list[str] | None = None,
    stance_adjustments: dict[str, float] | None = None,
    scope_adjustments: dict[str, Any] | None = None,
) -> None:
    get_handle_manager().refine(
        handle_id,
        more_like=more_like,
        less_like=less_like,
        stance_adjustments=stance_adjustments,
        scope_adjustments=scope_adjustments,
    )


def mem_resolve(
    handle_id: str,
    *,
    ranker_fn: Any = None,
    max_results: int = 50,
) -> list[RenderedCandidate]:
    return get_handle_manager().resolve(
        handle_id, ranker_fn=ranker_fn, max_results=max_results
    )


def mem_close(handle_id: str) -> None:
    get_handle_manager().close(handle_id)


__all__ = [
    "HandleManager",
    "HandleState",
    "RefinementSignal",
    "get_handle_manager",
    "mem_allocate",
    "mem_close",
    "mem_refine",
    "mem_resolve",
    "set_handle_manager",
]
