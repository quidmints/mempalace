"""
Drawer capture orchestrator.

The capture pipeline runs at the boundary between the app (which streams audio
blobs and pre-extracted features) and the palace's master log. Responsibilities:

  1. Compute content hash from the verbatim transcript.
  2. Check for collision against existing drawers; emit collision events
     (without blocking — duplicates are kept; collision is signal).
  3. Extract the 5-facet bundle (delegates to drawer.facets).
  4. Embed the verbatim text via the EmbeddingService (which emits its own
     attestation events).
  5. Persist the embedding vector to the vector store (drawer_id keyed).
  6. (Track 5D) Optionally encrypt verbatim through a phone secure element.
  7. Emit a `drawer_captured` event with the assembled metadata.
  8. Optionally hook the drawer into an event/period via the graph layer.

The capture function is the *only* path by which substrate enters the palace.
All higher-level interpretations (assertions, schemas, contradictions) derive
from drawer events; nothing bypasses this entry point.

# Track 5D — Encryption-at-edge

When `secure_element` is provided, the capture function:

  - Encrypts the verbatim transcript via `secure_element.encrypt_drawer`.
  - Stores the resulting (ciphertext, dek_handle, attestation_sig) on
    the `DrawerCaptured` event.
  - Sets `encryption_schema_version="v2"`.
  - Carries the `session_bundle_generation` from the cloud-box manager
    if one is provided (for stale-bundle detection).

When `secure_element` is None (legacy path), capture works exactly as
before — plaintext flows through, no ciphertext fields populated,
`encryption_schema_version="v0"`. This keeps existing tests green
during the migration.

Spec ref: Part 4 (drawer capture), Part 5 (facets), R3 §1.4 (attestation),
ENCRYPTION_AT_EDGE_DESIGN.md v2 (Track 5)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..embed.client import EmbeddingStore, get_default_store
from ..embed.model import EmbeddingService, get_default_service
from ..log.client import LogClient, get_default_client
from ..schema.events import DrawerCaptured
from ..schema.facets import FacetBundle
from ..schema.identifiers import make_drawer_id, make_event_id_log
from ..schema.kinds import InteractionalKind
from .collision import check_and_record_collision, compute_content_hash
from .facets import bundle_to_payload_props, extract_facets

if TYPE_CHECKING:
    from ..secure import (
        CloudBoxKeyManager,
        PhoneSecureElement,
    )


# =============================================================================
# Capture result
# =============================================================================


@dataclass
class CaptureResult:
    """What `capture_drawer` returns to the caller.

    drawer_id    : the new drawer's ID (always set, even on collision)
    content_hash : the canonical hash of the transcript
    facets       : the assembled FacetBundle
    collision_with: list of drawer_ids that shared the same content_hash
                    (empty if first occurrence)
    event_id     : the log event ID for the `drawer_captured` event
    """

    drawer_id: str
    content_hash: str
    facets: FacetBundle
    collision_with: list[str]
    event_id: str


# =============================================================================
# capture_drawer — the canonical entry point
# =============================================================================


def capture_drawer(
    *,
    transcript: str,
    actor: str = "app",
    audio_blob_ref: str | None = None,
    duration_ms: int = 0,
    transcription_model_id: str | None = None,
    transcription_confidence: float | None = None,
    pitch_contour_summary: list[float] | None = None,
    syllable_rhythm: list[float] | None = None,
    formant_trajectory_summary: list[float] | None = None,
    event_id: str | None = None,
    period_ids: list[str] | None = None,
    interactional: InteractionalKind = InteractionalKind.MEMO_TO_SELF,
    state_context_fields: dict[str, Any] | None = None,
    goal_markers: list[str] | None = None,
    self_other_world: str = "self",
    direct_participants: list[str] | None = None,
    subjects_of_discussion: list[str] | None = None,
    implicit_references: list[str] | None = None,
    audience: list[str] | None = None,
    capture_recorded_at: int | None = None,
    drawer_id: str | None = None,
    log_client: LogClient | None = None,
    embedding_service: EmbeddingService | None = None,
    embedding_store: EmbeddingStore | None = None,
    # Track 5D — encryption-at-edge integration
    secure_element: "PhoneSecureElement | None" = None,
    cloud_box_key_manager: "CloudBoxKeyManager | None" = None,
) -> CaptureResult:
    """Capture a drawer from raw inputs.

    All facet inputs are pre-extracted at the app boundary; this function
    is pure orchestration on the palace side. Returns a CaptureResult; the
    caller uses `.drawer_id` to attach the drawer to events / periods if
    needed (via the graph layer).

    The function is non-blocking on collision — if the content_hash has
    been seen, collision events are emitted but the new drawer is still
    captured. Recurrence-cluster mining downstream interprets the pattern.

    # Encryption (Track 5D)

    When `secure_element` is provided, the verbatim transcript is
    encrypted via `secure_element.encrypt_drawer(transcript_bytes,
    drawer_id=drawer_id)` before the DrawerCaptured event is built.
    The resulting (ciphertext, dek_handle, attestation_sig) goes onto
    the event; `encryption_schema_version` becomes "v2".

    `cloud_box_key_manager`, if provided, contributes its current
    `bundle_generation()` to the event for stale-bundle detection.
    Otherwise the event records generation 0.

    The plaintext transcript is still used locally for embedding —
    that's a cloud-box operation under the session-key bundle. The
    plaintext doesn't survive past this function.
    """
    log = log_client or get_default_client()
    embed_svc = embedding_service or get_default_service()
    store = embedding_store or get_default_store()

    drawer_id = drawer_id or make_drawer_id()
    capture_recorded_at = capture_recorded_at or int(time.time() * 1000)
    content_hash = compute_content_hash(transcript)

    # 1. Collision check (emits collision events but doesn't block)
    prior_drawer_ids = check_and_record_collision(
        incoming_drawer_id=drawer_id,
        content_hash=content_hash,
        actor=actor,
        client=log,
    )

    # 2. Facet extraction
    bundle = extract_facets(
        transcript=transcript,
        audio_blob_ref=audio_blob_ref,
        duration_ms=duration_ms,
        transcription_model_id=transcription_model_id,
        transcription_confidence=transcription_confidence,
        pitch_contour_summary=pitch_contour_summary,
        syllable_rhythm=syllable_rhythm,
        formant_trajectory_summary=formant_trajectory_summary,
        period_ids=period_ids,
        event_id=event_id,
        interactional=interactional,
        state_context_fields=state_context_fields,
        goal_markers=goal_markers,
        self_other_world=self_other_world,
        direct_participants=direct_participants,
        subjects_of_discussion=subjects_of_discussion,
        implicit_references=implicit_references,
        audience=audience,
        embedding_service=embed_svc,
    )

    # 3. Compute embedding (separate from the facet metadata; the bundle
    #    only carries the *fact* that an embedding was made, not the vector)
    vector = embed_svc.embed(transcript, step_id=f"capture:{drawer_id}")

    # 4. Persist vector to the store
    store.upsert(
        drawer_id=drawer_id,
        vector=vector,
        metadata={
            "content_hash": content_hash,
            "interactional": interactional.value,
            "self_other_world": self_other_world,
            "duration_ms": duration_ms,
        },
    )

    # 5. Build event payload
    payload_props = bundle_to_payload_props(bundle)

    # 6. Encrypt (Track 5D) — only when SE is provided
    enc_fields: dict[str, Any] = {
        "encryption_schema_version": "v0",
        "verbatim_text": transcript,  # v0 plaintext path, R3 §9.3
        "verbatim_ciphertext": b"",
        "verbatim_dek_handle": "",
        "verbatim_attestation_sig": b"",
        "audio_blob_dek_handle": "",
        "audio_blob_attestation_sig": b"",
        "session_bundle_generation": 0,
    }
    if secure_element is not None:
        enc_result = secure_element.encrypt_drawer(
            transcript.encode("utf-8"),
            drawer_id=drawer_id,
        )
        enc_fields["encryption_schema_version"] = "v2"
        enc_fields["verbatim_text"] = ""  # v2+ ciphertext-only path
        enc_fields["verbatim_ciphertext"] = enc_result.ciphertext
        enc_fields["verbatim_dek_handle"] = enc_result.dek_handle
        enc_fields["verbatim_attestation_sig"] = enc_result.attestation_sig
        if cloud_box_key_manager is not None:
            try:
                enc_fields["session_bundle_generation"] = (
                    cloud_box_key_manager.bundle_generation()
                )
            except Exception:
                # Diagnostic only; missing generation isn't fatal
                pass

    # The event has explicit named fields; flatten payload_props into them.
    now_ms = int(time.time() * 1000)
    event = DrawerCaptured(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        drawer_id=drawer_id,
        content_hash=content_hash,
        capture_recorded_at=capture_recorded_at,
        source_uri=audio_blob_ref,
        duration_ms=duration_ms,
        interactional=interactional.value,
        state_context=payload_props["state_context"],
        goal_markers=payload_props["goal_markers"],
        self_other_world=self_other_world,
        acoustic_blob_ref=audio_blob_ref,
        semantic_embedding_ref=f"vec:{drawer_id}",
        embedding_model_id=payload_props["embedding_model_id"],
        embedding_model_version=payload_props["embedding_model_version"],
        direct_participants=payload_props["direct_participants"],
        subjects_of_discussion=payload_props["subjects_of_discussion"],
        implicit_references=payload_props["implicit_references"],
        audience=payload_props["audience"],
        # Encryption-at-edge fields
        encryption_schema_version=enc_fields["encryption_schema_version"],
        verbatim_text=enc_fields["verbatim_text"],
        verbatim_ciphertext=enc_fields["verbatim_ciphertext"],
        verbatim_dek_handle=enc_fields["verbatim_dek_handle"],
        verbatim_attestation_sig=enc_fields["verbatim_attestation_sig"],
        audio_blob_dek_handle=enc_fields["audio_blob_dek_handle"],
        audio_blob_attestation_sig=enc_fields["audio_blob_attestation_sig"],
        session_bundle_generation=enc_fields["session_bundle_generation"],
    )
    log.append(event)

    return CaptureResult(
        drawer_id=drawer_id,
        content_hash=content_hash,
        facets=bundle,
        collision_with=prior_drawer_ids,
        event_id=event.event_id,
    )


__all__ = ["CaptureResult", "capture_drawer"]
