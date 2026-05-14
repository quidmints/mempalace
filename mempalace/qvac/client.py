"""
HTTP client for the QVAC sidecar.

Thin wrapper around `urllib` so this module has zero non-stdlib runtime
dependencies. Callers that prefer httpx/requests can subclass `QvacClient`
and override `_request`.

# Why urllib instead of requests/httpx

mempalace's keep-files list (MEMPALACE_FILE_ACCOUNTING.md) already pulls
in `requests` via the existing `llm_client.py`, so dep cost would be near
zero — but writing the sidecar transport on stdlib means this module's
tests have no network mocking pain and the module can be imported in
environments where pip-installed deps are restricted (e.g., the
StrongBox-attested launcher path on cloud-box). Trade-off is verbose
error handling here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Exceptions
# =============================================================================

class QvacError(Exception):
    """Base class for QVAC client errors."""


class QvacUnavailable(QvacError):
    """The sidecar is unreachable. Caller decides whether to fall back."""


class QvacBadRequest(QvacError):
    """Sidecar rejected the request (4xx). Usually a programming error."""


class QvacServerError(QvacError):
    """Sidecar returned a 5xx. Usually a model-loading or runtime failure."""


# =============================================================================
# Config
# =============================================================================

@dataclass
class QvacConfig:
    """Sidecar connection parameters. Read from env by default."""
    base_url: str = "http://127.0.0.1:11434"
    api_token: str | None = None
    timeout_seconds: float = 60.0
    llm_model: str = "qvac-local-llm"
    embed_model: str = "qvac-local-embed"

    @classmethod
    def from_env(cls) -> "QvacConfig":
        return cls(
            base_url=os.environ.get(
                "MEMPALACE_QVAC_BASE_URL", "http://127.0.0.1:11434",
            ).rstrip("/"),
            api_token=os.environ.get("MEMPALACE_QVAC_API_TOKEN") or None,
            timeout_seconds=float(os.environ.get("MEMPALACE_QVAC_TIMEOUT", "60.0")),
            llm_model=os.environ.get(
                "MEMPALACE_QVAC_LLM_MODEL", "qvac-local-llm",
            ),
            embed_model=os.environ.get(
                "MEMPALACE_QVAC_EMBED_MODEL", "qvac-local-embed",
            ),
        )


# =============================================================================
# Client
# =============================================================================

class QvacClient:
    """Low-level HTTP client.

    Methods correspond directly to sidecar endpoints; higher-level wrappers
    (`QvacEmbedder`, `QvacLLMClient`, `QvacOCR`, `HyperdriveFetcher`) compose
    on top of this.
    """

    def __init__(self, config: QvacConfig | None = None) -> None:
        self.config = config or QvacConfig.from_env()

    # ---------------------------------------------------------------------
    # Transport
    # ---------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.config.base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url=url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        if self.config.api_token:
            req.add_header("Authorization", f"Bearer {self.config.api_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if 400 <= e.code < 500:
                raise QvacBadRequest(
                    f"{method} {path}: {e.code} {e.reason} — {err_body}",
                ) from e
            raise QvacServerError(
                f"{method} {path}: {e.code} {e.reason} — {err_body}",
            ) from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise QvacUnavailable(
                f"{method} {path}: sidecar unreachable — {e}",
            ) from e

    # ---------------------------------------------------------------------
    # Endpoint methods (thin wrappers)
    # ---------------------------------------------------------------------

    def healthz(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def list_models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models")

    def embeddings(
        self, input: str | list[str], *, model: str | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/embeddings", {
            "input": input,
            "model": model or self.config.embed_model,
        })

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        return self._request("POST", "/v1/chat/completions", {
            "messages": messages,
            "model": model or self.config.llm_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })

    def ocr(self, image_base64: str, *, language: str = "en") -> dict[str, Any]:
        return self._request("POST", "/v1/ocr", {
            "image_base64": image_base64,
            "language": language,
        })

    def hyperdrive_fetch(self, drive_key: str, file_path: str) -> dict[str, Any]:
        return self._request("POST", "/v1/hyperdrive/fetch", {
            "drive_key": drive_key,
            "file_path": file_path,
        })

    def is_reachable(self) -> bool:
        """Cheap connection check. False if the sidecar isn't responding."""
        try:
            self.healthz()
            return True
        except QvacError:
            return False


# =============================================================================
# Default singleton
# =============================================================================

_default_client: QvacClient | None = None
_default_lock = threading.Lock()


def get_default_client() -> QvacClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = QvacClient()
        return _default_client


def set_default_client(client: QvacClient) -> None:
    global _default_client
    with _default_lock:
        _default_client = client
