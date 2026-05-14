"""
Sandbox lifecycle.

Per R3 §7.1: federation matching runs inside a sandbox — an isolated
execution environment that:

  1. Provisions a per-session boundary (process/container/enclave)
  2. Loads the foreign palace's encrypted slice INSIDE the boundary
  3. Decrypts using session keys (which never leave the boundary)
  4. Runs the matching steps (LOCAL_ONLY privacy mode)
  5. Emits findings (the only data that exits the boundary)
  6. Tears down: zeros memory, destroys session keys, removes scratch

The sandbox is the actual security boundary; everything else is
defense-in-depth around it.

This module ships the sandbox lifecycle interface. The actual isolation
mechanism (gVisor, nsjail, hardware enclave) is platform-specific;
production deployment swaps in a real sandbox runner.

Spec ref: R3 §7.1.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    ForeignPalaceLoaded,
    SandboxProvisioned,
    SandboxTornDown,
)
from ..schema.identifiers import make_event_id_log, make_sandbox_id
from .session_keys import get_session_key_manager


class SandboxStatus(str, Enum):
    PROVISIONED = "provisioned"
    LOADED = "loaded"        # foreign slice decrypted inside
    RUNNING = "running"
    COMPLETED = "completed"
    TORN_DOWN = "torn_down"
    FAILED = "failed"


@dataclass
class SandboxState:
    """Live state of one sandbox."""

    sandbox_id: str
    session_key_id: str
    foreign_palace_pubkey: str
    status: SandboxStatus
    provisioned_at_ms: int
    loaded_at_ms: int | None = None
    completed_at_ms: int | None = None
    torn_down_at_ms: int | None = None
    findings_emitted: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SandboxManager:
    """Manages the lifecycle of all federation sandboxes."""

    def __init__(self, *, log_client: LogClient | None = None) -> None:
        self._client = log_client or get_default_client()
        self._sessions: dict[str, SandboxState] = {}
        self._lock = threading.Lock()

    # ---- provision ---------------------------------------------------------

    def provision(
        self,
        *,
        foreign_palace_pubkey: str,
        match_request_id: str = "",
    ) -> SandboxState:
        """Provision a new sandbox. Returns the state record.

        Opens a distributed-lifecycle batch keyed on `sandbox_id`. The
        batch closes when `tear_down` is called. If the process crashes
        between provision and teardown, recovery will see an open batch
        for the federate.sandbox consumer.
        """
        skm = get_session_key_manager()
        session_key_id = skm.generate()
        sandbox_id = make_sandbox_id()
        now = int(time.time() * 1000)
        state = SandboxState(
            sandbox_id=sandbox_id,
            session_key_id=session_key_id,
            foreign_palace_pubkey=foreign_palace_pubkey,
            status=SandboxStatus.PROVISIONED,
            provisioned_at_ms=now,
            metadata={"match_request_id": match_request_id},
        )
        with self._lock:
            self._sessions[sandbox_id] = state

        # Open the distributed-lifecycle batch keyed on sandbox_id.
        # All sandbox events for this sandbox carry batch_id=sandbox_id.
        self._client.open_batch(
            "federate.sandbox",
            sandbox_id,
            input_summary={
                "foreign_palace_pubkey": foreign_palace_pubkey,
                "match_request_id": match_request_id,
            },
            actor="federate.sandbox",
        )
        self._client.append(SandboxProvisioned(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="federate.sandbox",
            sandbox_id=sandbox_id,
            match_id=match_request_id,
            privacy_mode="local_only",
            batch_id=sandbox_id,
        ))
        return state

    # ---- load foreign slice -------------------------------------------------

    def load_foreign_slice(
        self,
        sandbox_id: str,
        *,
        slice_blob: bytes,
        slice_signature: bytes,
        slice_summary: dict[str, Any],
    ) -> bool:
        """Mark the foreign slice loaded into this sandbox.

        In production: decrypt slice_blob inside the boundary using the
        session key + verify slice_signature. Here we just record the
        state transition.

        Returns True on successful load.
        """
        with self._lock:
            state = self._sessions.get(sandbox_id)
            if state is None or state.status != SandboxStatus.PROVISIONED:
                return False
            now = int(time.time() * 1000)
            state.status = SandboxStatus.LOADED
            state.loaded_at_ms = now
            state.metadata["slice_byte_count"] = len(slice_blob)
            state.metadata.update(slice_summary)

        self._client.append(ForeignPalaceLoaded(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="federate.sandbox",
            sandbox_id=sandbox_id,
            foreign_palace_id=state.foreign_palace_pubkey,
            slice_size_bytes=len(slice_blob),
            layer=int(slice_summary.get("layer", 0)),
            batch_id=sandbox_id,
        ))
        return True

    # ---- mark execution states ---------------------------------------------

    def mark_running(self, sandbox_id: str) -> None:
        with self._lock:
            state = self._sessions.get(sandbox_id)
            if state is not None:
                state.status = SandboxStatus.RUNNING

    def mark_completed(self, sandbox_id: str, *, findings_emitted: int = 0) -> None:
        with self._lock:
            state = self._sessions.get(sandbox_id)
            if state is not None:
                state.status = SandboxStatus.COMPLETED
                state.completed_at_ms = int(time.time() * 1000)
                state.findings_emitted = findings_emitted

    # ---- teardown ----------------------------------------------------------

    def tear_down(self, sandbox_id: str, *, reason: str = "completed") -> bool:
        """Tear down the sandbox: destroy session keys, mark torn down,
        and close the distributed-lifecycle batch."""
        with self._lock:
            state = self._sessions.get(sandbox_id)
            if state is None or state.status == SandboxStatus.TORN_DOWN:
                return False
            now = int(time.time() * 1000)
            state.status = SandboxStatus.TORN_DOWN
            state.torn_down_at_ms = now

        # Destroy session key — zeros private bytes
        get_session_key_manager().destroy(state.session_key_id)

        self._client.append(SandboxTornDown(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="federate.sandbox",
            sandbox_id=sandbox_id,
            keys_destroyed=True,
            batch_id=sandbox_id,
        ))
        # Close the distributed-lifecycle batch.
        # `reason="completed"` → BatchCommitted; anything else → BatchAborted.
        if reason == "completed":
            self._client.close_batch(
                "federate.sandbox", sandbox_id, actor="federate.sandbox",
            )
        else:
            self._client.abort_batch(
                "federate.sandbox", sandbox_id,
                reason=reason, actor="federate.sandbox",
            )
        return True

    # ---- query -------------------------------------------------------------

    def get_state(self, sandbox_id: str) -> SandboxState | None:
        with self._lock:
            return self._sessions.get(sandbox_id)

    def list_active(self) -> list[SandboxState]:
        with self._lock:
            return [
                s for s in self._sessions.values()
                if s.status not in (SandboxStatus.TORN_DOWN, SandboxStatus.FAILED)
            ]


# =============================================================================
# Module-level singleton
# =============================================================================


_MANAGER: SandboxManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_sandbox_manager() -> SandboxManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = SandboxManager()
        return _MANAGER


def set_sandbox_manager(mgr: SandboxManager) -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = mgr


__all__ = [
    "SandboxManager",
    "SandboxState",
    "SandboxStatus",
    "get_sandbox_manager",
    "set_sandbox_manager",
]
