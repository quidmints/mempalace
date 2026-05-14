"""
Phone-side secure element abstractions.

Implements Track 5A from IMPLEMENTATION_ROADMAP.md per
ENCRYPTION_AT_EDGE_DESIGN.md (v2).

# What's here

  - `PhoneSecureElement` Protocol — interface that both production
    (real iOS/Android SE bindings) and development (Software impl)
    satisfy.
  - `SoftwarePhoneSE` — pure-Python AES-GCM impl with the
    Phone Master Key in process memory. **NOT production-grade.**
    Logs a stark warning on first encrypt.
  - `EncryptResult`, `SessionKeyBundle`, `KeyHandleError` — shared
    types.

# What's NOT here (lives in `key_manager.py`)

  - `CloudBoxKeyManager` — the cloud-box-side counterpart that
    receives session-key bundles. Different security primitives
    (in-memory + idle-zero, not hardware-isolated).

# Why two different abstractions

The phone has hardware-isolated key storage; raw key bytes never
leave the SE. The cloud box is software-only; raw key bytes ARE in
process memory while the daemon runs. Conflating them in one
"SecureElement" Protocol (as the v1 design did) was the mistake
the v2 design corrected.

# Why no real-hardware impl yet

Hardware bindings (iOS Secure Enclave via CryptoKit, Android
StrongBox via Keystore) require platform-specific code that this
Python codebase doesn't host. The phone-side production code lives
in whichever app framework the user's mobile client is built on
(Swift / Kotlin). What this module provides is the protocol contract
the app framework's bindings have to satisfy, plus the software impl
for tests and dev.

Spec ref: ENCRYPTION_AT_EDGE_DESIGN.md v2 §"SecureElement interface
(revised)", R3 §7.4
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# AES-GCM via cryptography lib if available; otherwise software fallback
# using HMAC-SHA256 + ChaCha-style streaming. The cryptography lib is
# the production choice; the fallback is so tests can run without the
# dep installed.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    _AESGCM_AVAILABLE = True
except ImportError:
    AESGCM = None  # type: ignore
    _AESGCM_AVAILABLE = False
    logger.warning(
        "cryptography library not available; falling back to "
        "HMAC-derived stream cipher. Acceptable for tests; NOT for "
        "production. Install `cryptography` to use AES-GCM."
    )


# =============================================================================
# Result types
# =============================================================================


@dataclass(frozen=True)
class EncryptResult:
    """What `encrypt_drawer` and friends return.

    The triple (ciphertext, dek_handle, attestation_sig) goes into the
    log event verbatim. Decryption requires all three.
    """

    ciphertext: bytes
    """The encrypted bytes. Includes nonce + tag for AES-GCM."""

    dek_handle: str
    """Opaque handle naming the DEK that was used. The Phone SE knows
    how to derive the actual key bytes from this handle + the master
    key. The cloud box receives the same handle and asks the
    CloudBoxKeyManager to decrypt."""

    attestation_sig: bytes
    """HMAC over (ciphertext, dek_handle, key_purpose, context).
    Catches any tampering with the on-disk ciphertext after capture."""


@dataclass(frozen=True)
class SessionKeyBundle:
    """A TTL'd release from the phone to the cloud box.

    Sent over TLS during daemon startup challenge-response. The cloud
    box loads it into its `CloudBoxKeyManager`; the bundle is good
    until `expires_at_ms` or the watchdog idle-zeros it.
    """

    bundle_id: str
    """Unique per-release. Used to detect replay."""

    generation: int
    """Monotonic. The cloud box stamps each ciphertext write with the
    current bundle generation so stale-bundle decryption can be
    detected."""

    issued_at_ms: int
    expires_at_ms: int

    bundle_blob: bytes
    """Serialized key material. Encrypted by the phone under a
    challenge-response key derived from the daemon's binary
    attestation. The cloud box's CloudBoxKeyManager unwraps this
    before storing the actual derivation keys."""

    daemon_attestation: bytes
    """The daemon binary hash this bundle was issued for. The cloud
    box must verify its own current binary matches before unwrapping.
    Defense against bundle being shipped to a tampered daemon."""

    bundle_signature: bytes
    """Phone's signature over the rest of the fields. Lets the cloud
    box verify the bundle came from the enrolled phone."""


# =============================================================================
# Errors
# =============================================================================


class KeyHandleError(Exception):
    """Raised when a DEK handle isn't bound to this SE / palace."""


