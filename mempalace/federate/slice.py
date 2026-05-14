"""
Slice extraction for federation matching.

Per R3 §7.1 / Part 9.2: when two palaces enter a sandboxed match, each
side prepares an encrypted slice of its own substrate and ships it to
the sandbox. The slice is scoped to:

  - The match request's layer (1 / 2 / 3)
  - The match request's scope_spec (themes / periods / events)
  - The user's privacy mode for federation

Layer 1: structural sketch only (manifest already covers this; slice
         is empty or just the manifest).
Layer 2: assertion fingerprints + derivation-graph topology, no
         substrate text.
Layer 3: full drawer substrate (verbatim, semantic, paralinguistic) for
         the in-scope drawers — this is the only path that exposes
         substrate, and only inside the sandbox.

The slice is encrypted to the sandbox's session key, which is destroyed
when the sandbox tears down (zeroizes substrate exposure).

Spec ref: R3 §7.1, Part 9.2.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Slice layers
# =============================================================================


class SliceLayer(int, Enum):
    STRUCTURAL = 1
    DERIVATION = 2
    SUBSTRATE = 3


# =============================================================================
# Slice payloads
# =============================================================================


@dataclass
class StructuralSlicePayload:
    """Layer-1 slice. Carries the same fields as the manifest's structural
    summary — included for completeness; usually peers already have this
    via the manifest pull."""

    minhash_sketch: list[int] = field(default_factory=list)
    schema_fingerprints: list[str] = field(default_factory=list)
    velocity_summary_flat: dict[str, float] = field(default_factory=dict)


@dataclass
class DerivationSlicePayload:
    """Layer-2 slice.

    Ships the topology of the derivation DAG restricted to the scoped
    region, plus per-assertion fingerprints. No substrate text.
    """

    assertion_fingerprints: list[str] = field(default_factory=list)
    # edges as (src_assertion_idx, dst_assertion_idx)
    derivation_edges: list[tuple[int, int]] = field(default_factory=list)
    # predicates → counts (anonymized via canonicalizer)
    predicate_distribution: dict[str, int] = field(default_factory=dict)


@dataclass
class SubstrateDrawerEntry:
    """A single drawer in a Layer-3 slice."""

    drawer_id: str
    verbatim_text: str = ""               # under sandbox boundary only
    semantic_embedding: list[float] = field(default_factory=list)
    paralinguistic: dict[str, Any] = field(default_factory=dict)
    structural: dict[str, Any] = field(default_factory=dict)
    social: dict[str, Any] = field(default_factory=dict)
    captured_at_ms: int = 0


@dataclass
class SubstrateSlicePayload:
    """Layer-3 slice: full drawer substrate for in-scope drawers."""

    drawers: list[SubstrateDrawerEntry] = field(default_factory=list)


# =============================================================================
# Encrypted slice envelope
# =============================================================================


@dataclass
class EncryptedSlice:
    """An encrypted slice ready to ship over /mempalace/slice/1.0.0."""

    slice_id: str
    layer: SliceLayer
    palace_id: str
    target_session_pubkey_hex: str
    nonce_hex: str
    ciphertext_hex: str
    aad_hex: str                          # additional authenticated data
    slice_size_bytes: int
    schema_version: str = "slice.v1"


# =============================================================================
# Slice builder + encryption
# =============================================================================


def _serialize_payload(payload: Any) -> bytes:
    if hasattr(payload, "__dataclass_fields__"):
        from dataclasses import asdict
        return json.dumps(asdict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _aad(palace_id: str, layer: SliceLayer, slice_id: str) -> bytes:
    return json.dumps(
        {"palace_id": palace_id, "layer": int(layer), "slice_id": slice_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _encrypt(payload_bytes: bytes, target_pubkey_hex: str, aad: bytes) -> tuple[str, str]:
    """Encrypt with a session key derived from the target pubkey.

    TODO: production uses an X25519 ECIES variant + ChaCha20-Poly1305
    AEAD against the target session key. This stub keeps the byte
    layout (nonce + ciphertext) so callers can be tested before the
    crypto wiring lands.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

        # Derive a key from the target pubkey hex (NOT REAL ECIES — just
        # a deterministic dev derivation; production must do X25519).
        key_seed = bytes.fromhex(target_pubkey_hex)[:32].ljust(32, b"\x00")
        cipher = ChaCha20Poly1305(key_seed)
        nonce = secrets.token_bytes(12)
        ct = cipher.encrypt(nonce, payload_bytes, aad)
        return nonce.hex(), ct.hex()
    except Exception:
        # Fallback: not encrypted, just length-tagged. ONLY for shape testing.
        nonce = secrets.token_bytes(12)
        return nonce.hex(), payload_bytes.hex()


def build_encrypted_slice(
    *,
    palace_id: str,
    layer: SliceLayer,
    payload: StructuralSlicePayload | DerivationSlicePayload | SubstrateSlicePayload,
    target_session_pubkey_hex: str,
) -> EncryptedSlice:
    """Serialize and encrypt a slice payload."""
    slice_id = "slc_" + hashlib.blake2b(
        f"{palace_id}|{int(layer)}|{secrets.token_hex(8)}".encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    payload_bytes = _serialize_payload(payload)
    aad = _aad(palace_id, layer, slice_id)
    nonce_hex, ct_hex = _encrypt(payload_bytes, target_session_pubkey_hex, aad)
    return EncryptedSlice(
        slice_id=slice_id,
        layer=layer,
        palace_id=palace_id,
        target_session_pubkey_hex=target_session_pubkey_hex,
        nonce_hex=nonce_hex,
        ciphertext_hex=ct_hex,
        aad_hex=aad.hex(),
        slice_size_bytes=len(payload_bytes),
    )


# =============================================================================
# Scope-bound slice extraction
# =============================================================================


@dataclass
class SliceScope:
    """The scope bound for slice extraction (a subset of scope_spec)."""

    theme_ids: tuple[str, ...] = ()
    period_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    drawer_ids: tuple[str, ...] = ()
    max_drawers: int = 256


def extract_substrate_slice(
    *,
    drawer_records: Iterable[dict[str, Any]],
    scope: SliceScope,
) -> SubstrateSlicePayload:
    """Build a Layer-3 substrate slice from raw drawer records.

    `drawer_records` is the iterable produced by walking views.current_drawers
    filtered to the scope. Each record carries the 5 facets per R3 §6.1.
    """
    out: list[SubstrateDrawerEntry] = []
    for r in drawer_records:
        did = r.get("drawer_id", "")
        if not did:
            continue
        if scope.drawer_ids and did not in scope.drawer_ids:
            continue
        out.append(
            SubstrateDrawerEntry(
                drawer_id=did,
                verbatim_text=str(r.get("verbatim", "") or ""),
                semantic_embedding=list(r.get("semantic_embedding", []) or []),
                paralinguistic=dict(r.get("paralinguistic", {}) or {}),
                structural=dict(r.get("structural", {}) or {}),
                social=dict(r.get("social", {}) or {}),
                captured_at_ms=int(r.get("captured_at_ms", 0) or 0),
            )
        )
        if len(out) >= scope.max_drawers:
            break
    return SubstrateSlicePayload(drawers=out)


__all__ = [
    "DerivationSlicePayload",
    "EncryptedSlice",
    "SliceLayer",
    "SliceScope",
    "StructuralSlicePayload",
    "SubstrateDrawerEntry",
    "SubstrateSlicePayload",
    "build_encrypted_slice",
    "extract_substrate_slice",
]
