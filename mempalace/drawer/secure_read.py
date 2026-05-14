"""
Read-path helpers for encrypted drawer content.

Implements Track 5E (read path lazy decryption) from
IMPLEMENTATION_ROADMAP.md per ENCRYPTION_AT_EDGE_DESIGN.md (v2).

# What this is

When a `DrawerCaptured` event has been encrypted (Track 5C/D), the
verbatim transcript lives in `event.verbatim_ciphertext` plus a
DEK handle and attestation sig. To read it back, callers go through
`decrypt_verbatim(event, key_manager)`.

# Why a function and not a property on a Drawer class

The codebase represents drawers via the `DrawerCaptured` event +
node properties + facet bundles, not via a single `Drawer` object.
A function lets callers decrypt-on-demand without having to
construct an intermediate object.

# What if the bundle isn't loaded

`decrypt_verbatim` raises `KeysNotLoaded` from the key manager.
Callers that should keep working when keys are unavailable (e.g.,
operations that don't actually need the verbatim) can catch and
substitute None / a placeholder. Callers that genuinely need the
plaintext propagate the exception.

# What if the event is unencrypted (legacy)

Returns None. Callers that have a separate plaintext source
(e.g., `transcript` parameter passed in at capture time) handle
the None case appropriately. Most legacy callers won't go through
this function at all because they have direct plaintext access.

Spec ref: ENCRYPTION_AT_EDGE_DESIGN.md v2 §"Where encryption sits in
the data flow (revised)" / "Local read path"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..schema.events import DrawerCaptured

if TYPE_CHECKING:
    from ..secure import CloudBoxKeyManager


def is_encrypted(event: DrawerCaptured) -> bool:
    """True if this event was captured under encryption-at-edge.

    Equivalent to `event.encryption_schema_version == "v2"` (or
    higher), but expressed semantically.
    """
    return event.encryption_schema_version not in ("", "v0")


def decrypt_verbatim(
    event: DrawerCaptured,
    key_manager: "CloudBoxKeyManager",
) -> bytes | None:
    """Decrypt the verbatim transcript from an encrypted drawer event.

    Returns:
      bytes:  the plaintext transcript bytes (UTF-8 encoded).
      None:   the event was captured without encryption (legacy path).

    Raises:
      KeysNotLoaded: the cloud-box key manager isn't loaded with a
                     bundle (idle-zeroed, expired, or never loaded).
      AttestationError: the ciphertext or sig has been tampered with.
      KeyHandleError: the dek_handle isn't bound to this manager's
                      loaded palace (cross-palace data, possibly
                      from a federation cache).

    Callers that know the manager is loaded and want a string back
    can do `decrypt_verbatim(event, mgr).decode("utf-8")`.
    """
    if not is_encrypted(event):
        return None

    if not event.verbatim_ciphertext:
        # Encryption schema declared but no ciphertext present.
        # Treat as unencrypted (defensive: legacy events that got
        # the schema-version field added but never actually
        # encrypted).
        return None

    return key_manager.decrypt(
        event.verbatim_ciphertext,
        dek_handle=event.verbatim_dek_handle,
        attestation_sig=event.verbatim_attestation_sig,
    )


def decrypt_verbatim_str(
    event: DrawerCaptured,
    key_manager: "CloudBoxKeyManager",
) -> str | None:
    """UTF-8 decoded form of `decrypt_verbatim`.

    Same return semantics: bytes plaintext → decoded str, None → None.
    """
    plaintext = decrypt_verbatim(event, key_manager)
    if plaintext is None:
        return None
    return plaintext.decode("utf-8")


__all__ = [
    "decrypt_verbatim",
    "decrypt_verbatim_str",
    "is_encrypted",
]