class AttestationError(Exception):
    """Raised when an attestation_sig doesn't verify."""


class RevokedError(Exception):
    """Raised after `revoke_palace()` was called. All subsequent ops fail."""


# =============================================================================
# Protocol
# =============================================================================


@runtime_checkable
class PhoneSecureElement(Protocol):
    """Phone-side secure element interface.

    Production implementations bind to iOS Secure Enclave (via
    CryptoKit) or Android StrongBox (via Keystore). Development uses
    `SoftwarePhoneSE`.
    """

    def palace_id(self) -> str:
        """Stable identifier for this palace. Derived from the master
        key; not the master key itself. Used for binding checks."""
        ...

    def encrypt_drawer(
        self,
        plaintext: bytes,
        *,
        drawer_id: str,
    ) -> EncryptResult:
        """Encrypt drawer content.

        The result's `dek_handle` is bound to this drawer_id; passing
        it with mismatched ciphertext fails decryption.
        """
        ...

    def encrypt_property(
        self,
        plaintext: bytes,
        *,
        node_id: str,
        field_name: str,
    ) -> EncryptResult:
        """Encrypt an assertion property value. Bound to (node_id,
        field_name) so the same encrypted bytes can't be replayed
        into a different field."""
        ...

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        dek_handle: str,
        attestation_sig: bytes,
    ) -> bytes:
        """Phone-side decryption. Used for in-app drawer view +
        chunked plaintext export. The cloud box does NOT call this.

        Raises:
          KeyHandleError: handle not bound to this SE.
          AttestationError: sig doesn't verify against ciphertext.
          RevokedError: palace was burned.
        """
        ...

    def release_session_bundle(
        self,
        *,
        daemon_attestation: bytes,
        ttl_seconds: int = 24 * 3600,
    ) -> SessionKeyBundle:
        """Issue a TTL'd bundle to the cloud-box daemon.

        Called by the daemon's startup challenge-response. The bundle
        is encrypted to the daemon's binary attestation; a tampered
        daemon can't unwrap it.
        """
        ...

    def revoke_palace(self) -> None:
        """Destroy the Phone Master Key. Permanently irreversible.

        After this:
          - All subsequent encrypt/decrypt ops raise RevokedError.
          - No new session bundles can be issued.
          - Existing bundles in cloud-box memory continue to work
            until their TTL expires; idle-zeroing finishes them off.
          - Existing on-disk ciphertext becomes permanently
            unrecoverable.

        UI-facing burn-the-palace flow per
        USER_VIEW_AND_DELETE_DESIGN.md §"Burning the palace".
        """
        ...

    def is_revoked(self) -> bool:
        """Diagnostic. Production code shouldn't branch on this."""
        ...


# =============================================================================
# SoftwarePhoneSE — dev/test impl
# =============================================================================


