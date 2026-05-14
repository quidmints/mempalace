"""
Stance-conditional ranker dispatch.

Maps ConsumerKind → ranker. Different consumers have different latency,
exploration, and interpretability requirements; the dispatch table makes
the choice explicit.

Default policy:
  CLAUDE_THREAD  → neural_stub      (latency 50–200ms acceptable, quality high)
  MONTAGE        → neural_stub      (offline, can afford the extra cost)
  MATCHING       → neural_stub      (federation matching values quality)
  FOYER          → factored         (real-time UI, must be fast & deterministic)
  AGENT          → factored         (low-latency loops)
  REVIEW_MODE    → neural_stub      (user-driven, takes its time)
  RESOLVE        → factored         (handle resolve in retrieval — fast path)

The mapping is overridable per palace via configure_dispatch().

Spec ref: Part 7 (consumer-specific rankers), R3 §1, §9.4.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..retrieve.gather import Candidate
from ..schema.stance import ConsumerKind, Stance
from .protocol import Ranker, ScoredCandidate
from .registry import get_ranker_registry


# =============================================================================
# Default mapping
# =============================================================================


_DEFAULT_DISPATCH: dict[ConsumerKind, str] = {
    ConsumerKind.CLAUDE_THREAD: "neural_stub",
    ConsumerKind.MONTAGE: "neural_stub",
    ConsumerKind.MATCHING: "neural_stub",
    ConsumerKind.FOYER: "factored_multiplicative",
    ConsumerKind.AGENT: "factored_multiplicative",
    ConsumerKind.REVIEW_MODE: "neural_stub",
    ConsumerKind.RESOLVE: "factored_multiplicative",
}


# =============================================================================
# DispatchTable
# =============================================================================


@dataclass
class DispatchTable:
    """Mutable mapping of ConsumerKind → ranker name."""

    _mapping: dict[ConsumerKind, str] = field(
        default_factory=lambda: dict(_DEFAULT_DISPATCH)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_ranker_name(self, consumer_kind: ConsumerKind) -> str:
        with self._lock:
            return self._mapping.get(consumer_kind, "factored_multiplicative")

    def set(self, consumer_kind: ConsumerKind, ranker_name: str) -> None:
        with self._lock:
            self._mapping[consumer_kind] = ranker_name

    def reset(self) -> None:
        with self._lock:
            self._mapping = dict(_DEFAULT_DISPATCH)


# =============================================================================
# Module-level singleton
# =============================================================================


_TABLE = DispatchTable()


def get_dispatch_table() -> DispatchTable:
    return _TABLE


def configure_dispatch(consumer_kind: ConsumerKind, ranker_name: str) -> None:
    """Override the default dispatch for a consumer kind."""
    _TABLE.set(consumer_kind, ranker_name)


# =============================================================================
# dispatch()
# =============================================================================


def dispatch_ranker(stance: Stance) -> Ranker:
    """Pick the appropriate ranker for the given stance.

    Falls back to factored if the configured ranker isn't registered.
    """
    name = _TABLE.get_ranker_name(stance.consumer_kind)
    registry = get_ranker_registry()
    ranker = registry.get(name)
    if ranker is None:
        # Fallback to factored if configured ranker is missing
        ranker = registry.get("factored_multiplicative")
        if ranker is None:
            raise RuntimeError(
                f"No ranker available for {stance.consumer_kind.value}; "
                f"configured={name!r} and no factored fallback registered"
            )
    return ranker


def rank_candidates(
    candidates: list[Candidate],
    stance: Stance,
    *,
    feature_names: list[str] | None = None,
    ranker_name: str | None = None,
) -> list[ScoredCandidate]:
    """One-shot: dispatch a ranker and run it.

    If `ranker_name` is provided, that ranker is used directly (bypassing
    the dispatch table). Otherwise the dispatch table maps the stance's
    consumer_kind to a ranker.
    """
    if ranker_name is not None:
        ranker = get_ranker_registry().get(ranker_name)
        if ranker is None:
            raise KeyError(f"ranker not registered: {ranker_name}")
    else:
        ranker = dispatch_ranker(stance)
    return ranker.rank(candidates, stance, feature_names=feature_names)


__all__ = [
    "DispatchTable",
    "configure_dispatch",
    "dispatch_ranker",
    "get_dispatch_table",
    "rank_candidates",
]
