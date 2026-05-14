"""
Drawer facet bundle.

A drawer is a bundle of facets — five distinct surfaces that consumers slice
along. Each facet has independent extraction cost, independent storage,
independent consumers.

Per R3 §5: paralinguistic moved to interpretation layer (it's a derivation
over acoustic + semantic, not a substrate); interactional folded into
structural as a typed property; social added.

Final facet list:
    1. verbatim_text       — transcript
    2. acoustic            — prosodic features for phonetic-rhyme matching
    3. semantic_embedding  — trained-model vector
    4. structural          — KG metadata + interactional + state context
    5. social              — relational orbit

Spec ref: Part 5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .kinds import InteractionalKind


# =============================================================================
# Facet enumeration
# =============================================================================

class FacetKind(str, Enum):
    """The five facet kinds. Used for facet selection in handle protocol."""
    VERBATIM_TEXT = "verbatim_text"
    ACOUSTIC = "acoustic"
    SEMANTIC_EMBEDDING = "semantic_embedding"
    STRUCTURAL = "structural"
    SOCIAL = "social"


# =============================================================================
# Per-facet payload schemas
#
# These describe what each facet contains. The facet payloads themselves are
# stored where appropriate (audio in encrypted blob storage, embeddings in
# ChromaDB, structural fields in the master views, etc.); these dataclasses
# describe the shape passed at capture time.
# =============================================================================

@dataclass
class VerbatimTextFacet:
    """The transcript. The textual substrate.

    The transcription model and its version are recorded so re-transcriptions
    can be tracked separately as `drawer_amended` events.
    """
    text: str
    transcription_model_id: str | None = None
    transcription_model_version: str | None = None
    transcription_confidence: float | None = None  # 0..1


@dataclass
class AcousticFacet:
    """Prosodic features.

    Captured at write time; expensive to re-extract from raw audio later, so
    capture-time discipline matters. The audio reference itself is held
    elsewhere (encrypted blob); this facet stores derived features.

    The dimensions here are starting points; the actual feature vector size
    is determined by the acoustic-feature extractor's model and is stored in
    a separate vector store keyed by drawer_id.
    """
    pitch_contour_summary: list[float] = field(default_factory=list)
    syllable_rhythm: list[float] = field(default_factory=list)
    formant_trajectory_summary: list[float] = field(default_factory=list)
    duration_ms: int = 0
    audio_blob_ref: str | None = None
    acoustic_model_id: str | None = None


@dataclass
class SemanticEmbeddingFacet:
    """The trained-model embedding vector.

    The vector itself is stored in ChromaDB or equivalent vector store; this
    dataclass carries the metadata needed for coordination (model identity
    and version) so that comparisons across embeddings can verify they were
    produced by the same model.

    Per R3 §7.3 / Part 7.3: the model is locally trained, not ChromaDB's
    default.
    """
    embedding_model_id: str
    embedding_model_version: str
    weights_hash: str | None = None       # for federation cross-palace queries
    dimension: int = 0


@dataclass
class StructuralFacet:
    """KG-attached structural metadata.

    Per R3 §5: interactional is a typed field within structural, not its own
    facet. The same applies to state_context (capture-time conditions).

    `period_id` is a list to support overlap (R1: periods can overlap on the
    same theme; precedence carried on the period node).
    """
    period_ids: list[str] = field(default_factory=list)
    event_id: str | None = None
    interactional: InteractionalKind = InteractionalKind.MEMO_TO_SELF
    state_context: "StateContext" = field(default_factory=lambda: StateContext())
    goal_markers: list[str] = field(default_factory=list)
    self_other_world: str = "self"        # 'self' | 'other' | 'world'

    def __post_init__(self) -> None:
        # Validate self_other_world
        if self.self_other_world not in ("self", "other", "world"):
            raise ValueError(
                f"self_other_world must be self/other/world, got {self.self_other_world!r}"
            )


@dataclass
class StateContext:
    """Capture-time conditions.

    Conway's encoding-specificity principle: retrieval cues must overlap with
    encoding context. Capturing this at write time is the only way; cannot be
    reconstructed later.

    Fields are open to extension; concrete fields here are starters.
    """
    sleep_state: str | None = None        # 'rested' | 'tired' | 'sleep_deprived'
    stress_level: float | None = None      # 0..1 if reported
    time_of_day_bucket: str | None = None  # 'morning' | 'afternoon' | ...
    location_cluster: str | None = None    # opaque cluster ID
    prior_activity: str | None = None      # opaque tag
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class SocialFacet:
    """Relational orbit.

    Distinct from the structural facet's event participants. The social facet
    captures who's in the relational orbit of *this drawer specifically*,
    which can differ from event participants:

        - direct_participants: who's actually in the conversation (audible)
        - subjects_of_discussion: who's being talked about, even if absent
        - implicit_references: who shaped the user's thinking ("I was thinking
          about what Maya would say")
        - audience: if intended for someone to hear (audio_letter, dictation)

    Identity values are entity IDs (resolved by the entity-resolution layer).
    """
    direct_participants: list[str] = field(default_factory=list)
    subjects_of_discussion: list[str] = field(default_factory=list)
    implicit_references: list[str] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)


# =============================================================================
# The bundle
# =============================================================================

@dataclass
class FacetBundle:
    """The full facet bundle for a drawer.

    Used at capture time to package a drawer's facets together for the
    `drawer_captured` event. Once captured, individual facets are stored in
    their respective backends (ChromaDB, audio blob storage, master views).
    """
    verbatim_text: VerbatimTextFacet
    acoustic: AcousticFacet
    semantic_embedding: SemanticEmbeddingFacet
    structural: StructuralFacet
    social: SocialFacet


# =============================================================================
# Facet selection
#
# Used by the handle protocol (Part 6.3): a consumer requests specific facets
# at specific fidelity levels. This dataclass describes the selection.
# =============================================================================

@dataclass
class FacetSelection:
    """Which facets to materialize, at what fidelity.

    Empty selection means "no facets at all" — useful for traversal-only
    queries that want graph topology but not content.
    """
    verbatim_text: bool = False
    acoustic: bool = False
    semantic_embedding: bool = False
    structural: bool = False
    social: bool = False

    def any_selected(self) -> bool:
        return any(
            (
                self.verbatim_text,
                self.acoustic,
                self.semantic_embedding,
                self.structural,
                self.social,
            )
        )

    @classmethod
    def all(cls) -> "FacetSelection":
        return cls(
            verbatim_text=True,
            acoustic=True,
            semantic_embedding=True,
            structural=True,
            social=True,
        )

    @classmethod
    def autobiographical(cls) -> "FacetSelection":
        """Default selection for autobiographical retrieval (Claude thread)."""
        return cls(verbatim_text=True, structural=True, social=True)

    @classmethod
    def montage(cls) -> "FacetSelection":
        """Default selection for montage composition."""
        return cls(acoustic=True, semantic_embedding=True, structural=True)

    @classmethod
    def matching(cls) -> "FacetSelection":
        """Default selection for federation-layer matching kernel."""
        return cls(semantic_embedding=True, structural=True, social=True)
