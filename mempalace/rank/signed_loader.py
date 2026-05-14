"""
Signed ranker loader.

Per R3 §6.1: rankers downloaded from the federation registry must be
signature-verified before they're allowed to register. The loader:

  1. Pulls the ranker bundle from a URI (binary + manifest + signature)
  2. Verifies the signature against a trusted public key set
  3. Verifies the binary's hash matches the manifest's claimed hash
  4. Constructs an IsolatedRankerSpec
  5. Returns a wrapped IsolatedRankerProxy ready for registration

This file ships the verification interface and a development-mode loader
that accepts unsigned bundles for testing. Production deployment requires
a real public-key infrastructure (Ed25519 over the manifest+binary digest).

Spec ref: R3 §6.1.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .isolation import (
    BehaviorMonitor,
    IsolatedRankerProxy,
    IsolatedRankerSpec,
    get_behavior_monitor,
)


# =============================================================================
# Bundle types
# =============================================================================


@dataclass
class RankerBundle:
    """Contents of a downloaded ranker bundle."""

    name: str
    version: str
    manifest: dict
    binary_path: str
    binary_hash: str
    signature: bytes
    signing_pubkey_hex: str


# =============================================================================
# Trust store
# =============================================================================


@dataclass
class TrustStore:
    """Trusted public keys for verifying ranker signatures.

    In dev mode, accepts the special "dev-trusted" key. Production
    populates this with federation-trusted Ed25519 public keys.
    """

    pubkeys_hex: set[str]
    dev_mode: bool = False

    def is_trusted(self, pubkey_hex: str) -> bool:
        if self.dev_mode and pubkey_hex == "dev-trusted":
            return True
        return pubkey_hex in self.pubkeys_hex


# =============================================================================
# Verification
# =============================================================================


@dataclass
class VerificationResult:
    success: bool
    reason: str | None = None


def verify_bundle(bundle: RankerBundle, trust_store: TrustStore) -> VerificationResult:
    """Verify the signature, hash, and trust chain of a bundle.

    In dev mode, only checks hashes and the manifest shape; signature
    bytes are tolerated as anything. In production mode, performs
    real Ed25519 verification (requires `cryptography` package).
    """
    # Trust-chain check
    if not trust_store.is_trusted(bundle.signing_pubkey_hex):
        return VerificationResult(
            success=False,
            reason=f"signing key not in trust store: {bundle.signing_pubkey_hex[:16]}…",
        )

    # Binary-hash check
    binary_path = Path(bundle.binary_path)
    if not binary_path.exists():
        return VerificationResult(
            success=False,
            reason=f"binary not found: {bundle.binary_path}",
        )

    actual_hash = _hash_file(binary_path)
    if actual_hash != bundle.binary_hash:
        return VerificationResult(
            success=False,
            reason=f"binary hash mismatch: expected {bundle.binary_hash[:16]}… got {actual_hash[:16]}…",
        )

    # Manifest shape check
    required_fields = ("name", "version", "feature_dependencies")
    for f in required_fields:
        if f not in bundle.manifest:
            return VerificationResult(
                success=False,
                reason=f"manifest missing required field: {f}",
            )

    # Signature check
    if trust_store.dev_mode and bundle.signing_pubkey_hex == "dev-trusted":
        # Dev mode: skip cryptographic verification
        return VerificationResult(success=True)

    # Real signature verification deferred to production loader; require
    # the cryptography package to be available there.
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return VerificationResult(
            success=False,
            reason="signature verification unavailable (cryptography package not installed)",
        )

    try:
        pubkey_bytes = bytes.fromhex(bundle.signing_pubkey_hex)
        pubkey = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        # Sign content: manifest JSON (canonical) + binary hash
        content = (
            json.dumps(bundle.manifest, sort_keys=True).encode("utf-8")
            + b"\x00"
            + bundle.binary_hash.encode("utf-8")
        )
        pubkey.verify(bundle.signature, content)
        return VerificationResult(success=True)
    except InvalidSignature:
        return VerificationResult(success=False, reason="signature did not verify")
    except Exception as e:
        return VerificationResult(success=False, reason=f"verification error: {e}")


# =============================================================================
# Loader
# =============================================================================


def load_signed_ranker(
    bundle: RankerBundle,
    *,
    trust_store: TrustStore,
    monitor: BehaviorMonitor | None = None,
) -> IsolatedRankerProxy:
    """Verify a bundle and return a ready-to-register IsolatedRankerProxy.

    Raises ValueError if verification fails.
    """
    result = verify_bundle(bundle, trust_store)
    if not result.success:
        raise ValueError(f"ranker verification failed: {result.reason}")

    spec = IsolatedRankerSpec(
        name=bundle.name,
        executable_path=bundle.binary_path,
        weights_hash=bundle.binary_hash,
    )
    return IsolatedRankerProxy(spec, monitor=monitor or get_behavior_monitor())


# =============================================================================
# Helpers
# =============================================================================


def _hash_file(path: Path, *, chunk_size: int = 65536) -> str:
    """SHA-256 of a file's contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "RankerBundle",
    "TrustStore",
    "VerificationResult",
    "load_signed_ranker",
    "verify_bundle",
]
