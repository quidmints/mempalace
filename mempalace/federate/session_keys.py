"""
Per-session keypair management.

Per R3 §7.3: each federation match session uses a fresh keypair generated
at session start and destroyed at session end. The keypair never touches
disk; it lives in the platform's hardware-bound keystore for its lifetime.

Lifecycle:
  - generate(): allocate keypair, return session_key_id
  - get_pubkey(session_key_id): return the public half (for sharing)
  - sign(session_key_id, payload): produce signature
  - destroy(session_key_id): zero-out and free

In dev mode, keys are generated in-process via Ed25519. Production
plugs in StrongBox / Secure Enclave / TPM-bound keys.

Spec ref: R3 §7.3.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SessionKey:
    """One in-memory session keypair."""

    session_key_id: str
    pubkey_hex: str
    privkey_bytes: bytes  # zeroed on destroy
    created_at_ms: int
    destroyed_at_ms: int | None = None

    @property
    def is_destroyed(self) -> bool:
        return self.destroyed_at_ms is not None


class SessionKeyManager:
    """Holds active session keys for the lifetime of their use."""

    def __init__(self) -> None:
        self._keys: dict[str, SessionKey] = {}
        self._lock = threading.Lock()

    def generate(self, *, dev_mode: bool = True) -> str:
        """Generate a new session keypair and return its session_key_id.

        In dev_mode: pure-Python Ed25519 from `cryptography` if available,
        else random bytes (for shape testing).
        Production: hardware-backed via platform keystore.
        """
        session_key_id = "skey_" + secrets.token_hex(8)
        now = int(time.time() * 1000)

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PublicFormat,
            )
            priv = Ed25519PrivateKey.generate()
            pub = priv.public_key()
            pub_bytes = pub.public_bytes(
                encoding=Encoding.Raw,
                format=PublicFormat.Raw,
            )
            priv_bytes = priv.private_bytes_raw()
        except ImportError:
            # No cryptography — use random bytes as placeholder
            priv_bytes = secrets.token_bytes(32)
            pub_bytes = secrets.token_bytes(32)

        key = SessionKey(
            session_key_id=session_key_id,
            pubkey_hex=pub_bytes.hex(),
            privkey_bytes=priv_bytes,
            created_at_ms=now,
        )
        with self._lock:
            self._keys[session_key_id] = key
        return session_key_id

    def get_pubkey(self, session_key_id: str) -> str | None:
        with self._lock:
            key = self._keys.get(session_key_id)
            if key is None or key.is_destroyed:
                return None
            return key.pubkey_hex

    def sign(self, session_key_id: str, payload: bytes) -> bytes | None:
        """Sign payload with the session's private key.

        Returns None if the key is destroyed or doesn't exist.
        """
        with self._lock:
            key = self._keys.get(session_key_id)
            if key is None or key.is_destroyed:
                return None
            priv_bytes = key.privkey_bytes

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
            return priv.sign(payload)
        except ImportError:
            # Placeholder signature: hash of (priv || payload)
            import hashlib
            return hashlib.sha256(priv_bytes + payload).digest()

    def destroy(self, session_key_id: str) -> bool:
        """Zero-out the private key and mark destroyed.

        Returns True if a live key was destroyed; False if it was
        already destroyed or didn't exist.
        """
        now = int(time.time() * 1000)
        with self._lock:
            key = self._keys.get(session_key_id)
            if key is None or key.is_destroyed:
                return False
            # Zero the bytes (best-effort in Python; production uses
            # hardware-bound key destroy via platform API)
            key.privkey_bytes = b"\x00" * len(key.privkey_bytes)
            key.destroyed_at_ms = now
            return True

    def reap_old(self, *, max_age_ms: int = 4 * 60 * 60 * 1000) -> int:
        """Destroy any session keys older than max_age_ms. Returns count."""
        now = int(time.time() * 1000)
        count = 0
        with self._lock:
            old_ids = [
                sid for sid, k in self._keys.items()
                if not k.is_destroyed and (now - k.created_at_ms) > max_age_ms
            ]
        for sid in old_ids:
            if self.destroy(sid):
                count += 1
        return count

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for k in self._keys.values() if not k.is_destroyed)


# =============================================================================
# Module-level singleton
# =============================================================================


_MANAGER = SessionKeyManager()


def get_session_key_manager() -> SessionKeyManager:
    return _MANAGER


__all__ = [
    "SessionKey",
    "SessionKeyManager",
    "get_session_key_manager",
]
