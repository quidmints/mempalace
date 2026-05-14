"""
Device identity and capability advertisement.

Per R3 §2.2: cross-platform attestation chain. Identifies the device
running this mempalace instance (Android / iOS / Linux+TPM / Dev),
which capabilities it advertises (camera, mic, biometric, GPS, BLE,
…), and which models it can run locally.

Used at resolution time to decide whether the requested pipeline
fits the local capabilities, and for capability-conditioned routing
in the multiplexer.

Spec ref: R3 §2.2.
"""

from __future__ import annotations

import platform as host_platform
import threading
from dataclasses import dataclass, field
from typing import Any

from ..federate.attest import Platform


# =============================================================================
# Capability taxonomy
# =============================================================================


@dataclass
class DeviceCapability:
    """One advertised capability."""

    name: str
    version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceContext:
    """Snapshot of this device's identity + capabilities."""

    device_id: str                                  # stable per-install
    enrolled_pubkey_hex: str = ""
    platform: Platform = Platform.DEV
    os_version: str = ""
    hardware_model: str = ""
    capabilities: dict[str, DeviceCapability] = field(default_factory=dict)
    can_run_local_llm: bool = False
    can_run_local_embedder: bool = False
    has_secure_enclave: bool = False
    has_gpu: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def declares(self, capability_name: str) -> bool:
        return capability_name in self.capabilities

    def add_capability(self, cap: DeviceCapability) -> None:
        self.capabilities[cap.name] = cap


# =============================================================================
# Detection
# =============================================================================


def detect_platform() -> Platform:
    """Best-effort detection of the host platform."""
    sysname = host_platform.system().lower()
    if "linux" in sysname:
        return Platform.LINUX_TPM
    if "darwin" in sysname:
        # iOS daemons typically aren't running this code; mark macOS as DEV
        return Platform.DEV
    if "windows" in sysname:
        return Platform.DEV
    return Platform.DEV


def make_default_context(
    *,
    device_id: str,
    enrolled_pubkey_hex: str = "",
) -> DeviceContext:
    """Build a minimal DeviceContext for local development."""
    return DeviceContext(
        device_id=device_id,
        enrolled_pubkey_hex=enrolled_pubkey_hex,
        platform=detect_platform(),
        os_version=host_platform.platform(),
        hardware_model=host_platform.machine(),
    )


# =============================================================================
# Module-level singleton
# =============================================================================


_CTX: DeviceContext | None = None
_CTX_LOCK = threading.Lock()


def get_device_context() -> DeviceContext:
    global _CTX
    with _CTX_LOCK:
        if _CTX is None:
            _CTX = make_default_context(device_id="dev-local")
        return _CTX


def set_device_context(ctx: DeviceContext) -> None:
    global _CTX
    with _CTX_LOCK:
        _CTX = ctx


__all__ = [
    "DeviceCapability",
    "DeviceContext",
    "detect_platform",
    "get_device_context",
    "make_default_context",
    "set_device_context",
]
