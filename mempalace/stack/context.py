"""
Stack execution context.

Threaded through every Step.run() call. Carries:
  - inputs (set at stack init)
  - outputs (accumulated by step executions)
  - stance / consumer info
  - privacy mode (LOCAL_ONLY / SANDBOX / EXTERNAL)
  - attestation handle (collects per-step attestations)
  - log-offset anchor (snapshot consistency across steps)

The context is mutable across step executions but each step's own
inputs are read-only views; mutation goes through ctx.write_output().

Spec ref: R3 §1.1.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..schema.stance import Stance


class PrivacyMode(str, Enum):
    """How the stack execution is constrained.

    LOCAL_ONLY  : all inference must run in-process or in a hardware-bound
                 isolation environment. No external API calls.
    SANDBOX     : execution is in a temporary isolated environment with
                 explicit attestation; foreign palace data may be present.
    EXTERNAL    : external APIs allowed (web search, public LLMs).
    """

    LOCAL_ONLY = "local_only"
    SANDBOX = "sandbox"
    EXTERNAL = "external"


# =============================================================================
# Attestation handle
# =============================================================================


@dataclass
class AttestationRecord:
    """One per-step attestation."""

    step_id: str
    step_name: str
    model_id: str
    weights_hash: str
    input_hash: str
    output_hash: str
    timestamp_ms: int
    signature: str = ""  # populated by AttestedStep wrapper


@dataclass
class AttestationHandle:
    """Collects attestations across a stack execution."""

    records: list[AttestationRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, rec: AttestationRecord) -> None:
        with self._lock:
            self.records.append(rec)

    def all(self) -> list[AttestationRecord]:
        with self._lock:
            return list(self.records)

    def is_empty(self) -> bool:
        with self._lock:
            return len(self.records) == 0


# =============================================================================
# Context
# =============================================================================


@dataclass
class StackContext:
    """Mutable context threaded through Stack.execute()."""

    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    stance: Stance | None = None
    privacy_mode: PrivacyMode = PrivacyMode.EXTERNAL
    attestation: AttestationHandle = field(default_factory=AttestationHandle)
    log_offset_anchor: int = 0
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    consumer_id: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- typed helpers ------------------------------------------------------

    def get_input(self, key: str, default: Any = None) -> Any:
        return self.inputs.get(key, default)

    def get_output(self, key: str, default: Any = None) -> Any:
        return self.outputs.get(key, default)

    def write_output(self, key: str, value: Any) -> None:
        self.outputs[key] = value

    def merge_outputs(self, kvs: dict[str, Any]) -> None:
        self.outputs.update(kvs)

    # ---- privacy helpers ----------------------------------------------------

    def is_external_allowed(self) -> bool:
        return self.privacy_mode == PrivacyMode.EXTERNAL

    def requires_attestation(self) -> bool:
        return self.privacy_mode in (PrivacyMode.LOCAL_ONLY, PrivacyMode.SANDBOX)


__all__ = [
    "AttestationHandle",
    "AttestationRecord",
    "PrivacyMode",
    "StackContext",
]