# Used by both the AES-GCM and fallback paths to derive purpose-scoped
# DEKs from the master key. HKDF-shaped (extract-then-expand).
def _derive_dek(master_key: bytes, purpose: str, context: bytes) -> bytes:
    """Derive a 32-byte DEK from master + purpose + context.

    Equivalent to HKDF(master_key, info=purpose||context). Replaceable
    with a real HKDF impl when `cryptography` is available; using
    raw HMAC is fine for the software fallback's purposes (this is
    NOT production-grade anyway).
    """
    info = purpose.encode("utf-8") + b"\x00" + context
    # Extract
    prk = hmac.new(b"mempalace-phone-se-v1", master_key, hashlib.sha256).digest()
    # Expand to 32 bytes (one block of SHA-256 = 32 bytes; sufficient)
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _attestation_mac(
    master_key: bytes,
    ciphertext: bytes,
    dek_handle: str,
    key_purpose: str,
    context: bytes,
) -> bytes:
    """HMAC-SHA256 over the binding fields. Detects on-disk tampering."""
    h = hmac.new(master_key, b"attestation-v1", hashlib.sha256)
    h.update(len(ciphertext).to_bytes(8, "big"))
    h.update(ciphertext)
    h.update(b"\x00")
    h.update(dek_handle.encode("utf-8"))
    h.update(b"\x00")
    h.update(key_purpose.encode("utf-8"))
    h.update(b"\x00")
    h.update(context)
    return h.digest()


def _aes_gcm_encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    """AES-GCM encrypt. Returns nonce || ciphertext_with_tag."""
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, aad)
    return nonce + ct


def _aes_gcm_decrypt(key: bytes, blob: bytes, *, aad: bytes) -> bytes:
    """AES-GCM decrypt. Inverse of _aes_gcm_encrypt."""
    if len(blob) < 12 + 16:
        raise AttestationError("ciphertext too short to be AES-GCM")
    nonce = blob[:12]
    ct = blob[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, aad)


def _fallback_encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    """Software-fallback encrypt. HMAC-derived keystream + HMAC tag.
    NOT cryptographically equivalent to AES-GCM. Tests-only.
    """
    nonce = secrets.token_bytes(12)
    keystream = b""
    counter = 0
    while len(keystream) < len(plaintext):
        block = hmac.new(
            key,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        keystream += block
        counter += 1
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, keystream))
    tag = hmac.new(
        key,
        b"tag-v1" + nonce + ciphertext + aad,
        hashlib.sha256,
    ).digest()[:16]
    return nonce + ciphertext + tag


