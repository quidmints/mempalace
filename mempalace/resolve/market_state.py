"""
Off-chain mirror of on-chain market state.

Per R3 §2.2: the resolution stack consumes market state from the
on-chain Anchor program, but doesn't read the chain on every step.
Instead, on-chain updates produce events in the local log; a derived
view (DDflow) maintains the current state of every market the daemon
cares about.

This module owns:

  - Market dataclass (off-chain mirror)
  - MarketStatus enum
  - MarketStateStore: keyed by market_id, fed by chain-update events
  - ingest_chain_update(): adapter from raw chain payload to local
    update events

Spec ref: R3 §2.2.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Market types
# =============================================================================


class MarketStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    QUALIFIED = "qualified"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class MarketKind(str, Enum):
    """Per R3 §5.2 + existing types."""

    TAG_ONLY = "tag_only"
    EXTERNAL_EVIDENCE = "external_evidence"
    BEHAVIOR_VS_BASELINE = "behavior_vs_baseline"
    MULTI_WITNESS = "multi_witness"
    BLIND_FORMULA = "blind_formula"


@dataclass
class Market:
    """An off-chain mirror of a market record."""

    market_id: str
    kind: MarketKind = MarketKind.TAG_ONLY
    status: MarketStatus = MarketStatus.PENDING
    creator_pubkey: str = ""
    subject_pubkey: str = ""

    # Question + outcomes
    question: str = ""
    outcomes: list[str] = field(default_factory=list)
    resolution_outcome: int | None = None      # index into outcomes once resolved

    # Lifecycle timestamps (ms)
    created_at_ms: int = 0
    qualifies_at_ms: int = 0
    closes_at_ms: int = 0
    resolves_at_ms: int = 0

    # Resolution metadata
    formula_id: str = ""
    resolver_pubkey: str = ""
    privacy_mode: str = "public"
    resolvability_class: str = ""

    # Stake / fee
    creator_stake_lamports: int = 0
    pool_lamports: int = 0

    # Extra metadata for kind-specific markets (baseline window, witness
    # set, encrypted formula, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Last update timestamp (ms) from the chain
    last_chain_update_ms: int = 0


# =============================================================================
# State store
# =============================================================================


class MarketStateStore:
    """In-memory keyed store of market mirrors."""

    def __init__(self) -> None:
        self._by_id: dict[str, Market] = {}
        self._lock = threading.Lock()

    def upsert(self, market: Market) -> None:
        with self._lock:
            self._by_id[market.market_id] = market

    def get(self, market_id: str) -> Market | None:
        with self._lock:
            return self._by_id.get(market_id)

    def all(self) -> list[Market]:
        with self._lock:
            return list(self._by_id.values())

    def by_status(self, status: MarketStatus) -> list[Market]:
        with self._lock:
            return [m for m in self._by_id.values() if m.status == status]

    def size(self) -> int:
        with self._lock:
            return len(self._by_id)

    def update_status(
        self,
        market_id: str,
        new_status: MarketStatus,
        *,
        update_ms: int | None = None,
    ) -> bool:
        with self._lock:
            market = self._by_id.get(market_id)
            if market is None:
                return False
            market.status = new_status
            market.last_chain_update_ms = update_ms or int(time.time() * 1000)
            return True


# =============================================================================
# Chain update ingestion
# =============================================================================


def ingest_chain_update(
    store: MarketStateStore,
    *,
    market_id: str,
    payload: dict[str, Any],
    received_at_ms: int | None = None,
) -> Market:
    """Apply a chain payload to the local store, returning the updated Market.

    Production wiring: the Solana subscription feed produces these
    updates; this is the adapter into the off-chain store.
    """
    received = received_at_ms or int(time.time() * 1000)
    existing = store.get(market_id) or Market(market_id=market_id)

    # Map known fields. Unknown fields land in metadata.
    for key, value in payload.items():
        if key == "kind":
            try:
                existing.kind = MarketKind(value)
            except ValueError:
                existing.metadata["unknown_kind"] = value
        elif key == "status":
            try:
                existing.status = MarketStatus(value)
            except ValueError:
                existing.metadata["unknown_status"] = value
        elif key in {
            "creator_pubkey", "subject_pubkey", "question",
            "formula_id", "resolver_pubkey", "privacy_mode",
            "resolvability_class",
        }:
            setattr(existing, key, value)
        elif key == "outcomes" and isinstance(value, list):
            existing.outcomes = list(value)
        elif key == "resolution_outcome":
            existing.resolution_outcome = int(value) if value is not None else None
        elif key in {
            "created_at_ms", "qualifies_at_ms", "closes_at_ms",
            "resolves_at_ms",
        }:
            setattr(existing, key, int(value))
        elif key in {"creator_stake_lamports", "pool_lamports"}:
            setattr(existing, key, int(value))
        else:
            existing.metadata[key] = value

    existing.last_chain_update_ms = received
    store.upsert(existing)
    return existing


# =============================================================================
# Module-level singleton
# =============================================================================


_STORE: MarketStateStore | None = None
_STORE_LOCK = threading.Lock()


def get_market_state_store() -> MarketStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = MarketStateStore()
        return _STORE


def set_market_state_store(s: MarketStateStore) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = s


__all__ = [
    "Market",
    "MarketKind",
    "MarketStateStore",
    "MarketStatus",
    "get_market_state_store",
    "ingest_chain_update",
    "set_market_state_store",
]
