"""
Typed `InterpretiveFrame` (Track 2 output).

Replaces the v2 design's `InterpretiveFrame.fields: dict[str, Any]`
placeholder with a typed dataclass whose fields fall out of the
substrate-signal analysis pass (SUBSTRATE_SIGNAL_ANALYSIS.md).

# Five axes

A frame is a query-in-flight's specialization to a region of
substrate. Each frame carries typed slots for the five axes the
analysis pass identified:

  1. Signature region — R3 §8.2 dimensions targeted to a query
  2. Conway rate — Class 1 / 2 / 3 alignment for the frame's evidence
  3. Co-activation pattern — miner-identified co-activation regions
  4. Refinement cues — user signals during the query
  5. Voice flavor — Track 1A per-token + per-segment voice signals

Axes that don't apply are `None`. A frame that's purely voice-driven
populates only `voice_flavor`; a frame about signature region
specialization populates only `signature_region`.

# Why dataclasses, not Protocol

Frames flow through the trusted aggregator and the cache layer.
Concrete dataclasses serialize cleanly, hash consistently for cache
keys, and let `mypy` / runtime type checks catch shape errors.
Protocol would push too much onto the consumer side.

# Why `confidence` is on the frame, not per-axis

Per the analysis pass §5: the trusted aggregator combines frames
using each frame's confidence. Per-axis confidence would be cleaner
in theory but produces a combinatorial explosion in the aggregator
formula; the design accepts the simplification of frame-level
confidence and trusts the miner / signature module to produce
multiple frames if axes have different confidences.

Spec ref: SUBSTRATE_SIGNAL_ANALYSIS.md, HANDLES_DESIGN.md v2
§"Deferred shape — `InterpretiveFrame` fields".
"""

from __future__ import annotations

from dataclasses import dataclass, field


# =============================================================================
# Axis 1 — Signature region
# =============================================================================


@dataclass
class SignatureRegion:
    """A targeted region in signature space (R3 §8.2 specialization).

    A frame with this populated says: queries should weight candidates
    that sit in this region of signature space.

    Built from `SignatureSnapshot` fields. Source: signature module.
    """

    centered_on_theme_ids: list[str] = field(default_factory=list)
    """Which themes this frame is anchored to. Empty = no theme
    constraint."""

    target_position: dict[str, list[float]] = field(default_factory=dict)
    """theme_id → centroid vector. The frame prefers candidates whose
    own embedding sits near this position. Centroid dimensionality
    matches the embedder's output (typically 256–768)."""

    target_velocity_band: tuple[float, float] | None = None
    """(min, max) velocity scalar. None = no velocity constraint.
    A "I'm asking about a fast-moving theme" frame has a tight high
    band; a "thematic, settled" frame has a tight low band."""

    schema_fingerprints_required: list[str] = field(default_factory=list)
    """Active schemas the frame requires. Empty = no constraint."""

    contradiction_zone: str = "any"
    """One of "resolved" | "unresolved" | "any". Per §1.6 of the
    analysis pass — frame may target the resolved-contradiction zone
    for stable interpretation, or the unresolved zone to expose
    what's still being worked out."""

    fork_distribution_target: list[float] | None = None
    """Length-5 fork-significance bucket distribution to match
    against. None = no fork constraint."""


# =============================================================================
# Axis 2 — Conway rate
# =============================================================================


CONWAY_RATE_CLASS_1 = 1
CONWAY_RATE_CLASS_2 = 2
CONWAY_RATE_CLASS_3 = 3
"""The three Conway 2005 levels. Class 1 = episodic-buffer (fast,
recent); Class 2 = autobiographical-events (medium); Class 3 =
autobiographical-knowledge (slow, settled themes)."""


@dataclass
class ConwayRate:
    """Conway-rate alignment for a frame.

    Frames carry a consolidation rate matching where their evidence
    lives. A query that's specifically about a recent event has rate-1
    frames. A query about long-running themes has rate-3 frames.
    Multiple frames at different rates are normal.
    """

    target_rate: int = CONWAY_RATE_CLASS_2
    """1, 2, or 3. Defaults to Class 2 (medium) — the safe default
    when rate isn't strongly indicated."""

    rate_confidence: float = 0.5
    """How strongly this frame is committed to the target rate.
    Low = "weakly prefer this rate"; high = "this rate is the
    point." In [0, 1]."""

    rate_features_weight: dict[str, float] = field(default_factory=dict)
    """Per-feature weights to apply to per-feature rank inputs.
    A rate=1 frame boosts `drawer_recency_score` and `drawer_heat`;
    a rate=3 frame boosts `theme_canonicality` and schema-stability.

    Default weights for the three rates (used when this dict is
    empty) are produced by `default_rate_features_weight()`.
    """


