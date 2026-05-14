"""
Facet extraction at capture time.

Receives raw inputs from the app (transcript, audio blob ref, capture-side
metadata) and produces a populated FacetBundle. Some facets are computed
locally (semantic embedding via the embedding service); others are passed
through (acoustic features extracted at the app side; this module assembles
them into the bundle).

Acoustic feature extraction itself happens at the app boundary because raw
audio analysis is expensive and battery-bound on mobile; the app sends
already-extracted prosodic features. This module's job is to bundle them.

Spec ref: Part 5
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..embed.model import EmbeddingService, get_default_service
from ..schema.facets import (
    AcousticFacet, FacetBundle, SemanticEmbeddingFacet, SocialFacet, StateContext,
    StructuralFacet, VerbatimTextFacet,
)
from ..schema.kinds import InteractionalKind


def extract_facets(
    *,
    transcript: str,
    audio_blob_ref: str | None = None,
    duration_ms: int = 0,
    transcription_model_id: str | None = None,
    transcription_confidence: float | None = None,
    # Acoustic features pre-extracted at the app side
    pitch_contour_summary: list[float] | None = None,
    syllable_rhythm: list[float] | None = None,
    formant_trajectory_summary: list[float] | None = None,
    # Structural metadata
    period_ids: list[str] | None = None,
    event_id: str | None = None,
    interactional: InteractionalKind = InteractionalKind.MEMO_TO_SELF,
    state_context_fields: dict[str, Any] | None = None,
    goal_markers: list[str] | None = None,
    self_other_world: str = "self",
    # Social facet
    direct_participants: list[str] | None = None,
    subjects_of_discussion: list[str] | None = None,
    implicit_references: list[str] | None = None,
    audience: list[str] | None = None,
    # Embedding service override (for tests; default uses singleton)
    embedding_service: EmbeddingService | None = None,
) -> FacetBundle:
    """Build a FacetBundle from capture-time inputs.

    Returns a populated bundle. Does not log or persist anything; the caller
    (drawer.capture.capture_drawer) wires the bundle into a `drawer_captured`
    event.
    """
    service = embedding_service or get_default_service()
    info = service.info()

    verbatim = VerbatimTextFacet(
        text=transcript,
        transcription_model_id=transcription_model_id,
        transcription_model_version=None,
        transcription_confidence=transcription_confidence,
    )

    acoustic = AcousticFacet(
        pitch_contour_summary=pitch_contour_summary or [],
        syllable_rhythm=syllable_rhythm or [],
        formant_trajectory_summary=formant_trajectory_summary or [],
        duration_ms=duration_ms,
        audio_blob_ref=audio_blob_ref,
        acoustic_model_id=None,
    )

    # Embedding is computed at capture time from the transcript. The vector
    # itself goes to ChromaDB (handled by capture.py); the facet metadata is
    # what's stored in the bundle/log.
    semantic = SemanticEmbeddingFacet(
        embedding_model_id=info.model_id,
        embedding_model_version=info.version,
        weights_hash=info.weights_hash,
        dimension=info.dimension,
    )

    structural = StructuralFacet(
        period_ids=list(period_ids or []),
        event_id=event_id,
        interactional=interactional,
        state_context=StateContext(**(state_context_fields or {})),
        goal_markers=list(goal_markers or []),
        self_other_world=self_other_world,
    )

    social = SocialFacet(
        direct_participants=list(direct_participants or []),
        subjects_of_discussion=list(subjects_of_discussion or []),
        implicit_references=list(implicit_references or []),
        audience=list(audience or []),
    )

    return FacetBundle(
        verbatim_text=verbatim,
        acoustic=acoustic,
        semantic_embedding=semantic,
        structural=structural,
        social=social,
    )


def bundle_to_payload_props(bundle: FacetBundle) -> dict[str, Any]:
    """Flatten a FacetBundle to the property fields used in drawer_captured.

    The DrawerCaptured event has explicit fields for the structural and
    social facets (small, inline) and refs for verbatim/acoustic/semantic
    (which live in their backend stores). This helper extracts the parts
    that go into the event payload.
    """
    return {
        "verbatim_text": bundle.verbatim_text.text,
        "transcription_model_id": bundle.verbatim_text.transcription_model_id,
        "transcription_confidence": bundle.verbatim_text.transcription_confidence,
        "duration_ms": bundle.acoustic.duration_ms,
        "audio_blob_ref": bundle.acoustic.audio_blob_ref,
        "embedding_model_id": bundle.semantic_embedding.embedding_model_id,
        "embedding_model_version": bundle.semantic_embedding.embedding_model_version,
        # Structural fields (carried inline)
        "period_ids": bundle.structural.period_ids,
        "event_id": bundle.structural.event_id,
        "interactional": bundle.structural.interactional.value,
        "state_context": asdict(bundle.structural.state_context),
        "goal_markers": bundle.structural.goal_markers,
        "self_other_world": bundle.structural.self_other_world,
        # Social facet (carried inline)
        "direct_participants": bundle.social.direct_participants,
        "subjects_of_discussion": bundle.social.subjects_of_discussion,
        "implicit_references": bundle.social.implicit_references,
        "audience": bundle.social.audience,
    }
