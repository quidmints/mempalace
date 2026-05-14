"""
QVAC-backed Embedder.

Implements the `Embedder` Protocol from `mempalace/embed/model.py` against
QVAC's `lib-infer-llamacpp-embed` via the sidecar's `/v1/embeddings`
endpoint. Drop-in replacement for the cold-start embedder.

# Usage

    from mempalace.embed.model import EmbeddingService, set_default_service
    from mempalace.qvac import QvacEmbedder

    set_default_service(EmbeddingService(embedder=QvacEmbedder()))

The embedder's `info().weights_hash` comes from the sidecar's `/v1/models`
response when available; falls back to a placeholder so federation
comparisons (which gate cross-palace embedding compatibility on
matching `weights_hash`) at least fail loudly rather than silently.
"""

from __future__ import annotations

import logging
from typing import Any

# Lazy import — embed.model imports schema.events which imports the world.
# Keep this module importable without pulling all that in.
def _get_model_info_cls():
    from mempalace.embed.model import ModelInfo
    return ModelInfo


from mempalace.qvac.client import QvacClient, get_default_client

logger = logging.getLogger(__name__)


class QvacEmbedder:
    """Embedder that delegates to the QVAC sidecar.

    Caches model info from `/v1/models` after first call; embedding model
    identity is resolved once per process. If the sidecar changes models
    underneath, callers must reinstantiate.
    """

    def __init__(
        self,
        client: QvacClient | None = None,
        *,
        model: str | None = None,
        dimension_override: int | None = None,
    ) -> None:
        self._client = client or get_default_client()
        self._model = model or self._client.config.embed_model
        self._info_cache: Any | None = None
        # The QVAC sidecar doesn't currently expose embedding dimension via
        # /v1/models. We learn it from the first embedding call and cache.
        # Caller can override if they know it ahead of time.
        self._dimension = dimension_override

    def info(self):
        if self._info_cache is not None:
            return self._info_cache
        ModelInfo = _get_model_info_cls()
        weights_hash = "qvac-sidecar-unknown"
        try:
            models = self._client.list_models()
            entry = next(
                (m for m in models.get("data", []) if m.get("id") == self._model),
                None,
            )
            if entry:
                # Use the model path as a stable identifier when we don't
                # have a real weights hash. Reconciliation can detect a
                # mismatch by string comparison.
                weights_hash = f"qvac-sidecar:{entry.get('path', self._model)}"
        except Exception as e:
            logger.warning(
                "QvacEmbedder.info: /v1/models failed (%s); using placeholder", e,
            )
        dim = self._dimension or 384  # MiniLM default; corrected after first embed
        self._info_cache = ModelInfo(
            model_id=self._model,
            version="qvac-sidecar",
            weights_hash=weights_hash,
            dimension=dim,
        )
        return self._info_cache

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings(text, model=self._model)
        vectors = self._extract_vectors(resp)
        if not vectors:
            raise RuntimeError("QvacEmbedder.embed: empty response from sidecar")
        return vectors[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings(texts, model=self._model)
        return self._extract_vectors(resp)

    # ---------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------

    def _extract_vectors(self, resp: dict) -> list[list[float]]:
        data = resp.get("data") or []
        # OpenAI-compatible response: data is a list of {index, embedding, object}
        # sorted by index. The sidecar guarantees order; we re-sort defensively.
        ordered = sorted(data, key=lambda d: int(d.get("index", 0)))
        vectors = [list(d.get("embedding") or []) for d in ordered]
        # Update cached dimension if we now know it
        if vectors and self._info_cache is not None:
            actual_dim = len(vectors[0])
            if actual_dim and self._info_cache.dimension != actual_dim:
                ModelInfo = _get_model_info_cls()
                self._info_cache = ModelInfo(
                    model_id=self._info_cache.model_id,
                    version=self._info_cache.version,
                    weights_hash=self._info_cache.weights_hash,
                    dimension=actual_dim,
                )
        return vectors