def default_rate_features_weight(rate: int) -> dict[str, float]:
    """Sensible defaults for `ConwayRate.rate_features_weight`.

    These are starting points; the trusted aggregator may override
    via stance or refinement.
    """
    if rate == CONWAY_RATE_CLASS_1:
        return {
            "drawer_recency_score": 1.5,
            "drawer_heat": 1.3,
            "drawer_velocity_30d": 1.1,
            "theme_canonicality": 0.7,
        }
    if rate == CONWAY_RATE_CLASS_2:
        return {
            "drawer_recency_score": 1.0,
            "drawer_heat": 1.0,
            "drawer_velocity_30d": 1.0,
            "theme_canonicality": 1.0,
            "event_fork_significance": 1.2,
            "period_velocity_coupling": 1.1,
        }
    if rate == CONWAY_RATE_CLASS_3:
        return {
            "drawer_recency_score": 0.7,
            "drawer_heat": 0.8,
            "theme_canonicality": 1.5,
            "assertion_substrate_faithfulness": 1.3,
            "period_velocity_coupling": 1.2,
        }
    # Fallback for invalid rates: neutral
    return {}


# =============================================================================
# Axis 3 — Co-activation pattern
# =============================================================================


@dataclass
class CoActivationPattern:
    """Frame inheritance from miner-identified co-activation.

    Closest analog to the v1 design's
    `preferred_edge_kinds`/`preferred_node_kinds`, but grounded in
    observed co-activation rather than guess-shaped taxonomy.

    Built from `recurrence_cluster_member` events, `derived_from_*`
    edges, schema `derived_from_drawers` lists.
    """

    seed_recurrence_cluster_ids: list[str] = field(default_factory=list)
    """Ground the frame in recurrence clusters the miner has already
    produced. The frame's preferred-region inherits from these
    clusters' co-occurring nodes."""

    seed_drawer_ids: list[str] = field(default_factory=list)
    """Specific drawer exemplars. The frame prefers candidates that
    have been observed co-active with these drawers. Empty = no
    drawer-level seeding."""

    co_active_node_kinds: dict[str, float] = field(default_factory=dict)
    """node_kind → preference weight. Derived from co-activation
    statistics in the seed clusters/drawers. Empty = no node-kind
    preference."""

    co_active_edge_kinds: dict[str, float] = field(default_factory=dict)
    """edge_kind → preference weight. Same source as
    `co_active_node_kinds`."""


# =============================================================================
# Axis 4 — Refinement cues
# =============================================================================


@dataclass
class RefinementCues:
    """Signals from user refinements during the query.

    Aggregates `RefinementSignal`s (existing in retrieve/handle.py)
    plus voice-derived signals from Track 1A. The fields here flow
    into ranker scoring at each hop.
    """

    more_like_node_ids: list[str] = field(default_factory=list)
    """User said "more like these" (positive examples). Increases
    ranker score for similar candidates."""

    less_like_node_ids: list[str] = field(default_factory=list)
    """User said "less like these" (negative examples)."""

    stance_pulls: dict[str, float] = field(default_factory=dict)
    """axis_name → adjustment in [-1, 1]. Stance-adjustment
    deltas accumulated from refinements."""

    voice_match_pulls: list[tuple[str, float]] = field(default_factory=list)
    """(entity_id, confidence) pairs from voice-stack speaker
    matches. The user's voice cues identifying who they meant.
    Source: SpeakerMatchStep `voice_match_candidates`."""

    voice_affect_pulls: dict[str, float] = field(default_factory=dict)
    """affect category → preference weight. From the user's
    refinement-time prosody/affect — e.g., the user said "wait,
    no" with frustrated affect, indicating the prior result
    direction was wrong."""

    paralinguistic_event_filters: list[str] = field(default_factory=list)
    """Paralinguistic event kinds to gate candidates on. E.g.
    `["laughter"]` means "show me drawers where laughter occurred."
    Source: ParalinguisticStep events from Track 1A."""


