"""
Model registry.

Per R3 §2.2 / §1.4: model metadata, version tracking, weights-hash
registration, model-class taxonomy, attestation-key bindings, signed
code verification. Powers the per-inference attestation chain
(§7.6 / §1.4) and lets the resolver gate inference on enrolled keys.

Spec ref: R3 §1.4, §2.2.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import ModelLoaded
from ..schema.identifiers import make_event_id_log


# =============================================================================
# Model class taxonomy
# =============================================================================


class ModelClass(str, Enum):
    """Per R3 §1.4: model-class taxonomy."""

    PUBLIC_LLM = "public_llm"                # frontier API
    LOCAL_FINETUNE = "local_finetune"        # locally fine-tuned variant
    EMBEDDER = "embedder"
    CLASSIFIER = "classifier"
    RANKER = "ranker"
    EXTRACTOR = "extractor"


@dataclass
class ModelEntry:
    """One registered model."""

    model_id: str
    model_class: ModelClass
    weights_hash: str                            # claimed weights hash (e.g. blake2b)
    version: str
    signing_pubkey_hex: str = ""                 # bound attestation key
    enrollment_signature_hex: str = ""           # signed-by-device-root
    code_signature_hex: str = ""                 # signed code-binary hash
    enrolled_at_ms: int = 0
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Registry
# =============================================================================


class ModelRegistry:
    """Keyed model registry. Loading a model emits `model_loaded`."""

    def __init__(self, *, log: LogClient | None = None) -> None:
        self._by_id: dict[str, ModelEntry] = {}
        self._revoked: set[str] = set()
        self._lock = threading.Lock()
        self._log = log

    def register(self, entry: ModelEntry, *, emit_event: bool = True) -> ModelEntry:
        with self._lock:
            if not entry.enrolled_at_ms:
                entry.enrolled_at_ms = int(time.time() * 1000)
            self._by_id[entry.model_id] = entry
        if emit_event:
            self._emit_model_loaded(entry)
        return entry

    def revoke(self, model_id: str) -> bool:
        with self._lock:
            if model_id not in self._by_id:
                return False
            self._revoked.add(model_id)
            return True

    def is_revoked(self, model_id: str) -> bool:
        with self._lock:
            return model_id in self._revoked

    def get(self, model_id: str) -> ModelEntry | None:
        with self._lock:
            return self._by_id.get(model_id)

    def all(self) -> list[ModelEntry]:
        with self._lock:
            return list(self._by_id.values())

    def by_class(self, model_class: ModelClass) -> list[ModelEntry]:
        with self._lock:
            return [
                e for e in self._by_id.values()
                if e.model_class == model_class
                and e.model_id not in self._revoked
            ]

    def verify_code_signature(
        self,
        model_id: str,
        *,
        expected_code_hash: str,
    ) -> bool:
        """Naive code-signature check: matches the recorded hash.

        Production wiring uses the device-root key to verify the
        recorded signature. Here we just check the hash matches.
        """
        entry = self.get(model_id)
        if entry is None or entry.model_id in self._revoked:
            return False
        return entry.code_signature_hex == expected_code_hash

    def _emit_model_loaded(self, entry: ModelEntry) -> None:
        log = self._log or get_default_client()
        now = entry.enrolled_at_ms or int(time.time() * 1000)
        log.append(ModelLoaded(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="resolve.model_registry",
            model_id=entry.model_id,
            weights_hash=entry.weights_hash,
            signing_pubkey=entry.signing_pubkey_hex,
            enrollment_signature=entry.enrollment_signature_hex,
        ))


# =============================================================================
# Module-level singleton
# =============================================================================


_REGISTRY: ModelRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_model_registry() -> ModelRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ModelRegistry()
        return _REGISTRY


def set_model_registry(r: ModelRegistry) -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = r


__all__ = [
    "ModelClass",
    "ModelEntry",
    "ModelRegistry",
    "get_model_registry",
    "set_model_registry",
]
