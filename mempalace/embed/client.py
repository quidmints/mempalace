"""
ChromaDB-facing client.

Stores embeddings (vector + drawer_id + minimal metadata) and provides ANN
search. ChromaDB is just a backend; the source of truth is the log. The
reconciliation sweeper (`reconcile.py`) keeps ChromaDB consistent with the
log even after restarts or partial writes.

In dev / test environments we use a pure-Python backend that mirrors the
ChromaDB interface enough to support search workflows without the heavy
dependency. Production swaps in real ChromaDB.

Spec ref: Part 4
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Iterable, Protocol


# =============================================================================
# Vector backend protocol
# =============================================================================

@dataclass
class VectorRecord:
    """One record in the vector store."""
    drawer_id: str
    vector: list[float]
    metadata: dict[str, str | int | float] = field(default_factory=dict)


@dataclass
class SearchResult:
    drawer_id: str
    score: float            # cosine similarity in [-1, 1]
    metadata: dict[str, str | int | float]


class VectorBackend(Protocol):
    def upsert(self, records: Iterable[VectorRecord]) -> None: ...
    def delete(self, drawer_ids: list[str]) -> None: ...
    def query(self, vector: list[float], k: int) -> list[SearchResult]: ...
    def has(self, drawer_id: str) -> bool: ...
    def count(self) -> int: ...


# =============================================================================
# In-memory backend (for tests and cold-start)
# =============================================================================

class InMemoryBackend:
    """Pure-Python backend mirroring the parts of ChromaDB we use.

    Linear-scan ANN; fine for tests and cold-start. Production swaps in
    real ChromaDB which uses HNSW or IVF for sublinear search.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, VectorRecord] = {}

    def upsert(self, records: Iterable[VectorRecord]) -> None:
        with self._lock:
            for r in records:
                self._records[r.drawer_id] = r

    def delete(self, drawer_ids: list[str]) -> None:
        with self._lock:
            for did in drawer_ids:
                self._records.pop(did, None)

    def query(self, vector: list[float], k: int) -> list[SearchResult]:
        with self._lock:
            scored = [
                SearchResult(
                    drawer_id=r.drawer_id,
                    score=_cosine(vector, r.vector),
                    metadata=dict(r.metadata),
                )
                for r in self._records.values()
            ]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:k]

    def has(self, drawer_id: str) -> bool:
        with self._lock:
            return drawer_id in self._records

    def count(self) -> int:
        with self._lock:
            return len(self._records)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# =============================================================================
# Embedding store
#
# Front-facing API for the rest of the codebase. Wraps the backend so callers
# don't depend on ChromaDB vs in-memory vs anything else.
# =============================================================================

class EmbeddingStore:
    def __init__(self, backend: VectorBackend | None = None) -> None:
        self._backend: VectorBackend = backend or InMemoryBackend()

    def upsert(self, drawer_id: str, vector: list[float], metadata: dict | None = None) -> None:
        self._backend.upsert([VectorRecord(
            drawer_id=drawer_id,
            vector=list(vector),
            metadata=metadata or {},
        )])

    def upsert_batch(self, records: list[VectorRecord]) -> None:
        self._backend.upsert(records)

    def delete(self, drawer_ids: list[str]) -> None:
        self._backend.delete(drawer_ids)

    def query(self, vector: list[float], k: int = 50) -> list[SearchResult]:
        return self._backend.query(vector, k)

    def has(self, drawer_id: str) -> bool:
        return self._backend.has(drawer_id)

    def count(self) -> int:
        return self._backend.count()

    def set_backend(self, backend: VectorBackend) -> None:
        self._backend = backend


# =============================================================================
# Module-level singleton
# =============================================================================

_store: EmbeddingStore | None = None
_store_lock = threading.Lock()


def get_default_store() -> EmbeddingStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = EmbeddingStore()
        return _store


def set_default_store(store: EmbeddingStore) -> None:
    global _store
    with _store_lock:
        _store = store
