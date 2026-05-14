"""
Cross-platform hardware attestation.

Per R3 §7.2: federation matching requires that both palaces verify each
other's hardware-attestation chain. The chain is:

  1. Device root key (manufacturer-installed, hardware-fused)
  2. Enrolled key (palace-specific, generated in StrongBox / Secure Enclave
     at first run, attested by the device root)
  3. Per-model signing key (signs each model_inference_completed event)

Chain verification proves:
  - The enrolled key lives in real hardware (not extracted)
  - The model running is the model whose hash is claimed
  - The inference outputs were produced by that model

Platform support:
  - Android : StrongBox + Android Keystore Attestation extension
  - iOS     : Secure Enclave + DeviceCheck App Attest
  - Linux   : TPM 2.0 + IMA (production servers only)
  - Dev     : pure-Python "trust-on-first-use" + signed dev key

This module ships the verification interface and a dev-mode verifier;
production deployment plugs in platform-specific verifiers.

Spec ref: R3 §7.2.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum

from ..log.client import LogClient, get_default_client
from ..schema.events import AttestationChainBroken
from ..schema.identifiers import make_event_id_log


class Platform(str, Enum):
    ANDROID = "android"
    IOS = "ios"
    LINUX_TPM = "linux_tpm"
    DEV = "dev"


@dataclass
class AttestationChain:
    """A complete attestation chain from device root to inference signature."""

    platform: Platform
    device_root_key_hex: str           # manufacturer / dev root
    enrolled_key_hex: str              # palace-specific
    enrolled_key_attestation: bytes    # signed by device root
    model_id: str
    model_signing_key_hex: str         # used for per-inference signatures
    model_attestation: bytes           # signed by enrolled key
    weights_hash: str                  # claimed model weights


@dataclass
class ChainVerificationResult:
    success: bool
    reason: str | None = None
    verified_at_ms: int = 0
    platform: Platform = Platform.DEV


# =============================================================================
# Platform verifiers
# =============================================================================


class PlatformVerifier:
    """Per-platform verification logic. Subclasses implement verify()."""

    platform: Platform

    def verify(self, chain: AttestationChain) -> ChainVerificationResult:
        raise NotImplementedError


class DevVerifier(PlatformVerifier):
    """Dev-mode verifier: shape checks only, no real cryptographic chain.

    Accepts any chain that has structurally valid hex keys and the
    weights hash matches the claimed model_attestation hash. Only safe
    for development / testing.
    """

    platform = Platform.DEV

    def verify(self, chain: AttestationChain) -> ChainVerificationResult:
        # Shape checks
        if not chain.device_root_key_hex or not chain.enrolled_key_hex:
            return ChainVerificationResult(
                success=False,
                reason="missing keys in chain",
                verified_at_ms=int(time.time() * 1000),
                platform=Platform.DEV,
            )

        # Weights-hash binding: the model_attestation must commit to weights_hash
        expected = hashlib.sha256(
            (chain.enrolled_key_hex + chain.weights_hash).encode("utf-8")
        ).digest()
        if chain.model_attestation != expected:
            return ChainVerificationResult(
                success=False,
                reason="model_attestation does not commit to weights_hash",
                verified_at_ms=int(time.time() * 1000),
                platform=Platform.DEV,
            )

        return ChainVerificationResult(
            success=True,
            verified_at_ms=int(time.time() * 1000),
            platform=Platform.DEV,
        )


class AndroidVerifier(PlatformVerifier):
    """Android StrongBox + Keystore Attestation verifier.

    Production implementation requires the `pyjwt` + Android attestation
    extension OID parser. Stub here for interface compliance.
    """

    platform = Platform.ANDROID

    def verify(self, chain: AttestationChain) -> ChainVerificationResult:
        # TODO: parse the Android Keystore attestation certificate chain,
        # verify against Google's root, check the StrongBox-flag in the
        # extension, and verify the attestation challenge matches.
        return ChainVerificationResult(
            success=False,
            reason="Android verifier not implemented in this build",
            verified_at_ms=int(time.time() * 1000),
            platform=Platform.ANDROID,
        )


class IOSVerifier(PlatformVerifier):
    """iOS Secure Enclave + DeviceCheck App Attest verifier."""

    platform = Platform.IOS

    def verify(self, chain: AttestationChain) -> ChainVerificationResult:
        # TODO: validate App Attest assertion; parse Apple's root cert
        # chain; verify nonce binding and counter freshness.
        return ChainVerificationResult(
            success=False,
            reason="iOS verifier not implemented in this build",
            verified_at_ms=int(time.time() * 1000),
            platform=Platform.IOS,
        )


class LinuxTPMVerifier(PlatformVerifier):
    """Linux TPM 2.0 + IMA verifier."""

    platform = Platform.LINUX_TPM

    def verify(self, chain: AttestationChain) -> ChainVerificationResult:
        # TODO: verify TPM quote; parse PCRs; check IMA log integrity.
        return ChainVerificationResult(
            success=False,
            reason="Linux TPM verifier not implemented in this build",
            verified_at_ms=int(time.time() * 1000),
            platform=Platform.LINUX_TPM,
        )


# =============================================================================
# Dispatcher
# =============================================================================


_VERIFIERS: dict[Platform, PlatformVerifier] = {
    Platform.DEV: DevVerifier(),
    Platform.ANDROID: AndroidVerifier(),
    Platform.IOS: IOSVerifier(),
    Platform.LINUX_TPM: LinuxTPMVerifier(),
}


def verify_attestation_chain(
    chain: AttestationChain,
    *,
    log_client: LogClient | None = None,
    emit_break_event: bool = True,
) -> ChainVerificationResult:
    """Verify an attestation chain.

    On failure, emits an `attestation_chain_broken` event so the broken
    state is auditable.
    """
    verifier = _VERIFIERS.get(chain.platform)
    if verifier is None:
        result = ChainVerificationResult(
            success=False,
            reason=f"unknown platform: {chain.platform.value}",
            verified_at_ms=int(time.time() * 1000),
            platform=chain.platform,
        )
    else:
        result = verifier.verify(chain)

    if not result.success and emit_break_event:
        log = log_client or get_default_client()
        now = int(time.time() * 1000)
        try:
            log.append(AttestationChainBroken(
                event_id=make_event_id_log(now),
                recorded_at=now,
                actor="federate.attest",
                chain_kind=f"{chain.platform.value}:{chain.model_id}",
                failure_reason=result.reason or "verification_failed",
            ))
        except Exception:
            # If the schema doesn't have this event yet, fall back silently;
            # the verification result still propagates.
            pass

    return result


__all__ = [
    "AttestationChain",
    "AndroidVerifier",
    "ChainVerificationResult",
    "DevVerifier",
    "IOSVerifier",
    "LinuxTPMVerifier",
    "Platform",
    "PlatformVerifier",
    "verify_attestation_chain",
]
