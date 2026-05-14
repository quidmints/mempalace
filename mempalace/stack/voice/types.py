"""
Voice-stack data types.

Per VOICE_STACK_DESIGN.md §"The substrate enrichment". These are the
in-memory shapes the steps produce; persistent storage uses
`TokenFeaturesWritten` and `SegmentCreated` events plus edges, but the
in-flight Step → Step handoff uses these typed dataclasses.

# Why dataclasses, not events

The substrate-level events (`TokenFeaturesWritten`, `SegmentCreated`)
are append-once historical records. The in-flight values are how
later steps consume earlier steps' outputs without re-reading the log.
Same data, different lifecycle.

# Why per-token features

Per VOICE_STACK_DESIGN.md and HANDLES_DESIGN.md v2 §"Voice — the
rewrite", paralinguistic features are per-token, not per-drawer. A
single drawer might cover many speakers, multiple emotional tones,
and several paralinguistic events. Per-token is the right granularity
to capture all of that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Per-token features
# =============================================================================


@dataclass
class ProsodyVector:
    """Per-token prosodic features.

    Continuous features. Each carries an implied confidence baked into
    the model that produced it; per-feature confidence lives on the
    enclosing TokenFeatures.
    """

    pitch_hz: float = 0.0
    """Pitch estimate (F0). 0.0 means no pitch detected (silence,
    unvoiced)."""

    energy: float = 0.0
    """Loudness in [0, 1] normalized."""

    speech_rate: float = 0.0
    """Tokens per second over a small window centered on this token.
    Useful for affect detection: rapid speech often signals
    excitement, slow speech often signals deliberation."""


@dataclass
class AffectDistribution:
    """Soft distribution over affect categories.

    Per VOICE_STACK_DESIGN.md §"Soft-distribution for accent (vs hard
    label)". Same principle here — affect is rarely categorical;
    downstream rankers benefit from the distribution.

    Standard categories cover the basics; arbitrary keys allowed for
    model-specific outputs.
    """

    categories: dict[str, float] = field(default_factory=dict)
    """Keyed by affect name (e.g. "neutral", "excited", "anxious",
    "sad", "amused", "sarcastic"). Values in [0, 1]; sums roughly to
    1 but not strictly enforced (overlapping categories like "amused
    + sarcastic" are valid)."""

    def top_k(self, k: int = 3) -> list[tuple[str, float]]:
        """Return the top-K categories by score, descending."""
        return sorted(
            self.categories.items(), key=lambda kv: kv[1], reverse=True
        )[:k]


@dataclass
class TokenFeatures:
    """Per-token aggregate of all voice-stack outputs for one token.

    Aligns with VOICE_STACK_DESIGN.md's TokenFeatures schema, plus
    runtime-only fields for the in-flight stack handoff.
    """

    token: str
    """The transcribed text. May be a sub-word piece depending on
    tokenizer; the ASR step's tokenizer choice determines this."""

    onset_ms: int
    """When this token starts in the audio timeline."""

    offset_ms: int
    """When this token ends. offset_ms - onset_ms is duration."""

    # Optional enrichments — populated by later steps in the stack
    prosody: ProsodyVector | None = None
    affect: AffectDistribution | None = None
    speaker_label: str | None = None
    """Diarization output. Scoped to this drawer (e.g. "s0", "s1");
    cross-drawer continuity is the speaker-matching step's job."""

    speaker_label_confidence: float | None = None

    # Provenance: which model pass produced each enrichment
    produced_by_model_pass: dict[str, str] = field(default_factory=dict)
    """Keyed by feature-name → model-pass-id. Lets the dependency
    tracker invalidate selectively when a step is upgraded.

    Example: {
        "tokens": "voice.asr@whisper-large-v3",
        "prosody": "voice.prosody_affect@emo-net-v2",
        "speaker_label": "voice.diarization@pyannote-3",
    }
    """


# =============================================================================
# Drawer segments
# =============================================================================


@dataclass
class AccentDistribution:
    """Soft distribution over accent / origin categories.

    Per VOICE_STACK_DESIGN.md: never a hard label. The categories are
    intentionally coarse — "north_american_general", "british_received",
    "south_asian_indian", etc. Fine-grained accent inference is brittle
    and downstream consumers do better with a distribution they can
    weight.
    """

    categories: dict[str, float] = field(default_factory=dict)


@dataclass
class DrawerSegment:
    """First-class segment of a drawer.

    A drawer typically has 1-N segments. Single-speaker memo: 1
    segment. Multi-speaker conversation: one segment per speaker turn,
    or coarser if the diarization step decides turns aren't worth
    splitting on.
    """

    segment_id: str
    drawer_id: str
    start_ms: int
    end_ms: int

    # Aggregate signals over the segment
    dominant_speaker_label: str | None = None
    """The speaker label most-present in this segment. May be None if
    diarization couldn't decide or the segment predates diarization."""

    dominant_affect: str | None = None
    dominant_affect_confidence: float | None = None

    accent_distribution: AccentDistribution | None = None

    # Token-features within this segment, indexed by position. The
    # whole drawer's TokenFeatures list spans all segments; this is
    # just the slice that fits into this segment.
    tokens: list[TokenFeatures] = field(default_factory=list)


__all__ = [
    "AccentDistribution",
    "AffectDistribution",
    "DrawerSegment",
    "ProsodyVector",
    "TokenFeatures",
]
