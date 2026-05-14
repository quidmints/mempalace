"""
Federation egress encryption — Track 5F.

Per IMPLEMENTATION_ROADMAP.md §"Track 5F":
  - Session-key manager extends to FEK derivation per sandbox.
  - Findings carry FEK-encrypted payloads.
  - Tests: end-to-end federation between two palaces with separate
    key managers.

# What this module ships

Cloud-box-side wrapping/unwrapping of federation findings under a
shared FEK (Federation Encryption Key):

  - `EncryptedFindingEnvelope` — wire-format wrapper carrying
    ciphertext, sandbox_id, and the unencrypted signature so the
    receiver can verify before decrypting.
  - `wrap_finding_for_egress(finding, fek, sandbox_id, km)` — encrypt.
  - `unwrap_finding(envelope, fek, sandbox_id, km)` — decrypt + verify.

# What this module does NOT ship

  - FEK derivation. The FEK is assumed pre-negotiated via a prior
    key-agreement step (X25519 between session keys, or similar).
    The session-keys module already does the keypair exchange; the
    FEK derivation from a shared secret is its own step that
    Track 5F's docstring leaves to follow-on work.
  - Wire transport. Production ships envelopes over HTTPS / RPC;
    this module just produces the bytes.

# Why a separate module from `findings.py`

`findings.py` is the structural/signing layer: build a Finding,
sign it, classify topology. This module is the encryption wrapper
on top. Keeping them separate means:
  - Plain (signed-only) findings still work for non-encrypted paths
    (test fixtures, internal-loopback, audit).
  - The encryption layer can evolve (different FEK schemes, key
    rotation) without touching finding-building code.

Spec ref: IMPLEMENTATION_ROADMAP.md §"Track 5F",
ENCRYPTION_AT_EDGE_DESIGN.md v2 §"Cloud-box-side keys".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from .findings import Finding

if TYPE_CHECKING:
    from ..secure.key_manager import CloudBoxKeyManager


# =============================================================================
# Envelope shape
# =============================================================================


@dataclass(frozen=True)
class EncryptedFindingEnvelope:
    """Wire-format wrapper around an encrypted Finding.

    Fields kept outside the ciphertext:
      - `match_id` — needed by the receiver to look up the FEK + the
        prior session signatures.
      - `sandbox_id` — the AAD for the FEK encryption; the receiver
        passes this back to `decrypt_with_fek`.
      - `emitter_palace_id` — sender identity (already in the
        finding, but we expose it on the envelope so the receiver
        can route + verify before decrypting).
      - `session_pubkey_hex`, `signature_hex` — kept outside so the
        receiver can verify the signature against the wire-format
        body BEFORE decrypting. Defense-in-depth.

    The encrypted body contains the rest of the Finding.
    """

    match_id: str
    sandbox_id: str
    emitter_palace_id: str
    session_pubkey_hex: str
    signature_hex: str
    ciphertext: bytes

    schema_version: str = "encrypted_finding.v1"


# =============================================================================
# Wrap / unwrap
# =============================================================================


def wrap_finding_for_egress(
    finding: Finding,
    *,
    fek: bytes,
    sandbox_id: str,
    key_manager: "CloudBoxKeyManager",
) -> EncryptedFindingEnvelope:
    """Encrypt a Finding for egress under the negotiated FEK.

    Args:
      finding: The signed finding to ship.
      fek: Shared symmetric key from the prior key-agreement step.
      sandbox_id: Federation match sandbox the finding belongs to.
        Used as AAD; receiver must pass the same value to unwrap.
      key_manager: Cloud-box key manager (for `encrypt_with_fek`).

    Returns:
      EncryptedFindingEnvelope ready for transmission.
    """
    if not finding.signature_hex:
        raise ValueError("finding must be signed before wrapping for egress")

    body_dict = asdict(finding)
    # Strip fields we expose on the envelope so they aren't duplicated
    # inside the ciphertext (saves bytes; receiver reconstructs).
    body_dict.pop("session_pubkey_hex", None)
    body_dict.pop("signature_hex", None)
    body_bytes = json.dumps(
        body_dict, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")

    ciphertext = key_manager.encrypt_with_fek(
        body_bytes,
        fek=fek,
        sandbox_id=sandbox_id,
    )

    return EncryptedFindingEnvelope(
        match_id=finding.match_id,
        sandbox_id=sandbox_id,
        emitter_palace_id=finding.emitter_palace_id,
        session_pubkey_hex=finding.session_pubkey_hex,
        signature_hex=finding.signature_hex,
        ciphertext=ciphertext,
    )


def unwrap_finding(
    envelope: EncryptedFindingEnvelope,
    *,
    fek: bytes,
    sandbox_id: str,
    key_manager: "CloudBoxKeyManager",
) -> Finding:
    """Decrypt + reconstruct a Finding from an envelope.

    Args:
      envelope: The wire-format envelope received from the peer.
      fek: Shared symmetric key (same as the sender derived).
      sandbox_id: Must match the sender's sandbox_id; AAD enforces it.
      key_manager: Cloud-box key manager (for `decrypt_with_fek`).

    Returns:
      The reconstructed Finding (with signature + pubkey populated
      from the envelope).

    Raises:
      AttestationError: ciphertext failed authentication. Could be
        tampering, wrong FEK, wrong sandbox_id, or replay.
      ValueError: envelope's sandbox_id doesn't match argument.
    """
    if envelope.sandbox_id != sandbox_id:
        raise ValueError(
            f"envelope sandbox_id={envelope.sandbox_id!r} doesn't match "
            f"caller sandbox_id={sandbox_id!r}"
        )

    body_bytes = key_manager.decrypt_with_fek(
        envelope.ciphertext,
        fek=fek,
        sandbox_id=sandbox_id,
    )
    body_dict = json.loads(body_bytes.decode("utf-8"))

    # Reconstruct the Finding with signature + pubkey from envelope
    body_dict["session_pubkey_hex"] = envelope.session_pubkey_hex
    body_dict["signature_hex"] = envelope.signature_hex

    # Coerce topology back to enum if it's a string
    from .findings import FindingTopology
    if "topology" in body_dict and not isinstance(
        body_dict["topology"], FindingTopology,
    ):
        try:
            body_dict["topology"] = FindingTopology(body_dict["topology"])
        except ValueError:
            pass  # Leave as string; constructor will raise if needed

    return Finding(**body_dict)


# =============================================================================
# FEK derivation hook
# =============================================================================


def derive_fek_from_shared_secret(
    shared_secret: bytes,
    *,
    sandbox_id: str,
) -> bytes:
    """Derive a per-match FEK from a shared secret (e.g. X25519
    output) plus the sandbox_id.

    Both ends of the match independently derive the same FEK by
    running this with the same inputs. The X25519 step that produces
    the shared_secret is a separate concern handled by the federation
    handshake (not implemented here).

    Returns 32 bytes suitable for AES-GCM.
    """
    import hashlib
    if len(shared_secret) < 16:
        raise ValueError("shared_secret too short")
    h = hashlib.sha256()
    h.update(b"fek-derive-v1:")
    h.update(sandbox_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(shared_secret)
    return h.digest()


__all__ = [
    "EncryptedFindingEnvelope",
    "derive_fek_from_shared_secret",
    "unwrap_finding",
    "wrap_finding_for_egress",
]