def _fallback_decrypt(key: bytes, blob: bytes, *, aad: bytes) -> bytes:
    if len(blob) < 12 + 16:
        raise AttestationError("ciphertext too short for fallback cipher")
    nonce = blob[:12]
    ciphertext = blob[12:-16]
    tag = blob[-16:]
    expected = hmac.new(
        key,
        b"tag-v1" + nonce + ciphertext + aad,
        hashlib.sha256,
    ).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise AttestationError("fallback-cipher MAC failed")
    keystream = b""
    counter = 0
    while len(keystream) < len(ciphertext):
        block = hmac.new(
            key,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        keystream += block
        counter += 1
    return bytes(c ^ k for c, k in zip(ciphertext, keystream))


def _crypt_encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    if _AESGCM_AVAILABLE:
        return _aes_gcm_encrypt(key, plaintext, aad=aad)
    return _fallback_encrypt(key, plaintext, aad=aad)


def _crypt_decrypt(key: bytes, blob: bytes, *, aad: bytes) -> bytes:
    if _AESGCM_AVAILABLE:
        try:
            return _aes_gcm_decrypt(key, blob, aad=aad)
        except Exception as e:
            raise AttestationError(f"AES-GCM decryption failed: {e}")
    return _fallback_decrypt(key, blob, aad=aad)


# Stark warning shown once per process when SoftwarePhoneSE first encrypts
_WARNING_SHOWN = False
_WARNING_LOCK = threading.Lock()


def _show_warning_once() -> None:
    global _WARNING_SHOWN
    with _WARNING_LOCK:
        if _WARNING_SHOWN:
            return
        _WARNING_SHOWN = True
    logger.warning(
        "================================================================\n"
        "SoftwarePhoneSE in use. This is NOT production-grade key storage.\n"
        "The Phone Master Key is in process memory. Use ONLY for tests\n"
        "and development. Production must bind to iOS Secure Enclave or\n"
        "Android StrongBox.\n"
        "================================================================"
    )


class SoftwarePhoneSE:
    """Pure-Python phone secure element. Tests-and-dev only.

    Generates a Phone Master Key at construction (or accepts one for
    deterministic testing). All keys derived from it via HKDF-shaped
    expansion.
    """

    KEY_PURPOSE_DRAWER = "drawer.verbatim"
    KEY_PURPOSE_PROPERTY = "node.property"
    KEY_PURPOSE_BUNDLE = "session.bundle"

    def __init__(
        self,
        *,
        master_key: bytes | None = None,
        palace_id: str | None = None,
    ) -> None:
        if master_key is not None and len(master_key) != 32:
            raise ValueError("master_key must be 32 bytes")
        self._master_key = master_key or secrets.token_bytes(32)
        # Palace id is a deterministic hash of the master key. Used for
        # cross-checking that a dek_handle came from this SE.
        if palace_id is None:
            palace_id = (
                "palace_"
                + hashlib.sha256(b"id-v1" + self._master_key).hexdigest()[:16]
            )
        self._palace_id = palace_id
        self._revoked = False
        self._lock = threading.Lock()
        self._bundle_generation = 0

    # -------- protocol surface --------------------------------------------------

    def palace_id(self) -> str:
        return self._palace_id

    def encrypt_drawer(
        self,
        plaintext: bytes,
        *,
        drawer_id: str,
    ) -> EncryptResult:
        return self._encrypt(
            plaintext,
            key_purpose=self.KEY_PURPOSE_DRAWER,
            context=drawer_id.encode("utf-8"),
            handle_label=f"drawer/{drawer_id}",
        )

    def encrypt_property(
        self,
        plaintext: bytes,
        *,
        node_id: str,
        field_name: str,
    ) -> EncryptResult:
        ctx = f"{node_id}|{field_name}".encode("utf-8")
        return self._encrypt(
            plaintext,
            key_purpose=self.KEY_PURPOSE_PROPERTY,
            context=ctx,
            handle_label=f"prop/{node_id}/{field_name}",
        )

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        dek_handle: str,
        attestation_sig: bytes,
    ) -> bytes:
        with self._lock:
            if self._revoked:
                raise RevokedError("Phone Master Key revoked")

            try:
                key_purpose, context = self._parse_handle(dek_handle)
            except ValueError as e:
                raise KeyHandleError(str(e))

            expected_sig = _attestation_mac(
                self._master_key,
                ciphertext,
                dek_handle,
                key_purpose,
                context,
            )
            if not hmac.compare_digest(expected_sig, attestation_sig):
                raise AttestationError(
                    "attestation signature does not match"
                )

            dek = _derive_dek(self._master_key, key_purpose, context)
            try:
                return _crypt_decrypt(
                    dek,
                    ciphertext,
                    aad=key_purpose.encode("utf-8") + b"\x00" + context,
                )
            except AttestationError:
                raise
            except Exception as e:
                raise AttestationError(f"decryption failed: {e}")

    def release_session_bundle(
        self,
        *,
        daemon_attestation: bytes,
        ttl_seconds: int = 24 * 3600,
    ) -> SessionKeyBundle:
        with self._lock:
            if self._revoked:
                raise RevokedError("Phone Master Key revoked")
            self._bundle_generation += 1
            generation = self._bundle_generation

            now_ms = int(time.time() * 1000)
            bundle_id = secrets.token_hex(16)

            # The bundle blob carries the master key wrapped with a
            # daemon-attestation-bound key. In production this uses
            # public-key crypto (phone wraps to the cloud box's
            # registered public key). For the software impl, we use
            # a deterministic shared HMAC derivation that
            # SoftwareCloudBoxKM also knows. See
            # `_derive_software_wrap_key` in key_manager.py.
            #
            # Late-import to avoid a circular dependency on module
            # load — both modules import from each other lazily.
            from .key_manager import _derive_software_wrap_key

            wrap_key = _derive_software_wrap_key(daemon_attestation)
            bundle_blob = _crypt_encrypt(
                wrap_key,
                self._master_key,
                aad=b"bundle-v1" + daemon_attestation,
            )

            # Sign the bundle so the cloud box can verify it came
            # from us
            sig_input = (
                bundle_id.encode("utf-8")
                + b"\x00"
                + generation.to_bytes(8, "big")
                + b"\x00"
                + now_ms.to_bytes(8, "big")
                + b"\x00"
                + (now_ms + ttl_seconds * 1000).to_bytes(8, "big")
                + b"\x00"
                + bundle_blob
                + b"\x00"
                + daemon_attestation
            )
            bundle_signature = hmac.new(
                self._master_key,
                b"bundle-sig-v1" + sig_input,
                hashlib.sha256,
            ).digest()

            return SessionKeyBundle(
                bundle_id=bundle_id,
                generation=generation,
                issued_at_ms=now_ms,
                expires_at_ms=now_ms + ttl_seconds * 1000,
                bundle_blob=bundle_blob,
                daemon_attestation=daemon_attestation,
                bundle_signature=bundle_signature,
            )

    def revoke_palace(self) -> None:
        with self._lock:
            if self._revoked:
                return
            # Zero the master key buffer (best-effort; Python doesn't
            # really let us — bytes are immutable. The intent is to
            # make any future `decrypt` call fail rather than emit
            # plaintext.)
            self._master_key = b"\x00" * 32
            self._revoked = True
        logger.warning(
            "SoftwarePhoneSE.revoke_palace() called — palace %s is "
            "now permanently revoked",
            self._palace_id,
        )

    def is_revoked(self) -> bool:
        return self._revoked

    # -------- internals --------------------------------------------------------

    def _encrypt(
        self,
        plaintext: bytes,
        *,
        key_purpose: str,
        context: bytes,
        handle_label: str,
    ) -> EncryptResult:
        with self._lock:
            if self._revoked:
                raise RevokedError("Phone Master Key revoked")
            _show_warning_once()

            dek_handle = self._make_handle(key_purpose, context, handle_label)
            dek = _derive_dek(self._master_key, key_purpose, context)
            ciphertext = _crypt_encrypt(
                dek,
                plaintext,
                aad=key_purpose.encode("utf-8") + b"\x00" + context,
            )
            attestation_sig = _attestation_mac(
                self._master_key,
                ciphertext,
                dek_handle,
                key_purpose,
                context,
            )
            return EncryptResult(
                ciphertext=ciphertext,
                dek_handle=dek_handle,
                attestation_sig=attestation_sig,
            )

    def _make_handle(self, key_purpose: str, context: bytes, label: str) -> str:
        """Encode (purpose, context) into an opaque handle.

        Format: `seh1:{palace_id}:{purpose}:{context_hex}:{label}`
        The palace_id binds the handle to this SE; cross-palace
        decryption attempts fail at the parse step.
        """
        return (
            "seh1:"
            f"{self._palace_id}:"
            f"{key_purpose}:"
            f"{context.hex()}:"
            f"{label}"
        )

    def _parse_handle(self, handle: str) -> tuple[str, bytes]:
        """Inverse of _make_handle. Returns (key_purpose, context)."""
        parts = handle.split(":", 4)
        if len(parts) != 5 or parts[0] != "seh1":
            raise ValueError(f"malformed handle: {handle!r}")
        version, palace_id, key_purpose, context_hex, _label = parts
        if palace_id != self._palace_id:
            raise ValueError(
                f"handle bound to {palace_id}, this SE is {self._palace_id}"
            )
        try:
            context = bytes.fromhex(context_hex)
        except ValueError:
            raise ValueError(f"context_hex not valid hex: {context_hex!r}")
        return key_purpose, context


__all__ = [
    "AttestationError",
    "EncryptResult",
    "KeyHandleError",
    "PhoneSecureElement",
    "RevokedError",
    "SessionKeyBundle",
    "SoftwarePhoneSE",
]
