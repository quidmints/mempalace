"""
Phone-only-decrypt endpoint — Track 6B.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Layer 2 — Drawer view (on-demand,
phone-side decryption)" + §"Phone-only-decrypt path — concrete protocol":

    Phone                              Cloud box
       │                                   │
       │ GET /palace/drawer/{drawer_id}    │
       ├──────────────────────────────────▶│
       │                                   │ (no decryption performed)
       │  {ciphertext, dek_handle,         │
       │   attestation_sig, content_hash,  │
       │   metadata}                       │
       │◀──────────────────────────────────┤
       │                                   │
    Phone SE.decrypt_drawer(ciphertext, dek_handle)
       │  (verifies attestation_sig matches│
       │   content_hash, DEK_handle is     │
       │   bound to this palace, etc.)     │
       │                                   │
    Plaintext rendered on phone display    │
       │                                   │

# Key property

The cloud box does NOT decrypt for this flow. It only fetches the
ciphertext + handle + attestation sig and ships them to the phone over
TLS. The phone's SE does the decryption.

This is what makes the "user can view their data without trusting the
cloud-box-during-operation" property real. It distinguishes from the
miner/ranker flow, where the cloud box necessarily decrypts.

# Why a separate module from topology

The topology browser exposes structural metadata; phone-only-decrypt
exposes ciphertext envelopes for a specific drawer. Different contracts,
different rate-limit profiles (ciphertext is bigger; phone-only-decrypt
gets called less often), different security stories.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Layer 2", §"Phone-only-decrypt
path"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import DrawerCaptured, NodeCreated

logger = logging.getLogger(__name__)


# =============================================================================
# Errors
# =============================================================================


class DrawerNotFoundError(Exception):
    """The drawer_id doesn't appear in the log."""


class DrawerNotEncryptedError(Exception):
    """The drawer was captured under the legacy v0 (plaintext) path,
    so there's no ciphertext envelope to ship.

    Phone clients that hit this should fall back to the topology
    view's plaintext property accessors (which read node properties
    directly). This error is informational — the architecture
    supports legacy data.
    """


class DrawerInvalidatedError(Exception):
    """The drawer is invalidated (Tier-1 deletion) and the configured
    policy refuses to serve invalidated content.

    User-facing UX may explicitly surface invalidated drawers (so the
    user can see what they hid) or may filter them out — the policy is
    set per call.
    """


# =============================================================================
# Envelope
# =============================================================================


@dataclass
class DrawerCiphertextEnvelope:
    """What the cloud-box ships to the phone for one drawer.

    The phone uses (ciphertext, dek_handle, attestation_sig) to
    decrypt locally via PhoneSecureElement.decrypt(). content_hash
    is for post-decrypt verification (recompute hash of plaintext;
    compare to stored hash to catch any tampering).
    """

    drawer_id: str

    ciphertext: bytes
    dek_handle: str
    attestation_sig: bytes

    content_hash: str
    """Hash of plaintext computed at capture time. After decryption,
    the phone recomputes the hash of the recovered plaintext and
    compares to this. Mismatch indicates tampering."""

    encryption_schema_version: str
    session_bundle_generation: int

    # Plaintext metadata — same as topology view
    captured_at_ms: int = 0
    duration_ms: int = 0
    interactional: str = ""
    self_other_world: str = ""

    # Audio-blob fields (parallel to verbatim)
    audio_blob_uri: str = ""
    audio_blob_dek_handle: str = ""
    audio_blob_attestation_sig: bytes = b""

    # Diagnostics
    log_offset: int = 0


@dataclass
class DrawerCiphertextSinkRequest:
    """Server-side request shape (skeleton; the RPC layer
    materializes this from the wire request)."""

    drawer_id: str
    allow_invalidated: bool = False


# =============================================================================
# Endpoint
# =============================================================================


