"""
Ranker registry.

Holds the active rankers, keyed by name. Dispatch (`rank.dispatch`) reads
from the registry to pick a ranker by ConsumerKind. The registry is also
where signed-loaded rankers from the federation get registered after
verification.

Per R3 §6: ranker isolation is enforced at the boundary — the registry
itself trusts what it holds, but new rankers entering the registry must
have passed signature verification (`rank.signed_loader`) and must be
running in process isolation (`rank.isolation`).

Spec ref: Part 7 (rankers), R3 §6.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .factored import FactoredMultiplicativeRanker
from .neural_stub import NeuralRankerStub
from .protocol import Ranker, RankerManifest


@dataclass
class RankerRegistry:
    """Registry of available rankers."""

    _rankers: dict[str, Ranker] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, ranker: Ranker) -> None:
        with self._lock:
            self._rankers[ranker.name] = ranker

    def get(self, name: str) -> Ranker | None:
        with self._lock:
            return self._rankers.get(name)

    def list_rankers(self) -> list[Ranker]:
        with self._lock:
            return list(self._rankers.values())

    def list_manifests(self) -> list[RankerManifest]:
        with self._lock:
            return [r.declares() for r in self._rankers.values()]

    def unregister(self, name: str) -> None:
        with self._lock:
            self._rankers.pop(name, None)

    def reset(self) -> None:
        with self._lock:
            self._rankers.clear()


# =============================================================================
# Module-level singleton with builtins
# =============================================================================


_REGISTRY = RankerRegistry()


def get_ranker_registry() -> RankerRegistry:
    return _REGISTRY


def _seed_builtins() -> None:
    if _REGISTRY.get("factored_multiplicative") is None:
        _REGISTRY.register(FactoredMultiplicativeRanker())
    if _REGISTRY.get("neural_stub") is None:
        _REGISTRY.register(NeuralRankerStub())


_seed_builtins()


__all__ = ["RankerRegistry", "get_ranker_registry"]
