"""
QVAC LLM client.

Thin wrapper over the sidecar's `/v1/chat/completions`. The existing
`mempalace/llm_client.py` (kept from mempalace-develop) is OpenAI-shaped
and works against the sidecar with only a base-URL config change — no
code changes needed. This module provides a typed wrapper for the case
where callers want explicit access to the local-inference path rather
than going through `llm_client.py`.

# Why both paths

The OpenAI-compatible API means `llm_client.py` works against:
  - Anthropic's API
  - OpenAI's API
  - The QVAC sidecar
  - Anyone else with OpenAI-compatible v1

Pointing `llm_client.py.base_url` at `http://127.0.0.1:11434/v1` makes
the entire substrate use the local LLM with no further changes. That's
the headline integration.

This module exists so that:
  - Code that wants typed access (`QvacLLMResponse` with token counts,
    stop reasons) without parsing the raw dict can use it.
  - Local-only call sites can be explicit about the fact that they
    require the local path, without depending on `llm_client.py`'s
    runtime config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mempalace.qvac.client import QvacClient, get_default_client


@dataclass
class QvacChatMessage:
    role: str    # "system" | "user" | "assistant"
    content: str


@dataclass
class QvacLLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


class QvacLLMClient:
    """Local LLM completion via the sidecar.

    Defaults to `qvac-local-llm` from sidecar config. Override per-call
    by passing a different `model`.
    """

    def __init__(
        self,
        client: QvacClient | None = None,
        *,
        default_max_tokens: int = 512,
        default_temperature: float = 0.7,
    ) -> None:
        self._client = client or get_default_client()
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

    def chat(
        self,
        messages: list[QvacChatMessage] | list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> QvacLLMResponse:
        normalized: list[dict[str, str]] = []
        for m in messages:
            if isinstance(m, QvacChatMessage):
                normalized.append({"role": m.role, "content": m.content})
            else:
                normalized.append({"role": m["role"], "content": m["content"]})
        resp = self._client.chat_completion(
            messages=normalized,
            model=model,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            temperature=temperature if temperature is not None else self._default_temperature,
        )
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = resp.get("usage") or {}
        return QvacLLMResponse(
            text=msg.get("content", ""),
            model=resp.get("model", model or self._client.config.llm_model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=choice.get("finish_reason", "stop"),
            raw=resp,
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> QvacLLMResponse:
        """Single-turn convenience wrapper."""
        messages: list[QvacChatMessage] = []
        if system:
            messages.append(QvacChatMessage(role="system", content=system))
        messages.append(QvacChatMessage(role="user", content=prompt))
        return self.chat(messages, **kwargs)