class PhoneDecryptEndpoint:
    """Cloud-box-side endpoint for the phone-only-decrypt protocol.

    Construction:
      ep = PhoneDecryptEndpoint()
      ep = PhoneDecryptEndpoint(log_client=...)

    Endpoint methods are read-only against the log; nothing is
    mutated. The endpoint never decrypts — it only fetches and
    bundles the envelope.

    # Audit

    Every call appends a `drawer_ciphertext_fetched` audit event so
    the user can see what was viewed and when. NOT included in this
    skeleton — it's a Track 6E follow-on (audit events are their own
    schema slice).
    """

    def __init__(self, log_client: LogClient | None = None) -> None:
        self._log = log_client or get_default_client()

    def fetch_drawer_ciphertext(
        self,
        drawer_id: str,
        *,
        allow_invalidated: bool = False,
    ) -> DrawerCiphertextEnvelope:
        """Fetch the ciphertext envelope for a drawer.

        Raises:
          DrawerNotFoundError: drawer_id not in log.
          DrawerNotEncryptedError: drawer is legacy v0; no envelope
            available. Phone client falls back to direct plaintext
            read via topology API.
          DrawerInvalidatedError: drawer is invalidated and
            allow_invalidated=False.
        """
        evt = self._find_drawer_event(drawer_id)
        if evt is None:
            raise DrawerNotFoundError(
                f"drawer_id={drawer_id!r} not found in log"
            )

        # Check invalidation first — failing fast saves shipping
        # ciphertext over the wire.
        if not allow_invalidated:
            from . import current as current_views
            current_views.tick_views()
            if current_views.is_drawer_invalidated(drawer_id):
                raise DrawerInvalidatedError(
                    f"drawer_id={drawer_id!r} is invalidated; "
                    "pass allow_invalidated=True to fetch anyway"
                )

        version = evt.encryption_schema_version
        if version in ("", "v0"):
            raise DrawerNotEncryptedError(
                f"drawer_id={drawer_id!r} was captured at "
                f"encryption_schema_version={version!r}; no ciphertext "
                "envelope. Phone client should read plaintext via "
                "topology accessors."
            )

        return DrawerCiphertextEnvelope(
            drawer_id=evt.drawer_id,
            ciphertext=evt.verbatim_ciphertext,
            dek_handle=evt.verbatim_dek_handle,
            attestation_sig=evt.verbatim_attestation_sig,
            content_hash=evt.content_hash,
            encryption_schema_version=evt.encryption_schema_version,
            session_bundle_generation=evt.session_bundle_generation,
            captured_at_ms=evt.capture_recorded_at,
            duration_ms=evt.duration_ms,
            interactional=evt.interactional,
            self_other_world=evt.self_other_world,
            audio_blob_uri=evt.acoustic_blob_ref or "",
            audio_blob_dek_handle=evt.audio_blob_dek_handle,
            audio_blob_attestation_sig=evt.audio_blob_attestation_sig,
            log_offset=0,  # set by _find_drawer_event when convenient
        )

    def _find_drawer_event(self, drawer_id: str) -> DrawerCaptured | None:
        """Linear scan of the log for the most recent DrawerCaptured
        event with this drawer_id.

        Production replaces with a drawer-index DD view (same note as
        TopologyBrowser._scan_drawers); Track 6B's scope is the
        endpoint contract.
        """
        end = self._log.current_offset() + 1
        rows = list(self._log.read_range(0, end))
        # Filter to DrawerCaptured for this drawer_id; take the last
        # one (in case of amendment).
        matching: list[tuple[int, dict]] = [
            (offset, payload)
            for offset, kind, payload in rows
            if kind == DrawerCaptured.EVENT_KIND
            and payload.get("drawer_id") == drawer_id
        ]
        if not matching:
            return None

        offset, payload = matching[-1]
        # Reconstruct the dataclass from the stored payload dict
        import dataclasses as _dc
        valid_fields = {f.name for f in _dc.fields(DrawerCaptured)}
        cleaned = {k: v for k, v in payload.items() if k in valid_fields}
        evt = DrawerCaptured(**cleaned)
        return evt


__all__ = [
    "DrawerCiphertextEnvelope",
    "DrawerCiphertextSinkRequest",
    "DrawerInvalidatedError",
    "DrawerNotEncryptedError",
    "DrawerNotFoundError",
    "PhoneDecryptEndpoint",
]