# =============================================================================
# Axis 5 — Voice flavor
# =============================================================================


@dataclass
class VoiceFlavor:
    """Voice signals integrated as a first-class axis.

    Per VOICE_STACK_DESIGN.md, voice features at per-token granularity
    matter for routing. A voice-flavored frame says: this query's
    interpretation is shaped by voice cues at capture.

    Built from `TokenFeatures`, `DrawerSegment` aggregates,
    `voice_matches_reference` and `paralinguistic_event_at` edges
    from Track 1A.
    """

    target_speaker_entities: list[str] = field(default_factory=list)
    """Voice-match targets. Frame prefers segments matching one of
    these speakers."""

    target_affect_distribution: dict[str, float] = field(default_factory=dict)
    """affect category → weight. Frame prefers segments whose
    dominant_affect matches. E.g.,
    `{"sarcastic": 0.7, "amused": 0.3}` for a frame about a
    teasing-style conversation."""

    accent_region_pulls: dict[str, float] = field(default_factory=dict)
    """accent category → weight. Soft, not a hard label."""

    target_paralinguistic_event_kinds: list[str] = field(default_factory=list)
    """Paralinguistic event kinds the frame is about (laughter,
    code_switch, etc.)."""

    prosody_target: dict[str, tuple[float, float]] | None = None
    """Per-feature target band for prosody. feature_name → (min, max).
    E.g. `{"pitch_hz": (180, 260)}` for "raised pitch."
    None = no prosody constraint."""

    confidence: float = 0.5
    """How strongly this frame leans on voice. Low = degrade
    gracefully when voice is absent (e.g., text-only queries);
    high = voice IS the routing signal."""


# =============================================================================
# InterpretiveFrame — the typed shape
# =============================================================================


@dataclass
class InterpretiveFrame:
    """A query-in-flight's specialization to a region of substrate.

    Per HANDLES_DESIGN.md v2 + SUBSTRATE_SIGNAL_ANALYSIS.md.

    Multiple frames per handle, accumulating as refinements come in.
    Each frame holds typed slots per axis from the analysis. Slots
    that don't apply are None.

    The trusted aggregator combines frames using each frame's
    `confidence`; per-axis weights inside each frame steer ranking.
    """

    # Identity + meta
    frame_id: str
    """Stable identifier. Allocated when the frame is first
    constructed."""

    confidence: float
    """How strongly the handle is committed to this frame's reading
    of the query. In [0, 1]. Used by the trusted aggregator to
    weight frames against each other."""

    description: str = ""
    """Human-readable description. For logging / introspection /
    debugging UI. Not consumed by ranking."""

    derived_from_refinements: list[int] = field(default_factory=list)
    """Indices into the originating `HandleState.refinements`. Lets
    the trusted aggregator and the cache projection mechanism
    correlate frames with the refinement history that produced them."""

    # The five axes (each None when not applicable)
    signature_region: SignatureRegion | None = None
    conway_rate: ConwayRate | None = None
    co_activation_pattern: CoActivationPattern | None = None
    refinement_cues: RefinementCues | None = None
    voice_flavor: VoiceFlavor | None = None

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def populated_axes(self) -> list[str]:
        """Names of the axes that are non-None on this frame.

        Useful for log lines + cache-projection equivalence tests
        (two frames are likely equivalent if they have the same
        populated axes with the same shape)."""
        out: list[str] = []
        if self.signature_region is not None:
            out.append("signature_region")
        if self.conway_rate is not None:
            out.append("conway_rate")
        if self.co_activation_pattern is not None:
            out.append("co_activation_pattern")
        if self.refinement_cues is not None:
            out.append("refinement_cues")
        if self.voice_flavor is not None:
            out.append("voice_flavor")
        return out

    def is_empty(self) -> bool:
        """True if no axes are populated. An empty frame is a
        placeholder — the handle has allocated a frame slot but
        the substrate analysis hasn't filled it yet."""
        return not self.populated_axes()


__all__ = [
    "CONWAY_RATE_CLASS_1",
    "CONWAY_RATE_CLASS_2",
    "CONWAY_RATE_CLASS_3",
    "CoActivationPattern",
    "ConwayRate",
    "InterpretiveFrame",
    "RefinementCues",
    "SignatureRegion",
    "VoiceFlavor",
    "default_rate_features_weight",
]
