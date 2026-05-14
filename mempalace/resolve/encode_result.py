"""
Off-chain → on-chain encoding for resolution results.

Per R3 §2.2: a thin wrapper that encodes a resolved-result Python
object into the Borsh-compatible bytes the on-chain Anchor program
expects. Production wiring uses a real Borsh codec; this module ships
a stable, documented encoder that any conformant Borsh implementation
can reproduce.

The on-chain submission format is: `submit_resolution(market_pubkey,
encoded_result)`. The encoded payload covers:

  - market_id
  - outcome (i8: 0=YES, 1=NO, -1=indeterminate)
  - confidence_bps (u16)
  - method ("deterministic" | "llm" | "veto" | "insufficient")
  - resolver_attestation_hash (32 bytes)
  - resolution_at_ms (u64)
  - reason_summary (capped utf8)

Spec ref: R3 §2.2.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Final


# =============================================================================
# Constants
# =============================================================================

# Hash size for the attestation reference
ATTESTATION_HASH_SIZE: Final = 32

# Cap on the human-readable reason string carried on-chain
MAX_REASON_BYTES: Final = 256

# Method codes (we encode as a u8 to save bytes)
METHOD_CODES = {
    "deterministic": 0,
    "llm": 1,
    "veto": 2,
    "insufficient": 3,
}


# =============================================================================
# Result dataclass
# =============================================================================


@dataclass
class EncodedResolution:
    """A resolution ready for on-chain submission."""

    market_id: bytes
    outcome: int
    confidence_bps: int
    method: str
    resolver_attestation_hash: bytes
    resolution_at_ms: int
    reason_summary: str = ""


# =============================================================================
# Encoder
# =============================================================================


def encode_resolution(
    *,
    market_id: str,
    outcome: int,
    confidence_bps: int,
    method: str,
    resolver_attestation_hash: bytes | None = None,
    resolution_at_ms: int = 0,
    reason_summary: str = "",
) -> bytes:
    """Encode a resolution result into on-chain bytes.

    Layout (little-endian):
      32  market_id_hash         (blake2b digest of market_id string)
       1  outcome                (i8)
       2  confidence_bps         (u16)
       1  method_code            (u8)
      32  resolver_attestation   (zero-padded if not provided)
       8  resolution_at_ms       (u64)
       2  reason_len             (u16)
       N  reason_bytes           (utf-8, capped at MAX_REASON_BYTES)
    """
    if not -1 <= outcome <= 127:
        raise ValueError("outcome must fit in i8")
    if not 0 <= confidence_bps <= 65535:
        raise ValueError("confidence_bps must fit in u16")
    if method not in METHOD_CODES:
        raise ValueError(f"unknown method: {method}")

    market_hash = hashlib.blake2b(
        market_id.encode("utf-8"),
        digest_size=32,
    ).digest()
    method_code = METHOD_CODES[method]

    if resolver_attestation_hash is None:
        attestation = b"\x00" * ATTESTATION_HASH_SIZE
    else:
        if len(resolver_attestation_hash) != ATTESTATION_HASH_SIZE:
            raise ValueError(
                f"resolver_attestation_hash must be {ATTESTATION_HASH_SIZE} bytes, "
                f"got {len(resolver_attestation_hash)}"
            )
        attestation = resolver_attestation_hash

    reason_bytes = reason_summary.encode("utf-8")[:MAX_REASON_BYTES]

    return (
        market_hash
        + struct.pack("<b", outcome)
        + struct.pack("<H", confidence_bps)
        + struct.pack("<B", method_code)
        + attestation
        + struct.pack("<Q", resolution_at_ms)
        + struct.pack("<H", len(reason_bytes))
        + reason_bytes
    )


def decode_resolution(buf: bytes) -> EncodedResolution:
    """Inverse of `encode_resolution()` — useful for tests."""
    pos = 0
    market_hash = buf[pos:pos + 32]
    pos += 32
    outcome = struct.unpack_from("<b", buf, pos)[0]
    pos += 1
    confidence_bps = struct.unpack_from("<H", buf, pos)[0]
    pos += 2
    method_code = struct.unpack_from("<B", buf, pos)[0]
    pos += 1
    attestation = buf[pos:pos + 32]
    pos += 32
    resolution_at_ms = struct.unpack_from("<Q", buf, pos)[0]
    pos += 8
    reason_len = struct.unpack_from("<H", buf, pos)[0]
    pos += 2
    reason = buf[pos:pos + reason_len].decode("utf-8", errors="replace")

    method = next(
        (k for k, v in METHOD_CODES.items() if v == method_code), "unknown"
    )

    return EncodedResolution(
        market_id=market_hash,
        outcome=outcome,
        confidence_bps=confidence_bps,
        method=method,
        resolver_attestation_hash=attestation,
        resolution_at_ms=resolution_at_ms,
        reason_summary=reason,
    )


__all__ = [
    "ATTESTATION_HASH_SIZE",
    "EncodedResolution",
    "MAX_REASON_BYTES",
    "METHOD_CODES",
    "decode_resolution",
    "encode_resolution",
]
