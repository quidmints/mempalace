"""
Disk-at-rest log encryption layer — Track 5G.

Per IMPLEMENTATION_ROADMAP.md §"Track 5G":
  - Wrap `mempalace_core` log appender in a transparent encryption
    layer using DARK from the bundle.
  - Tests: cold-start a fresh process, verify it can't read existing
    log without re-loading bundle from phone.

# What this module ships

A transparent encryption wrapper for log payloads:

  - `derive_dark(master_key)` — deterministic key derivation from
    the bundle's master_key. Both writes and reads derive the same
    DARK, so as long as the bundle is loaded the wrapper round-trips.
  - `EncryptedLogBackend` — wraps any `LogBackend`, encrypts payloads
    on append, decrypts on read. Activates only when a DARK is
    provided; without DARK the wrapper falls back to passthrough
    (legacy / pre-Track-5G logs continue to work).
  - `wrap_log_with_dark(client, dark)` — convenience: takes an
    existing LogClient, swaps its backend for an encrypted wrapper.

# Defense in depth

The layer encrypts whole serialized payloads, so even structural
metadata (event_id, batch_id, drawer_id, etc.) is at-rest-encrypted.
Cold-start without the bundle yields ciphertext blobs the daemon
can't make sense of. With the bundle, the daemon decrypts on read
transparently.

# Why a wrapper, not a new backend

Production has multiple backend implementations (MockBackend in
tests; real on-disk in prod; potentially a memory-mapped variant).
A wrapper composes with all of them — encryption is orthogonal to
storage.

# What this module does NOT ship

  - The bundle-load wiring. Production wires this in the daemon
    startup: load bundle → derive DARK → wrap log client. Tests
    show the shape; production daemons plug it in.
  - Per-event AAD beyond the offset. AAD currently is just the
    offset (so reordering / replaying with a different offset
    fails). Future work could include the kind in AAD, requiring
    the kind to be plaintext on disk; current shape keeps the kind
    plaintext anyway because the LogBackend Protocol stores it
    separately from the payload.
  - Key rotation. Current shape uses a single DARK. Rotation would
    be a follow-on track.

Spec ref: IMPLEMENTATION_ROADMAP.md §"Track 5G",
ENCRYPTION_AT_EDGE_DESIGN.md v2 §"Cloud-box-side keys".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Iterator

from ..secure.element import _crypt_decrypt, _crypt_encrypt
from .client import LogBackend, LogClient

logger = logging.getLogger(__name__)


# =============================================================================
# Derivation
# =============================================================================


def derive_dark(master_key: bytes) -> bytes:
    """Derive the Disk-At-Rest Key (DARK) from the bundle master_key.

    Deterministic: same master_key always yields the same DARK, so
    a daemon that loads the bundle twice (cold start with cached
    bundle) reads its own logs transparently.

    Returns 32 bytes suitable for AES-GCM.
    """
    if not isinstance(master_key, (bytes, bytearray)) or len(master_key) < 16:
        raise ValueError("master_key must be at least 16 bytes")
    h = hashlib.sha256()
    h.update(b"mempalace-dark-v1:")
    h.update(bytes(master_key))
    return h.digest()


# =============================================================================
# Wire format
# =============================================================================


_ENCRYPTED_PAYLOAD_MARKER = "_at_rest_encrypted"
"""Field added to encrypted payloads. Lets a fresh-start backend
distinguish encrypted entries from legacy plaintext (pre-5G) entries
in the same log."""

_CIPHERTEXT_FIELD = "_blob_b64"
"""The encrypted payload bytes, base64-encoded for JSON-friendly
transport. We could store raw bytes if the backend is binary-clean,
but base64 keeps compatibility with the existing dict-based
LogBackend Protocol."""


def _encrypt_payload(
    plaintext_payload: dict,
    *,
    dark: bytes,
    offset_hint: int,
) -> dict:
    """Wrap a plaintext payload into an encrypted form.

    The output is a dict with two fields: the marker + the
    base64-encoded ciphertext. The wrapper preserves the dict shape
    so existing read paths that don't go through the wrapper see
    obvious "this is encrypted" signal rather than crashing on
    surprise binary.
    """
    import base64
    body = json.dumps(
        plaintext_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    aad = b"dark-v1:" + str(offset_hint).encode("utf-8")
    ct = _crypt_encrypt(dark[:32], body, aad=aad)
    return {
        _ENCRYPTED_PAYLOAD_MARKER: True,
        _CIPHERTEXT_FIELD: base64.b64encode(ct).decode("ascii"),
    }


def _decrypt_payload(
    wrapped_payload: dict,
    *,
    dark: bytes,
    offset_hint: int,
) -> dict:
    """Inverse of _encrypt_payload. Raises on tampering / wrong DARK.

    The actual decryption error surface depends on the underlying
    AES-GCM impl; we re-raise as a generic ValueError so callers
    don't need to import secure-element internals.
    """
    import base64

    blob = wrapped_payload.get(_CIPHERTEXT_FIELD)
    if not isinstance(blob, str):
        raise ValueError("encrypted payload missing ciphertext field")
    ct = base64.b64decode(blob.encode("ascii"))
    aad = b"dark-v1:" + str(offset_hint).encode("utf-8")
    try:
        body = _crypt_decrypt(dark[:32], ct, aad=aad)
    except Exception as e:
        raise ValueError(f"DARK decryption failed: {e}") from e
    return json.loads(body.decode("utf-8"))


def _is_encrypted(payload: dict) -> bool:
    return bool(payload.get(_ENCRYPTED_PAYLOAD_MARKER))


# =============================================================================
# EncryptedLogBackend — the wrapper
# =============================================================================


@dataclass
class EncryptedLogBackend:
    """Backend that transparently encrypts payloads.

    Wraps any underlying LogBackend. Production wires this between
    the LogClient and the on-disk backend.

    Construction:
      inner = MockBackend()  # or production on-disk backend
      enc = EncryptedLogBackend(inner=inner, dark=dark_bytes)
      client = LogClient(backend=enc)

    DARK rotation:
      The current shape uses a single DARK. To rotate, callers
      construct a new wrapper with the new DARK + a key_id hint
      and re-encrypt entries on read (a one-shot migration).
      Out of scope for Track 5G.

    Pass `dark=None` for passthrough (e.g., before bundle load).
    Operations succeed but no encryption is applied; callers
    that *should* be encrypted should check `is_active()` first
    and refuse to operate when DARK is absent.
    """

    inner: LogBackend
    dark: bytes | None = None
    """When None, the wrapper is in passthrough. Set via `set_dark()`
    after the bundle loads."""

    require_encryption: bool = False
    """When True, refuse to operate without a DARK loaded. Production
    daemons set this to True to prevent accidentally writing
    plaintext if the bundle wasn't loaded."""

    _lock: threading.RLock = field(default_factory=threading.RLock)
    _passthrough_count: int = 0
    """Diagnostic: how many appends/reads went through unencrypted
    because DARK wasn't loaded. Production should monitor this."""

    # ---- DARK management ---------------------------------------------------

    def set_dark(self, dark: bytes | None) -> None:
        """Install / replace the DARK. None → passthrough."""
        with self._lock:
            self.dark = dark

    def is_active(self) -> bool:
        with self._lock:
            return self.dark is not None

    # ---- LogBackend Protocol -----------------------------------------------

    def append(self, event_kind: str, payload: dict) -> int:
        with self._lock:
            dark = self.dark
            if dark is None:
                if self.require_encryption:
                    raise RuntimeError(
                        "EncryptedLogBackend.append called without DARK; "
                        "require_encryption=True"
                    )
                self._passthrough_count += 1
                return self.inner.append(event_kind, payload)

        # Encrypt outside the lock — _crypt_encrypt is thread-safe per call.
        # We need an offset for AAD, but the offset is assigned by the
        # inner backend on append. Compromise: peek `current_offset() + 1`
        # as the offset_hint. Worst case the inner backend assigns a
        # different offset (e.g. concurrent writers); we re-encrypt
        # below with the actual offset.
        # In practice the inner backend is locked too; this is fine.
        hinted_offset = self.inner.current_offset() + 1
        encrypted = _encrypt_payload(payload, dark=dark, offset_hint=hinted_offset)
        actual_offset = self.inner.append(event_kind, encrypted)

        # If hinted_offset != actual_offset, the AAD doesn't match.
        # We need to re-encrypt under the actual offset.
        if actual_offset != hinted_offset:
            # Caller's payload is already on disk with stale AAD. We
            # need a backend rewrite to fix it. Use the tombstoning
            # API if available.
            re_encrypted = _encrypt_payload(
                payload, dark=dark, offset_hint=actual_offset,
            )
            from .client import TombstoningBackend
            if isinstance(self.inner, TombstoningBackend):
                self.inner.rewrite_payload(actual_offset, re_encrypted)
            else:
                logger.warning(
                    "EncryptedLogBackend: offset hint mismatch (%d vs %d) "
                    "and inner backend doesn't support rewrite; payload "
                    "may not decrypt correctly.",
                    hinted_offset, actual_offset,
                )
        return actual_offset

    def current_offset(self) -> int:
        return self.inner.current_offset()

    def read_range(
        self, start: int, end: int,
    ) -> list[tuple[int, str, dict]]:
        with self._lock:
            dark = self.dark

        rows = self.inner.read_range(start, end)
        if dark is None:
            # Passthrough: caller will see encrypted blobs as-is. Useful
            # for diagnostics; production should require_encryption=True
            # so we never get here without DARK.
            self._passthrough_count += len(rows)
            return rows

        out: list[tuple[int, str, dict]] = []
        for offset, kind, payload in rows:
            if not _is_encrypted(payload):
                # Legacy / pre-5G entry — pass through unchanged.
                out.append((offset, kind, payload))
                continue
            try:
                decrypted = _decrypt_payload(
                    payload, dark=dark, offset_hint=offset,
                )
                out.append((offset, kind, decrypted))
            except ValueError:
                # Decryption failure (e.g. wrong DARK after key
                # rotation, or tampering). Surface as the encrypted
                # blob so caller can decide. Don't silently drop.
                logger.warning(
                    "EncryptedLogBackend: failed to decrypt payload at "
                    "offset %d kind=%s; surfacing encrypted blob.",
                    offset, kind,
                )
                out.append((offset, kind, payload))
        return out

    # ---- Tombstoning passthrough (Track 6D compatibility) ------------------

    def rewrite_payload(self, offset: int, new_payload: dict) -> bool:
        """Track 6D's tombstoning routes through us.

        We re-encrypt the new payload before passing through, so
        tombstoned entries stay encrypted-at-rest. The new payload
        is the tombstone form (markers + structural fields) — still
        sensitive enough to deserve encryption.
        """
        from .client import TombstoningBackend
        if not isinstance(self.inner, TombstoningBackend):
            return False
        with self._lock:
            dark = self.dark
        if dark is None:
            # Tombstone in plaintext (best we can do without DARK)
            return self.inner.rewrite_payload(offset, new_payload)
        encrypted = _encrypt_payload(
            new_payload, dark=dark, offset_hint=offset,
        )
        return self.inner.rewrite_payload(offset, encrypted)

    # ---- Diagnostics --------------------------------------------------------

    def passthrough_count(self) -> int:
        """Total appends + reads that went through unencrypted.
        Production should monitor this as a leakage indicator."""
        with self._lock:
            return self._passthrough_count


# =============================================================================
# Convenience: install on a LogClient
# =============================================================================


def wrap_log_with_dark(
    client: LogClient,
    dark: bytes | None,
    *,
    require_encryption: bool = False,
) -> EncryptedLogBackend:
    """Replace the client's backend with an EncryptedLogBackend.

    Returns the new backend so callers can later `set_dark(None)`
    on burn / `set_dark(new)` on key rotation.
    """
    inner = client._backend
    enc = EncryptedLogBackend(
        inner=inner, dark=dark, require_encryption=require_encryption,
    )
    client.set_backend(enc)
    return enc


__all__ = [
    "EncryptedLogBackend",
    "derive_dark",
    "wrap_log_with_dark",
]
