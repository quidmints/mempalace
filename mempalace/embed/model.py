"""
Locally-trained embedding model.

Per Part 7.3 / R3: the embedding model is locally trained, not ChromaDB's
default. Two roles:

  - Cold-start: until enough drawers have been written, fall back to a
    frontier-quality embedding (small bundled model — sentence-transformers
    or equivalent). Cold-start outputs are tagged with the cold-start model
    ID so reconciliation can re-embed when the local model trains.

  - Trained mode: once N drawers and feedback signals are available, fine-
    tune the local model on contrastive pairs derived from drawers (positives:
    drawers in same recurrence cluster; negatives: random distant drawers)
    plus feedback-recorded events that imply similarity / dissimilarity.

The model emits attestation events at load and inference time per R3 §1.4.

This module is intentionally a stub of the runtime training loop — the
actual contrastive trainer goes in batch 5 (features/) and batch 9 (miner/);
this file establishes the interface and the cold-start fallback.

Spec ref: Part 4, Part 7.3, R3 §1.4
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..log.client import LogClient, get_default_client
from ..schema.events import ModelInferenceCompleted, ModelLoaded


# =============================================================================
# Model metadata
# =============================================================================

@dataclass
class ModelInfo:
    """Identifies a specific trained-model snapshot.

    `weights_hash` is the SHA-256 of the model's weights file; used for
    cross-palace federation comparisons (two palaces can compare embeddings
    only when their `weights_hash` matches).
    """
    model_id: str
    version: str
    weights_hash: str
    dimension: int


# =============================================================================
# Embedder protocol
# =============================================================================

class Embedder(Protocol):
    """An embedder takes text → vector. Specific implementations may add
    inference-attestation steps; the protocol is minimal."""

    def info(self) -> ModelInfo: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


# =============================================================================
# Cold-start embedder
#
# A deterministic, content-addressed pseudo-embedder used until a real local
# model has trained. Hashes the input into a fixed-dimension vector. Useful
# for tests; production deployments swap in a real bundled model (e.g.,
# sentence-transformers all-MiniLM-L6-v2 quantized).
# =============================================================================

class _ColdStartEmbedder:
    """Deterministic placeholder. NOT for production retrieval quality.

    Produces vectors in [-1, 1] derived from SHA-256 of input. Same input
    always yields same output. Different inputs are pseudo-orthogonal in
    expectation. Used until the trained model is available.
    """

    DIM: int = 256

    def __init__(self) -> None:
        self._info = ModelInfo(
            model_id="cold_start",
            version="0.1.0",
            weights_hash="0" * 64,
            dimension=self.DIM,
        )

    def info(self) -> ModelInfo:
        return self._info

    def embed(self, text: str) -> list[float]:
        # Deterministic hash-based projection. Splits a SHA-256 hash across
        # the dimensions, mapped to [-1, 1].
        digest = hashlib.sha512(text.encode("utf-8")).digest()
        # Stretch via repeated hashing for higher dimensions.
        out: list[float] = []
        block = digest
        while len(out) < self.DIM:
            for byte in block:
                out.append((byte / 127.5) - 1.0)
                if len(out) >= self.DIM:
                    break
            block = hashlib.sha512(block).digest()
        return out[: self.DIM]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


# =============================================================================
# Embedding service
#
# Front-facing module. Holds the current embedder; provides a thin wrapper
# that emits attestation events and content-addresses each embedding so the
# log records what was embedded with what model.
# =============================================================================

class EmbeddingService:
    """Single front for embedding requests across the daemon.

    Wraps the active embedder, emits attestation events, and tracks the
    last-known model. Hot-swappable when the local model is retrained.
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        client: LogClient | None = None,
    ) -> None:
        self._embedder: Embedder = embedder or _ColdStartEmbedder()
        self._client = client or get_default_client()
        self._lock = threading.Lock()

    def info(self) -> ModelInfo:
        with self._lock:
            return self._embedder.info()

    def set_embedder(self, embedder: Embedder) -> None:
        """Swap the active embedder. Emits a `model_loaded` event."""
        with self._lock:
            self._embedder = embedder
            info = embedder.info()
        self._client.append(ModelLoaded(
            model_id=info.model_id,
            weights_hash=info.weights_hash,
            signing_pubkey="",
            enrollment_signature="",
        ))

    def embed(self, text: str, *, step_id: str = "embed") -> list[float]:
        """Embed a single text, emitting per-inference attestation."""
        with self._lock:
            embedder = self._embedder
            info = embedder.info()
        vector = embedder.embed(text)
        self._emit_attestation(info, text, vector, step_id)
        return vector

    def embed_batch(self, texts: list[str], *, step_id: str = "embed_batch") -> list[list[float]]:
        with self._lock:
            embedder = self._embedder
            info = embedder.info()
        vectors = embedder.embed_batch(texts)
        if texts:
            # Attestation per batch — input/output hashes are the SHA of the
            # joined batches; cheaper than per-item attestation for normal
            # batched workloads.
            joined_in = "\x00".join(texts).encode("utf-8")
            joined_out = b"".join(
                bytes((int((v + 1.0) * 127.5) % 256 for v in vec))
                for vec in vectors
            )
            self._emit_attestation_raw(info, joined_in, joined_out, step_id)
        return vectors

    # =========================================================================
    # Internal: attestation event emission
    # =========================================================================

    def _emit_attestation(
        self, info: ModelInfo, text: str, vector: list[float], step_id: str,
    ) -> None:
        in_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Output hash captures the vector deterministically.
        out_bytes = bytes((int((v + 1.0) * 127.5) % 256 for v in vector))
        out_hash = hashlib.sha256(out_bytes).hexdigest()
        self._client.append(ModelInferenceCompleted(
            model_id=info.model_id,
            weights_hash=info.weights_hash,
            step_id=step_id,
            input_hash=in_hash,
            output_hash=out_hash,
            attestation_signature="",  # set by AttestedStep at runtime; placeholder
        ))

    def _emit_attestation_raw(
        self, info: ModelInfo, in_bytes: bytes, out_bytes: bytes, step_id: str,
    ) -> None:
        self._client.append(ModelInferenceCompleted(
            model_id=info.model_id,
            weights_hash=info.weights_hash,
            step_id=step_id,
            input_hash=hashlib.sha256(in_bytes).hexdigest(),
            output_hash=hashlib.sha256(out_bytes).hexdigest(),
            attestation_signature="",
        ))


# =============================================================================
# Module-level singleton
# =============================================================================

_service: EmbeddingService | None = None
_service_lock = threading.Lock()


def get_default_service() -> EmbeddingService:
    global _service
    with _service_lock:
        if _service is None:
            _service = EmbeddingService()
        return _service


def set_default_service(service: EmbeddingService) -> None:
    global _service
    with _service_lock:
        _service = service
