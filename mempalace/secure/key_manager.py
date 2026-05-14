"""
Cloud-box-side key manager.

Implements Track 5B from IMPLEMENTATION_ROADMAP.md per
ENCRYPTION_AT_EDGE_DESIGN.md (v2) §"Session-key lifecycle".

# What this is

The cloud-box-side counterpart to `PhoneSecureElement`. Receives
phone-issued `SessionKeyBundle`s, holds them in process memory while
the daemon is active, and idle-zeros them after configurable
inactivity.

This is the layer the v1 design got wrong by treating it as if it
were SE-grade. It isn't — keys live in process memory; live-memory
attacks succeed against it. The defense is bundle TTL + idle-zero +
binary attestation + audit log, NOT hardware isolation.

# State machine

    LOCKED_INITIAL                  (no bundle ever loaded)
        │ load_bundle(...)
        ▼
    LOADED_ACTIVE                   (bundle loaded, daemon working)
        │ idle_zero() called by watchdog
        ▼
    LOCKED_ZEROED                   (bundle wiped, decrypt raises KeysNotLoaded)
        │ load_bundle(...) with fresh bundle from phone
        ▼
    LOADED_ACTIVE                   (back to operation)

A separate terminal state `LOCKED_FAILED` is reached if a bundle
fails verification (signature mismatch, attestation mismatch, expired)
— recoverable only via fresh load.

# What this manager handles for the rest of the codebase

  - Decryption of drawer ciphertext + property ciphertext during
    miner passes / ranker calls / retrieval.
  - Idle detection via `record_activity()` calls from the rest of
    the daemon. The watchdog thread compares
    `now - last_activity_ms` to the threshold.
  - Diagnostic surfaces (`is_loaded()`, `current_state()`,
    `bundle_generation()`).

# What this manager does NOT do

  - Encrypt new content. That's the phone's job (PhoneSecureElement)
    or, in test setups, the SoftwarePhoneSE that pretends to be the
    phone in the same process.
  - Federation egress encryption. That uses per-sandbox FEKs derived
    from the bundle but managed by the federation-session module
    (sub-track 5F, not yet implemented).
  - Long-term key storage. The bundle is ephemeral by design.

Spec ref: ENCRYPTION_AT_EDGE_DESIGN.md v2 §"Session-key lifecycle",
R3 §7.4 (idle-zeroing), R3 §7.6 (heartbeat refresh)
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .element import (
    AttestationError,
    EncryptResult,
    KeyHandleError,
    SessionKeyBundle,
    _attestation_mac,
    _crypt_decrypt,
    _crypt_encrypt,
    _derive_dek,
)

logger = logging.getLogger(__name__)


# =============================================================================
# State + errors
# =============================================================================


class KeyManagerState(str, enum.Enum):
    LOCKED_INITIAL = "locked_initial"
    LOADED_ACTIVE = "loaded_active"
    LOCKED_ZEROED = "locked_zeroed"
    LOCKED_FAILED = "locked_failed"


class KeysNotLoaded(Exception):
    """Raised when a decrypt is attempted in a non-LOADED state."""


class BundleVerificationError(Exception):
    """Raised when a bundle fails signature/attestation/expiry checks."""


# =============================================================================
# Protocol
# =============================================================================


@runtime_checkable
class CloudBoxKeyManager(Protocol):
    """Cloud-box-side key manager interface."""

    def load_bundle(
        self,
        bundle: SessionKeyBundle,
        *,
        daemon_binary_attestation: bytes,
    ) -> None:
        """Verify and load a fresh bundle from the phone.

        On success, transitions to LOADED_ACTIVE.
        On failure (sig mismatch, expiry, attestation mismatch), raises
        BundleVerificationError; state goes LOCKED_FAILED.
        """
        ...

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        dek_handle: str,
        attestation_sig: bytes,
    ) -> bytes:
        """Decrypt content for in-daemon operation.

        Raises:
          KeysNotLoaded: state isn't LOADED_ACTIVE.
          KeyHandleError: handle malformed or bound to a different palace.
          AttestationError: signature mismatch.
        """
        ...

    def encrypt_for_egress(
        self,
        plaintext: bytes,
        *,
        sandbox_id: str,
        peer_pubkey: bytes,
    ) -> bytes:
        """Federation-egress encryption under a per-sandbox FEK.

        TODO(track-5F): full FEK derivation requires the federation
        sandbox manager. The skeleton here returns a deterministic
        derived ciphertext for testing; full implementation in
        sub-track 5F.
        """
        ...

    def encrypt_with_fek(
        self,
        plaintext: bytes,
        *,
        fek: bytes,
        sandbox_id: str,
    ) -> bytes:
        """Track 5F — symmetric encryption under a negotiated FEK.

        Both sides of a federation match know the FEK (derived via
        prior key-agreement; out of scope for this method). This
        method encrypts a payload under it.

        AAD includes the sandbox_id so payloads encrypted for one
        sandbox can't be replayed under another sandbox's FEK.
        """
        ...

    def decrypt_with_fek(
        self,
        ciphertext: bytes,
        *,
        fek: bytes,
        sandbox_id: str,
    ) -> bytes:
        """Track 5F — inverse of encrypt_with_fek.

        Raises:
          AttestationError: ciphertext failed authentication
            (tampered, wrong FEK, wrong sandbox_id).
        """
        ...

    def record_activity(self) -> None:
        """Reset the idle watchdog. Called by the daemon's request
        handler on every operation."""
        ...

    def idle_zero(self) -> None:
        """Wipe bundle material. Idempotent. Transitions to
        LOCKED_ZEROED. Watchdog calls this on the inactivity threshold."""
        ...

    def is_loaded(self) -> bool:
        """Diagnostic. Production code shouldn't branch on this."""
        ...

    def current_state(self) -> KeyManagerState:
        ...

    def bundle_generation(self) -> int:
        """Generation of the currently-loaded bundle (or 0 if none)."""
        ...


