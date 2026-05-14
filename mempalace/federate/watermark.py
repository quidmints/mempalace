"""
Embedding watermarks.

Per R3 §3.3 / Part 9.2: a malicious peer might exfiltrate substrate by
asking for highly-targeted matches and reconstructing what they saw.
Watermarking lets the local palace embed a per-session-key invisible
signal in any substrate that left the sandbox, so the user (or auditor)
can later verify whether content seen elsewhere came from this palace.

Watermarking strategy:

  - For each session key, derive a per-session pseudo-random pattern
    over assertion-canonical-form bytes (a permutation seed).
  - When substrate is written to the sandbox, apply a barely-perceptible
    embedding shift (e.g. permute paraphrase choices, reorder bullet
    enumerations, drop optional articles) along the pattern.
  - The pattern is stored locally so if a later piece of text is
    suspected of being a leak, we can run detect() against it.

The watermark is not a hard guarantee — natural-language transforms
can wash it out — but it raises the cost of exfiltration.

This module ships only the protocol + a hashing-based stub. Real
watermark embedding/detection requires the substrate-aware codec
that lives in the substrate layer.

Spec ref: R3 §3.3, Part 9.2.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from typing import Any


# =============================================================================
# Watermark seed
# =============================================================================


@dataclass
class WatermarkSeed:
    """Per-session watermark seed."""

    session_key_id: str
    seed_bytes: bytes               # 32 bytes; treated as the HMAC key


@dataclass
class DetectionResult:
    """Outcome of running detect() against a piece of suspected content."""

    matched: bool
    confidence: float                # in [0, 1]; below 0.5 means 'no match'
    matched_session_key_id: str | None = None


# =============================================================================
# WatermarkRegistry
# =============================================================================


class WatermarkRegistry:
    """Holds the per-session seeds; supports embed/detect."""

    def __init__(self) -> None:
        self._seeds: dict[str, WatermarkSeed] = {}
        self._lock = threading.Lock()

    def create(self, session_key_id: str) -> WatermarkSeed:
        seed = WatermarkSeed(
            session_key_id=session_key_id,
            seed_bytes=secrets.token_bytes(32),
        )
        with self._lock:
            self._seeds[session_key_id] = seed
        return seed

    def get(self, session_key_id: str) -> WatermarkSeed | None:
        with self._lock:
            return self._seeds.get(session_key_id)

    def revoke(self, session_key_id: str) -> bool:
        """Revoke a seed (called when the session key is destroyed)."""
        with self._lock:
            if session_key_id not in self._seeds:
                return False
            # zero seed bytes before dropping
            seed = self._seeds.pop(session_key_id)
            try:
                buf = bytearray(seed.seed_bytes)
                for i in range(len(buf)):
                    buf[i] = 0
            except Exception:
                pass
            return True

    # ---- embed / detect ---------------------------------------------------

    def embed(self, session_key_id: str, content: str) -> str:
        """Apply a watermark transform to content.

        Stub: appends a zero-width hash tag to the very end of content
        derived from HMAC(seed, content). Production replaces this with
        a substrate-aware codec.
        """
        seed = self.get(session_key_id)
        if seed is None:
            return content
        tag = hmac.new(
            seed.seed_bytes,
            content.encode("utf-8"),
            hashlib.blake2b,
        ).digest()[:8]
        # Encode tag as zero-width characters (U+200B / U+200C bits)
        # 64 bits → 64 zw chars
        bits = "".join(f"{b:08b}" for b in tag)
        zw = "".join("\u200b" if c == "0" else "\u200c" for c in bits)
        return content + zw

    def detect(self, content: str) -> DetectionResult:
        """Detect whether content carries any registered watermark."""
        # Extract zero-width tail (if present)
        zw_chars = []
        for ch in reversed(content):
            if ch in ("\u200b", "\u200c"):
                zw_chars.append(ch)
            else:
                break
        if len(zw_chars) < 64:
            return DetectionResult(matched=False, confidence=0.0)
        zw_chars.reverse()
        bits = "".join("0" if c == "\u200b" else "1" for c in zw_chars[-64:])
        try:
            tag = bytes(int(bits[i : i + 8], 2) for i in range(0, 64, 8))
        except ValueError:
            return DetectionResult(matched=False, confidence=0.0)

        body = content[: len(content) - len(zw_chars)]
        with self._lock:
            seeds = list(self._seeds.values())

        for seed in seeds:
            expected = hmac.new(
                seed.seed_bytes, body.encode("utf-8"), hashlib.blake2b
            ).digest()[:8]
            if hmac.compare_digest(expected, tag):
                return DetectionResult(
                    matched=True,
                    confidence=1.0,
                    matched_session_key_id=seed.session_key_id,
                )
        return DetectionResult(matched=False, confidence=0.0)


# =============================================================================
# Module-level singleton
# =============================================================================


_REGISTRY: WatermarkRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_watermark_registry() -> WatermarkRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = WatermarkRegistry()
        return _REGISTRY


def set_watermark_registry(reg: WatermarkRegistry) -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = reg


__all__ = [
    "DetectionResult",
    "WatermarkRegistry",
    "WatermarkSeed",
    "get_watermark_registry",
    "set_watermark_registry",
]
