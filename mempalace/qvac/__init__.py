"""
QVAC client adapters.

Bridges mempalace's Python substrate to a local QVAC sidecar running on
127.0.0.1:11434 (or wherever `MEMPALACE_QVAC_BASE_URL` points). The sidecar
hosts QVAC's local-inference engines and exposes OpenAI-compatible endpoints
plus a Hyperdrive fetch helper.

# Why a separate module

`mempalace/embed/`, `mempalace/llm_client.py` (in the kept-files set per
MEMPALACE_FILE_ACCOUNTING.md), and `mempalace/diary_ingest.py` (also kept)
all have their own backend protocols. This module supplies concrete
implementations of those protocols that talk to the QVAC sidecar.

# Usage

    from mempalace.qvac import QvacEmbedder, QvacLLMClient, QvacOCR
    from mempalace.embed.model import set_default_service, EmbeddingService

    set_default_service(EmbeddingService(embedder=QvacEmbedder()))

The QVAC sidecar must be running. See `qvac-sidecar/README.md`. If the
sidecar isn't running, every QVAC call raises `QvacUnavailable` —
upstream code is responsible for deciding whether to fall back or fail.

# Configuration

Reads these env vars (defaults shown):

  MEMPALACE_QVAC_BASE_URL    http://127.0.0.1:11434
  MEMPALACE_QVAC_API_TOKEN   (none — sets Authorization header if present)
  MEMPALACE_QVAC_TIMEOUT     60.0  (seconds)
  MEMPALACE_QVAC_LLM_MODEL   qvac-local-llm
  MEMPALACE_QVAC_EMBED_MODEL qvac-local-embed
"""

from __future__ import annotations

from mempalace.qvac.client import (
    QvacClient,
    QvacConfig,
    QvacError,
    QvacUnavailable,
    get_default_client,
    set_default_client,
)
from mempalace.qvac.embedder import QvacEmbedder
from mempalace.qvac.hyperdrive import (
    HyperdriveFetcher,
    fetch_model_via_sidecar,
    fetch_model_via_cli,
)
from mempalace.qvac.llm import (
    QvacChatMessage,
    QvacLLMClient,
    QvacLLMResponse,
)
from mempalace.qvac.ocr import QvacOCR, OCRResult

__all__ = [
    "HyperdriveFetcher",
    "OCRResult",
    "QvacChatMessage",
    "QvacClient",
    "QvacConfig",
    "QvacEmbedder",
    "QvacError",
    "QvacLLMClient",
    "QvacLLMResponse",
    "QvacOCR",
    "QvacUnavailable",
    "fetch_model_via_cli",
    "fetch_model_via_sidecar",
    "get_default_client",
    "set_default_client",
]