# =============================================================================
# SoftwareCloudBoxKM impl
# =============================================================================


@dataclass
class _LoadedBundle:
    """The unwrapped key material plus identifying metadata."""

    master_key: bytes
    palace_id: str
    bundle_id: str
    generation: int
    issued_at_ms: int
    expires_at_ms: int


class SoftwareCloudBoxKM:
    """Software cloud-box key manager.

    Mirrors the production interface; uses the SoftwarePhoneSE's
    bundle wrap format. Production replaces this with whatever the
    real cloud-box daemon implementation looks like (the algorithm
    is the same; the impl details may vary).

    Idle-zero implementation: when called, the master_key bytes are
    overwritten with zeros (best effort — Python's immutable bytes
    semantics mean the *binding* is rewritten, not necessarily the
    underlying memory; production C/Rust impls would do explicit
    memset).
    """

    DEFAULT_IDLE_THRESHOLD_SEC = 15 * 60  # 15 minutes per design

    def __init__(
        self,
        *,
        idle_threshold_sec: float = DEFAULT_IDLE_THRESHOLD_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._state = KeyManagerState.LOCKED_INITIAL
        self._loaded: _LoadedBundle | None = None
        self._last_activity_ms = 0
        self._idle_threshold_sec = idle_threshold_sec
        # The watchdog is opt-in (start_watchdog()). Tests use manual
        # idle_zero() instead so they don't have to wait.
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    # -------- protocol surface --------------------------------------------------

    def load_bundle(
        self,
        bundle: SessionKeyBundle,
        *,
        daemon_binary_attestation: bytes,
    ) -> None:
        with self._lock:
            try:
                loaded = self._verify_and_unwrap(bundle, daemon_binary_attestation)
            except BundleVerificationError:
                self._state = KeyManagerState.LOCKED_FAILED
                self._loaded = None
                raise

            self._loaded = loaded
            self._state = KeyManagerState.LOADED_ACTIVE
            self._last_activity_ms = int(time.time() * 1000)
            logger.info(
                "Cloud-box key manager loaded bundle generation=%d for palace=%s; "
                "expires at %d ms (TTL %.1fh)",
                loaded.generation,
                loaded.palace_id,
                loaded.expires_at_ms,
                (loaded.expires_at_ms - loaded.issued_at_ms) / 3600_000.0,
            )

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        dek_handle: str,
        attestation_sig: bytes,
    ) -> bytes:
        with self._lock:
            self._check_loaded_or_raise()

            # Check expiry (lazy; the watchdog also catches it but a
            # decrypt that arrives between watchdog ticks should also
            # fail).
            self._check_expiry_or_raise()

            assert self._loaded is not None  # narrowed by _check_loaded_or_raise
            master_key = self._loaded.master_key
            palace_id = self._loaded.palace_id

            # Parse the dek_handle. Same format as SoftwarePhoneSE
            # produces: `seh1:{palace_id}:{purpose}:{context_hex}:{label}`
            try:
                key_purpose, context = self._parse_handle(dek_handle, palace_id)
            except ValueError as e:
                raise KeyHandleError(str(e))

            expected_sig = _attestation_mac(
                master_key, ciphertext, dek_handle, key_purpose, context
            )
            if not hmac.compare_digest(expected_sig, attestation_sig):
                raise AttestationError("attestation signature does not match")

            dek = _derive_dek(master_key, key_purpose, context)
            try:
                plaintext = _crypt_decrypt(
                    dek,
                    ciphertext,
                    aad=key_purpose.encode("utf-8") + b"\x00" + context,
                )
            except AttestationError:
                raise
            except Exception as e:
                raise AttestationError(f"decryption failed: {e}")

            self._last_activity_ms = int(time.time() * 1000)
            return plaintext

    def encrypt_for_egress(
        self,
        plaintext: bytes,
        *,
        sandbox_id: str,
        peer_pubkey: bytes,
    ) -> bytes:
        """Skeleton. Track 5F is the full federation-egress path.

        For now: derives a sandbox-specific key from the master key +
        sandbox_id + peer_pubkey, encrypts the plaintext under it.
        Real impl will use proper key agreement (X25519 or similar)
        with the peer's enrolled federation pubkey.
        """
        with self._lock:
            self._check_loaded_or_raise()
            assert self._loaded is not None

            fek = hmac.new(
                self._loaded.master_key,
                b"fek-v1-skeleton" + sandbox_id.encode("utf-8") + b"\x00" + peer_pubkey,
                hashlib.sha256,
            ).digest()
            self._last_activity_ms = int(time.time() * 1000)
            return _crypt_encrypt(
                fek,
                plaintext,
                aad=b"fek-egress-v1" + sandbox_id.encode("utf-8"),
            )

    def encrypt_with_fek(
        self,
        plaintext: bytes,
        *,
        fek: bytes,
        sandbox_id: str,
    ) -> bytes:
        """Track 5F — symmetric encryption under a caller-supplied FEK.

        The FEK comes from the federation match negotiation (prior
        key-agreement, e.g. X25519 between session keys). Both ends
        of the match end up with the same FEK; this method is what
        each end calls to wrap finding payloads.

        Doesn't require the bundle to be loaded — FEK encryption is a
        symmetric primitive parameterized only by the FEK + AAD. We
        still record activity to signal the daemon is active.
        """
        if not isinstance(fek, (bytes, bytearray)) or len(fek) < 16:
            raise ValueError("fek must be at least 16 bytes")
        with self._lock:
            self._last_activity_ms = int(time.time() * 1000)
            return _crypt_encrypt(
                bytes(fek)[:32],  # truncate / pad to 32 if needed
                plaintext,
                aad=b"fek-payload-v1:" + sandbox_id.encode("utf-8"),
            )

    def decrypt_with_fek(
        self,
        ciphertext: bytes,
        *,
        fek: bytes,
        sandbox_id: str,
    ) -> bytes:
        """Track 5F — inverse of encrypt_with_fek.

        AAD must match the encrypt side: same sandbox_id. AES-GCM
        catches tampering / wrong-FEK / wrong-sandbox automatically
        via the auth tag.
        """
        if not isinstance(fek, (bytes, bytearray)) or len(fek) < 16:
            raise ValueError("fek must be at least 16 bytes")
        with self._lock:
            self._last_activity_ms = int(time.time() * 1000)
            try:
                return _crypt_decrypt(
                    bytes(fek)[:32],
                    ciphertext,
                    aad=b"fek-payload-v1:" + sandbox_id.encode("utf-8"),
                )
            except Exception as e:
                raise AttestationError(f"FEK decryption failed: {e}")

    def record_activity(self) -> None:
        with self._lock:
            if self._state == KeyManagerState.LOADED_ACTIVE:
                self._last_activity_ms = int(time.time() * 1000)

    def idle_zero(self) -> None:
        with self._lock:
            if self._state == KeyManagerState.LOADED_ACTIVE:
                logger.info(
                    "Cloud-box key manager idle-zeroing (was generation=%d)",
                    self._loaded.generation if self._loaded else 0,
                )
            if self._loaded is not None:
                # Best-effort wipe. Python's immutable bytes mean we
                # rebind rather than overwriting in place; production
                # C/Rust impls do explicit memset.
                self._loaded = _LoadedBundle(
                    master_key=b"\x00" * len(self._loaded.master_key),
                    palace_id="",
                    bundle_id="",
                    generation=0,
                    issued_at_ms=0,
                    expires_at_ms=0,
                )
                self._loaded = None
            self._state = KeyManagerState.LOCKED_ZEROED

    def is_loaded(self) -> bool:
        with self._lock:
            return self._state == KeyManagerState.LOADED_ACTIVE

    def current_state(self) -> KeyManagerState:
        with self._lock:
            return self._state

    def bundle_generation(self) -> int:
        with self._lock:
            return self._loaded.generation if self._loaded else 0

    # -------- watchdog ---------------------------------------------------------

    def start_watchdog(self, *, tick_sec: float = 60.0) -> None:
        """Spawn the inactivity watchdog thread. Production daemons
        call this after load_bundle; tests usually don't (they call
        idle_zero() manually)."""
        with self._lock:
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                args=(tick_sec,),
                name="cloudbox-km-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def stop_watchdog(self, *, timeout: float = 5.0) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=timeout)
            self._watchdog_thread = None

    def _watchdog_loop(self, tick_sec: float) -> None:
        while not self._watchdog_stop.is_set():
            try:
                self._watchdog_check()
            except Exception as e:
                logger.warning("Watchdog tick failed: %s", e)
            self._watchdog_stop.wait(tick_sec)

    def _watchdog_check(self) -> None:
        with self._lock:
            if self._state != KeyManagerState.LOADED_ACTIVE:
                return
            now_ms = int(time.time() * 1000)
            idle_ms = now_ms - self._last_activity_ms
            if idle_ms / 1000.0 >= self._idle_threshold_sec:
                logger.info(
                    "Watchdog: idle threshold reached (%.1fs >= %.1fs); idle-zeroing",
                    idle_ms / 1000.0,
                    self._idle_threshold_sec,
                )
                self.idle_zero()
                return
            # Also catch hard expiry
            if now_ms >= (self._loaded.expires_at_ms if self._loaded else 0):
                logger.info("Watchdog: bundle expired; idle-zeroing")
                self.idle_zero()

    # -------- internals --------------------------------------------------------

    def _check_loaded_or_raise(self) -> None:
        if self._state != KeyManagerState.LOADED_ACTIVE:
            raise KeysNotLoaded(
                f"key manager state is {self._state.value}; "
                f"need {KeyManagerState.LOADED_ACTIVE.value}"
            )

    def _check_expiry_or_raise(self) -> None:
        assert self._loaded is not None
        now_ms = int(time.time() * 1000)
        if now_ms >= self._loaded.expires_at_ms:
            # Promote to zeroed state; subsequent calls fail cleanly
            self.idle_zero()
            raise KeysNotLoaded("bundle expired")

    def _verify_and_unwrap(
        self,
        bundle: SessionKeyBundle,
        daemon_binary_attestation: bytes,
    ) -> _LoadedBundle:
        """Verify bundle signature, expiry, daemon attestation; unwrap blob."""

        now_ms = int(time.time() * 1000)
        if now_ms >= bundle.expires_at_ms:
            raise BundleVerificationError(
                f"bundle already expired (now={now_ms} >= expires={bundle.expires_at_ms})"
            )

        if bundle.daemon_attestation != daemon_binary_attestation:
            raise BundleVerificationError(
                "bundle's daemon_attestation doesn't match this daemon's binary hash"
            )

        # Unwrap the blob. Per the SoftwarePhoneSE format, the blob is
        # AES-GCM (or fallback)-encrypted master key under a wrap key
        # derived from (master_key, "bundle-wrap-v1", daemon_attestation).
        # We don't have the master_key yet — that's what's IN the blob.
        # The wrap key derivation is symmetric, so we can re-derive it
        # only if we know the master key, which we don't yet.
        #
        # That's a chicken-and-egg in the SOFTWARE impl. The production
        # impl uses public-key encryption: phone wraps with cloud-box
        # public key, cloud-box unwraps with its private key (which is
        # held in the daemon's TPM or equivalent).
        #
        # For the software impl, we cheat: the bundle_blob's wrap key
        # is derived from the bundle_signature itself, which is over
        # the master key indirectly via the bundle_id+gen+timestamps.
        # The signature was made by the phone with the master key, so
        # we accept the bundle if and only if the signature verifies
        # under the master key WE EXTRACT. This is circular in the
        # software case but it preserves the test surface.
        #
        # The honest interface contract: in the real world, the cloud
        # box has its own keypair and the phone wraps to that public
        # key. The software impl uses a pre-shared HMAC for simplicity.
        # Verifying the bundle signature thus requires reading the
        # blob; we do trial-decrypt against a fixed wrap key:

        # Derive wrap key from daemon_attestation alone (test path).
        # In reality this would be the cloud box's own private key
        # decapsulating something the phone encrypted to the cloud
        # box's public key.
        trial_wrap = hmac.new(
            b"sw-cloudbox-test-wrap-key",
            b"bundle-wrap-v1" + bundle.daemon_attestation,
            hashlib.sha256,
        ).digest()

        # The SoftwarePhoneSE's bundle_blob was wrapped with the master
        # key + daemon_attestation, but we don't have the master key.
        # For the test path, the SoftwarePhoneSE's bundle_blob layout
        # is just _crypt_encrypt(wrap_key, master_key, aad=...). We
        # can't decrypt without the master key.
        #
        # SOLUTION: treat the SoftwarePhoneSE bundle differently.
        # Instead of trying to unwrap, we rely on the test fixture
        # passing both the SE and the manager and using a shared
        # pre-shared wrap key. Refactor the SoftwarePhoneSE to use
        # this same `trial_wrap`-style wrap key.
        #
        # For now: extract the master key directly from the wrap_key
        # path the SoftwarePhoneSE actually used. We re-derive that
        # using the master key, which we get by trial-decrypting
        # the blob with various candidate keys... this is getting
        # absurd. Let me just co-locate the wrap-key derivation
        # between the two software impls.
        #
        # See `_derive_software_wrap_key` below; both
        # SoftwarePhoneSE and SoftwareCloudBoxKM call it.

        try:
            master_key = _crypt_decrypt(
                _derive_software_wrap_key(bundle.daemon_attestation),
                bundle.bundle_blob,
                aad=b"bundle-v1" + bundle.daemon_attestation,
            )
        except Exception as e:
            raise BundleVerificationError(f"bundle blob unwrap failed: {e}")

        # Now verify the bundle signature with the unwrapped master key
        sig_input = (
            bundle.bundle_id.encode("utf-8")
            + b"\x00"
            + bundle.generation.to_bytes(8, "big")
            + b"\x00"
            + bundle.issued_at_ms.to_bytes(8, "big")
            + b"\x00"
            + bundle.expires_at_ms.to_bytes(8, "big")
            + b"\x00"
            + bundle.bundle_blob
            + b"\x00"
            + bundle.daemon_attestation
        )
        expected_sig = hmac.new(
            master_key,
            b"bundle-sig-v1" + sig_input,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_sig, bundle.bundle_signature):
            raise BundleVerificationError("bundle signature mismatch")

        palace_id = (
            "palace_"
            + hashlib.sha256(b"id-v1" + master_key).hexdigest()[:16]
        )

        return _LoadedBundle(
            master_key=master_key,
            palace_id=palace_id,
            bundle_id=bundle.bundle_id,
            generation=bundle.generation,
            issued_at_ms=bundle.issued_at_ms,
            expires_at_ms=bundle.expires_at_ms,
        )

    @staticmethod
    def _parse_handle(handle: str, expected_palace_id: str) -> tuple[str, bytes]:
        """Same shape as SoftwarePhoneSE._parse_handle, but checks
        against this manager's loaded palace_id."""
        parts = handle.split(":", 4)
        if len(parts) != 5 or parts[0] != "seh1":
            raise ValueError(f"malformed handle: {handle!r}")
        _version, palace_id, key_purpose, context_hex, _label = parts
        if palace_id != expected_palace_id:
            raise ValueError(
                f"handle bound to {palace_id}, manager loaded for {expected_palace_id}"
            )
        try:
            context = bytes.fromhex(context_hex)
        except ValueError:
            raise ValueError(f"context_hex not valid hex: {context_hex!r}")
        return key_purpose, context


# =============================================================================
# Software-only bundle wrap-key derivation
# =============================================================================
#
# This is the concession the v1 design hand-waved over and the v2
# design made explicit: in production, bundle blobs are encrypted with
# public-key cryptography (phone wraps to cloud box's public key). In
# the software impl, we use a deterministic shared key.
#
# Both SoftwarePhoneSE and SoftwareCloudBoxKM use the same derivation
# here. Production replaces this with real PKE.


def _derive_software_wrap_key(daemon_attestation: bytes) -> bytes:
    """Software-only deterministic bundle wrap key.

    NOT what production does. Production uses:
      bundle_blob = encrypt_to_pubkey(cloud_box_pubkey, master_key)
    where cloud_box_pubkey is registered at enrollment.

    For tests, we use a fixed-secret HMAC keyed by daemon_attestation
    so any SoftwarePhoneSE + SoftwareCloudBoxKM pair can interoperate
    in the same process.
    """
    return hmac.new(
        b"sw-pse-cbkm-shared-wrap-key-v1",
        b"bundle-wrap-v1" + daemon_attestation,
        hashlib.sha256,
    ).digest()


__all__ = [
    "BundleVerificationError",
    "CloudBoxKeyManager",
    "KeyManagerState",
    "KeysNotLoaded",
    "SoftwareCloudBoxKM",
]
