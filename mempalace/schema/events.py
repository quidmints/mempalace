"""
Event taxonomy.

Every change to the palace is an event appended to the log. This module
defines the Python representation of every event kind: what fields each
carries, what it means, when it's emitted.

The log itself is implemented in Rust (mempalace_core); this module is the
Python-facing schema. Rust's serialization mirrors these definitions.

Spec ref: Part 1 (event taxonomy)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Union

from .identifiers import make_event_id_log
from .kinds import (
    ContradictionResolution,
    DerivationType,
    EdgeKind,
    JobState,
    NodeKind,
)


# =============================================================================
# Base event
# =============================================================================

@dataclass
class Event:
    """Common fields for every event.

    `event_id` is generated automatically; consumers don't pass it. `kind`
    is set by each subclass via the EVENT_KIND class variable. `recorded_at`
    is the system time when the event was appended; subclasses can override
    in tests but normally it defaults to now.

    `actor` identifies who emitted the event (a job, a user action, a miner
    pass, a system process). It's free-form for now; conventions like
    "miner:class1" or "user:review" are recommended.

    `batch_id` ties this event to a batch (Phase 1 framing). Empty string
    means "atomic single-event implicit batch" — the legacy / single-shot
    write path. Multi-event ops set this to a non-empty id; recovery uses
    it to identify torn batches.
    """
    EVENT_KIND: ClassVar[str] = ""

    event_id: str = field(default_factory=make_event_id_log)
    recorded_at: int = 0     # ms since epoch; 0 means "fill in at append"
    actor: str = "system"
    batch_id: str = ""       # Phase 1: empty = implicit single-event batch

    @property
    def kind(self) -> str:
        return type(self).EVENT_KIND


# =============================================================================
# Substrate events
#
# Immutable in the strong sense. Record what was captured from the world.
# Cannot be invalidated; can only be superseded by a new substrate event
# pointing at a different blob (drawer_amended).
# =============================================================================

@dataclass
class DrawerCaptured(Event):
    """A new drawer was captured.

    Carries all five facets at capture time. The acoustic, semantic-embedding,
    paralinguistic, and audio-blob payloads are stored in their respective
    backends; this event records the identifiers and content_hash.

    # Encryption-at-edge fields (Track 5C, ENCRYPTION_AT_EDGE_DESIGN.md v2)

    The verbatim transcript (content) is encrypted by the phone's
    SE before this event is built. The cloud-box log only sees
    ciphertext + DEK handle + attestation sig. Same for the audio
    blob (which is stored at `acoustic_blob_ref` — the blob at the
    URI is encrypted too, but only the URI is in this event).

    All ciphertext fields are optional. When `encryption_schema_version`
    is "v0" (the default) or empty, no ciphertext fields are
    populated and `verbatim_text`/etc. live in plaintext in node
    properties (legacy path). When `encryption_schema_version` is
    "v2" or higher, the ciphertext fields ARE populated and node
    properties carry only the ciphertext envelope.
    """
    EVENT_KIND: ClassVar[str] = "drawer_captured"

    drawer_id: str = ""
    content_hash: str = ""
    capture_recorded_at: int = 0     # capture-side wall time (vs. log recorded_at)
    source_uri: str | None = None
    duration_ms: int = 0
    interactional: str = "memo_to_self"
    state_context: dict[str, Any] = field(default_factory=dict)
    goal_markers: list[str] = field(default_factory=list)
    self_other_world: str = "self"

    # Facet pointers (the actual payloads live in backend stores)
    acoustic_blob_ref: str | None = None
    semantic_embedding_ref: str | None = None
    embedding_model_id: str | None = None
    embedding_model_version: str | None = None

    # Social facet (carried inline because small)
    direct_participants: list[str] = field(default_factory=list)
    subjects_of_discussion: list[str] = field(default_factory=list)
    implicit_references: list[str] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)

    # Encryption-at-edge fields (Track 5C). All optional.
    encryption_schema_version: str = "v0"
    """The encryption schema version this event uses. "v0" = no
    encryption (legacy). "v2" = phone-encrypted verbatim per
    ENCRYPTION_AT_EDGE_DESIGN.md v2."""

    verbatim_text: str = ""
    """The plaintext verbatim transcript. Populated only when
    `encryption_schema_version` is "v0" or empty (legacy /
    no-encryption path). Empty for v2+ — for those the ciphertext
    sits in `verbatim_ciphertext` and the read-path is via
    `mempalace.drawer.secure_read` after phone-decrypt.

    R3 §9.3 substrate verification reads from this field for
    plaintext drawers."""

    verbatim_ciphertext: bytes = b""
    """The encrypted verbatim transcript. Populated only when
    encryption_schema_version >= "v2"."""

    verbatim_dek_handle: str = ""
    """Opaque handle naming the DEK that encrypted verbatim. The
    cloud-box's CloudBoxKeyManager uses it during local read."""

    verbatim_attestation_sig: bytes = b""
    """Phone SE's HMAC over (ciphertext, dek_handle, key_purpose,
    context). Catches on-disk tampering after capture."""

    audio_blob_dek_handle: str = ""
    """Handle for the audio blob at acoustic_blob_ref. The blob at
    the URI is ciphertext; this handle is needed to decrypt it.
    Populated only when encryption_schema_version >= "v2" AND an
    audio blob is present."""

    audio_blob_attestation_sig: bytes = b""

    session_bundle_generation: int = 0
    """Generation of the session-key bundle the cloud box was using
    when it first observed this event. Lets stale-bundle decryption
    be detected. 0 = not encryption-tracked or pre-bundle."""


@dataclass
class DrawerAmended(Event):
    """A drawer was re-transcribed or its audio was re-encoded.

    Same drawer_id; new content_hash. Original substrate event preserved in
    log; this event supersedes for current-state queries.
    """
    EVENT_KIND: ClassVar[str] = "drawer_amended"

    drawer_id: str = ""
    new_content_hash: str = ""
    previous_content_hash: str = ""
    reason: str = ""                  # "re-transcription" | "audio_re-encoded" | etc.


@dataclass
class DrawerHashCollision(Event):
    """A capture presented a content_hash that already exists.

    Storage does not silently dedup (R3); this event surfaces the collision
    for upstream resolution. The downstream resolution (keep, replace, merge
    with note) emits its own event.
    """
    EVENT_KIND: ClassVar[str] = "drawer_hash_collision"

    incoming_drawer_id: str = ""
    existing_drawer_id: str = ""
    content_hash: str = ""


# =============================================================================
# Interpretation events
#
# Versioned, pass-attributed, supersedable. Record what the system thinks
# about substrate. The substrate-vs-interpretation split: substrate events
# are immutable; interpretation events can be superseded.
# =============================================================================

@dataclass
class NodeCreated(Event):
    """A new non-drawer node was created.

    Drawer nodes are created via DrawerCaptured; this is for theme, period,
    event, entity, schema, assertion, recurrence_cluster.

    `properties` is kind-specific; validated against per-kind JSON schemas
    by validators.py.
    """
    EVENT_KIND: ClassVar[str] = "node_created"

    node_id: str = ""
    node_kind: str = ""              # NodeKind value
    properties: dict[str, Any] = field(default_factory=dict)
    canonical: bool = False
    canon_path: str | None = None
    importance: float = 0.5

    # Pass attribution (R3 §10.6 — provisional/confirmed/rejected lifecycle)
    created_by: str = "system"        # capture | agent | miner-pass-version | user
    miner_pass_version: str | None = None


@dataclass
class NodePropertySet(Event):
    """A node's property was updated.

    Property updates are versioned: each NodePropertySet supersedes the
    prior value for that (node_id, field). Includes a supersedes pointer
    if known.
    """
    EVENT_KIND: ClassVar[str] = "node_property_set"

    node_id: str = ""
    field_name: str = ""
    new_value: Any = None
    supersedes_event_id: str | None = None


@dataclass
class EdgeCreated(Event):
    """A new edge was created.

    Carries bitemporal validity and provenance. `derivation` indicates the
    epistemic act that produced the edge.
    """
    EVENT_KIND: ClassVar[str] = "edge_created"

    edge_id: str = ""
    edge_kind: str = ""              # EdgeKind value
    source_node_id: str = ""
    target_node_id: str = ""

    # Bitemporal validity
    valid_from: int | None = None    # ms since epoch; world time
    valid_to: int | None = None
    # `recorded_at` (system time) is on the base Event; matches edge.recorded_at

    # Provenance and weighting
    weight: float = 1.0
    confidence: float = 1.0
    derivation: str = DerivationType.OBSERVATION.value

    # Edge-kind-specific properties (e.g., role string on participates_in,
    # span pointer on derived_from per R3 §9.3)
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class EdgeInvalidated(Event):
    """An edge was invalidated.

    Mutation forbidden; this is the only way to "remove" an edge. The edge
    remains in the log; current_edges view filters by invalidated_at IS NULL.
    """
    EVENT_KIND: ClassVar[str] = "edge_invalidated"

    edge_id: str = ""
    reason: str | None = None


# =============================================================================
# User-facing invalidation (Track 6C — USER_VIEW_AND_DELETE_DESIGN.md)
#
# Tier-1 deletion: cheap and reversible. The substrate event is appended;
# views respect the invalidation; retrieval no longer surfaces the target;
# original ciphertext stays in the log. Reverse via *Revalidated.
#
# This is distinct from EdgeInvalidated (which existed for edge-level
# substrate maintenance and is automatically emitted by various components).
# These new events are user-emitted from the phone.
# =============================================================================


@dataclass
class DrawerInvalidated(Event):
    """A drawer was user-invalidated (Tier-1 deletion).

    All views respect this: retrieval doesn't surface the drawer;
    miner passes that already produced assertions from this drawer
    flag those assertions as derived-from-invalidated.

    Reversible via DrawerRevalidated. The original ciphertext stays
    in the log unchanged. To actually remove ciphertext, use
    RequestErase (Track 6D).
    """
    EVENT_KIND: ClassVar[str] = "drawer_invalidated"

    drawer_id: str = ""
    invalidated_by_user: bool = True
    reason: str | None = None
    invalidated_at_ms: int = 0


@dataclass
class DrawerRevalidated(Event):
    """A previously-invalidated drawer was revalidated (Tier-1 reverse).

    Idempotent — revalidating an already-valid drawer is a no-op.
    """
    EVENT_KIND: ClassVar[str] = "drawer_revalidated"

    drawer_id: str = ""
    revalidated_at_ms: int = 0


@dataclass
class NodeInvalidated(Event):
    """A node was user-invalidated (Tier-1 deletion).

    Cascading rules: edges incident to the node remain visible in
    topology view but flagged invalidated-via-node. Properties of
    the node aren't separately invalidated; if the user wants to
    keep the node but remove a property they invalidate the
    interpretation that asserted the property (separately).
    """
    EVENT_KIND: ClassVar[str] = "node_invalidated"

    node_id: str = ""
    invalidated_by_user: bool = True
    reason: str | None = None
    invalidated_at_ms: int = 0


@dataclass
class NodeRevalidated(Event):
    """A previously-invalidated node was revalidated."""
    EVENT_KIND: ClassVar[str] = "node_revalidated"

    node_id: str = ""
    revalidated_at_ms: int = 0


@dataclass
class EdgeRevalidated(Event):
    """A previously-invalidated edge was revalidated.

    The substrate model: EdgeInvalidated → EdgeRevalidated → EdgeInvalidated.
    Each transition appends an event; latest wins.
    """
    EVENT_KIND: ClassVar[str] = "edge_revalidated"

    edge_id: str = ""
    revalidated_at_ms: int = 0


@dataclass
class InterpretationAssigned(Event):
    """An interpretive field was assigned to a node.

    The backbone of substrate-vs-interpretation split. Memory_type, importance,
    confidence, etc. are all interpretations that can be revised by later
    miner passes. Each assignment supersedes the prior one; the history is
    preserved in the log.
    """
    EVENT_KIND: ClassVar[str] = "interpretation_assigned"

    node_id: str = ""
    field_name: str = ""
    new_value: Any = None
    supersedes_event_id: str | None = None
    miner_pass_version: str = ""
    confidence: float = 1.0


@dataclass
class SchemaInduced(Event):
    """A new schema version was induced by Class 3 miner.

    Schemas are versioned (R1): each Class 3 pass emits a new schema-version
    node; old versions linked via `supersedes` edges. Stability and coverage
    scores enable stable/drifting/broken classification across passes.
    """
    EVENT_KIND: ClassVar[str] = "schema_induced"

    schema_node_id: str = ""
    schema_kind: str = ""             # SchemaKind value
    name: str = ""
    description: str = ""
    miner_pass_version: str = ""

    # Derivation pointers
    derived_from_events: list[str] = field(default_factory=list)
    derived_from_assertions: list[str] = field(default_factory=list)
    derived_from_drawers: list[str] = field(default_factory=list)

    stability_score: float = 0.0
    coverage_score: float = 0.0
    supersedes_schema_id: str | None = None


@dataclass
class RecurrenceClusterMember(Event):
    """A drawer was added to a recurrence cluster.

    Aggregated treatment per R3 §3.1: linear in cluster size, not quadratic
    in pairwise relations. The cluster itself is a queryable node.
    """
    EVENT_KIND: ClassVar[str] = "recurrence_cluster_member"

    drawer_id: str = ""
    cluster_id: str = ""
    similarity_to_representative: float = 1.0


@dataclass
class ContradictionAsserted(Event):
    """A contradiction edge was created between two assertions."""
    EVENT_KIND: ClassVar[str] = "contradiction_asserted"

    contradicting_assertion_id: str = ""
    contradicted_assertion_id: str = ""
    edge_id: str = ""
    detected_by: str = ""             # which miner pass


@dataclass
class ContradictionResolved(Event):
    """An existing contradiction edge was annotated with resolution state.

    The four resolution strategies from Conway (Beike & Landoll): outweighed,
    justified, closed, superseded.
    """
    EVENT_KIND: ClassVar[str] = "contradiction_resolved"

    edge_id: str = ""
    resolution: str = ContradictionResolution.UNRESOLVED.value
    resolved_by: str = "user"


@dataclass
class AssertionOrphaned(Event):
    """An assertion's derived_from drawers are all invalidated/deleted.

    Conway two-systems consolidation pattern (R3 §9.1): drawer deletion does
    not cascade to derived assertions. The orphaned assertion isn't deleted
    but is flagged with reduced confidence.
    """
    EVENT_KIND: ClassVar[str] = "assertion_orphaned"

    assertion_id: str = ""
    invalidated_drawer_ids: list[str] = field(default_factory=list)


# =============================================================================
# Job-state events (Part 1.3)
#
# Job orchestration is in the same log; resumability and audit are trivial.
# =============================================================================

@dataclass
class JobScheduled(Event):
    EVENT_KIND: ClassVar[str] = "job_scheduled"

    job_id: str = ""
    job_kind: str = ""                # "miner-class1" | "matching-layer2" | etc.
    consumer: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobStarted(Event):
    EVENT_KIND: ClassVar[str] = "job_started"
    job_id: str = ""


@dataclass
class JobProgress(Event):
    EVENT_KIND: ClassVar[str] = "job_progress"

    job_id: str = ""
    progress_fraction: float = 0.0    # 0..1
    note: str = ""


@dataclass
class JobCompleted(Event):
    EVENT_KIND: ClassVar[str] = "job_completed"

    job_id: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobFailed(Event):
    EVENT_KIND: ClassVar[str] = "job_failed"

    job_id: str = ""
    error_kind: str = ""
    error_message: str = ""


@dataclass
class JobPaused(Event):
    EVENT_KIND: ClassVar[str] = "job_paused"
    job_id: str = ""
    reason: str = ""


@dataclass
class JobResumed(Event):
    EVENT_KIND: ClassVar[str] = "job_resumed"
    job_id: str = ""


@dataclass
class ViewOffsetAdvanced(Event):
    """A consumer's dataflow view advanced to a new log offset.

    Used for backpressure detection (consumer falling behind log head) and
    for resumption from checkpoint.
    """
    EVENT_KIND: ClassVar[str] = "view_offset_advanced"

    consumer_id: str = ""
    new_offset: int = 0


@dataclass
class FeedbackRecorded(Event):
    """Downstream feedback signal.

    Tagged with the consumer that emitted it and the interpretation versions
    that were active when the relevant artifact was produced. This is what
    makes credit assignment to the right interpretation generation possible
    (R3).
    """
    EVENT_KIND: ClassVar[str] = "feedback_recorded"

    consumer: str = ""                # "montage" | "matching" | "resolve" | etc.
    artifact_id: str = ""
    feedback_kind: str = ""           # "kept" | "discarded" | "match_outcome" | etc.
    feedback_value: Any = None
    interpretation_versions: dict[str, str] = field(default_factory=dict)
    notes: str = ""


# =============================================================================
# Federation events (Part 9)
# =============================================================================

@dataclass
class MatchRequestReceived(Event):
    EVENT_KIND: ClassVar[str] = "match_request_received"

    match_id: str = ""
    requester_pubkey: str = ""
    target_palace_id: str = ""
    requested_layer: int = 1          # 1 / 2 / 3 in the layered triangulation
    scope_spec: dict[str, Any] = field(default_factory=dict)
    stake_amount: int = 0


@dataclass
class SandboxProvisioned(Event):
    EVENT_KIND: ClassVar[str] = "sandbox_provisioned"

    sandbox_id: str = ""
    match_id: str = ""
    privacy_mode: str = "SANDBOX"


@dataclass
class ForeignPalaceLoaded(Event):
    EVENT_KIND: ClassVar[str] = "foreign_palace_loaded"

    sandbox_id: str = ""
    foreign_palace_id: str = ""
    slice_size_bytes: int = 0
    layer: int = 1


@dataclass
class MatchLayerCompleted(Event):
    EVENT_KIND: ClassVar[str] = "match_layer_completed"

    match_id: str = ""
    layer: int = 1
    outcome: str = ""                 # "promote" | "stop" | "indeterminate"
    score: float = 0.0


@dataclass
class FindingEmitted(Event):
    EVENT_KIND: ClassVar[str] = "finding_emitted"

    match_id: str = ""
    topology: str = ""                # "peer" | "mentor" | "complementary" | "divergent"
    strength_per_dimension: dict[str, float] = field(default_factory=dict)
    target: str = "mempalace_federation"  # was "switchboard"


@dataclass
class SandboxTornDown(Event):
    EVENT_KIND: ClassVar[str] = "sandbox_torn_down"

    sandbox_id: str = ""
    keys_destroyed: bool = True


# =============================================================================
# Handle lifecycle events (R3 §9.2)
#
# Handles are persistent stateful objects; their lifecycle is in the log so
# that the iteration history is auditable.
# =============================================================================

@dataclass
class HandleAllocated(Event):
    EVENT_KIND: ClassVar[str] = "handle_allocated"

    handle_id: str = ""
    query_text: str = ""
    scope_spec: dict[str, Any] = field(default_factory=dict)
    stance: dict[str, Any] = field(default_factory=dict)
    consumer: str = ""


@dataclass
class HandleRefined(Event):
    EVENT_KIND: ClassVar[str] = "handle_refined"

    handle_id: str = ""
    more_like_node_ids: list[str] = field(default_factory=list)
    less_like_node_ids: list[str] = field(default_factory=list)
    scope_adjustments: dict[str, Any] = field(default_factory=dict)


@dataclass
class HandleResolved(Event):
    EVENT_KIND: ClassVar[str] = "handle_resolved"

    handle_id: str = ""
    fidelity: dict[str, str] = field(default_factory=dict)
    result_count: int = 0
    elapsed_ms: int = 0


@dataclass
class HandleClosed(Event):
    EVENT_KIND: ClassVar[str] = "handle_closed"

    handle_id: str = ""
    reason: str = ""                  # "consumer_done" | "ttl_expired" | etc.


# =============================================================================
# Canonicalizer events (R3 §4)
# =============================================================================

@dataclass
class CanonicalPromoted(Event):
    EVENT_KIND: ClassVar[str] = "canonical_promoted"

    domain: str = ""                  # CanonicalDomain value
    canonical_form: str = ""
    cluster_member_surfaces: list[str] = field(default_factory=list)
    promoted_by: str = "miner"        # or "user"


@dataclass
class CanonicalRejected(Event):
    EVENT_KIND: ClassVar[str] = "canonical_rejected"

    domain: str = ""
    candidate_id: str = ""
    rejected_by: str = "user"
    reason: str = ""


@dataclass
class CanonicalizationReverted(Event):
    """A prior canonical mapping was reversed.

    Reversibility per R3 §4.5. A wrong collapse → emit this event with new
    mapping; canonical history is queryable; current canonicalization is
    the latest non-reverted mapping.
    """
    EVENT_KIND: ClassVar[str] = "canonicalization_reverted"

    domain: str = ""
    previous_canonical: str = ""
    surface_form: str = ""
    new_canonical: str | None = None  # None means "treat as novel again"


# =============================================================================
# Attestation events (R3 §1.4, §7.6)
# =============================================================================

@dataclass
class ModelLoaded(Event):
    EVENT_KIND: ClassVar[str] = "model_loaded"

    model_id: str = ""
    weights_hash: str = ""
    signing_pubkey: str = ""
    enrollment_signature: str = ""


@dataclass
class ModelInferenceCompleted(Event):
    """Per-inference attestation.

    Emitted by AttestedStep after each Step.run() completes. The attestation
    payload is SHA256(input || output || weights_hash || timestamp || step_id),
    signed by the model's StrongBox-bound signing key.
    """
    EVENT_KIND: ClassVar[str] = "model_inference_completed"

    model_id: str = ""
    weights_hash: str = ""
    step_id: str = ""
    input_hash: str = ""
    output_hash: str = ""
    attestation_signature: str = ""


@dataclass
class HeartbeatReceived(Event):
    EVENT_KIND: ClassVar[str] = "heartbeat_received"

    device_pubkey: str = ""
    slot: int = 0
    signature: str = ""


@dataclass
class PhoneOffModeChanged(Event):
    """The daemon's phone-off-mode state machine changed states.

    Per R3 §7.6, the daemon operates in one of three modes based on
    phone heartbeat freshness and decryption-key TTL:

      - ONLINE: heartbeat fresh AND keys not TTL-expired. Full operation.
      - READ_ONLY: heartbeat stale OR keys expired. Serve queries; no
        writes; no heavy operations.
      - LOCKED_OUT: 3 consecutive missed heartbeats (≥3 hours).
        Daemon stops; on-chain `trigger_app_integrity_lockout` callable
        by the contract.

    `from_mode` and `to_mode` are the literal mode strings. `reason`
    is a short code: "heartbeat_stale" / "keys_expired" /
    "missed_heartbeats_3" / "phone_reconnected" / etc. `now_ms` is
    when the transition was detected.
    """
    EVENT_KIND: ClassVar[str] = "phone_off_mode_changed"

    from_mode: str = ""
    to_mode: str = ""
    reason: str = ""
    now_ms: int = 0
    missed_heartbeat_count: int = 0
    last_heartbeat_at_ms: int = 0


# =============================================================================
# Voice stack events (Track 1A — VOICE_STACK_DESIGN.md)
#
# Each voice-stack step (ASR, diarization, speaker matching, prosody,
# accent, paralinguistic) emits VoiceStepCompleted. TokenFeaturesWritten
# stamps the per-token feature blob into the substrate. SegmentCreated
# declares a first-class drawer segment.
# =============================================================================


@dataclass
class VoiceStepCompleted(Event):
    """A voice-stack step finished processing a drawer or segment.

    Per VOICE_STACK_DESIGN.md §"Schema additions". Each step emits
    one of these with `step_id` from the well-known set
    {"voice.asr", "voice.diarization", "voice.speaker_match",
    "voice.prosody_affect", "voice.accent", "voice.paralinguistic"}.

    The `model_pass_version` is what the dependency tracker keys
    invalidation off. The `output_summary` is opaque per-step
    metadata (e.g. token count, dominant speaker label, top-k affect
    distribution); larger outputs land via TokenFeaturesWritten or
    edge events.
    """
    EVENT_KIND: ClassVar[str] = "voice_step_completed"

    drawer_id: str = ""
    segment_id: str | None = None
    step_id: str = ""
    model_pass_version: str = ""
    output_summary: dict[str, Any] = field(default_factory=dict)
    completed_at_ms: int = 0


@dataclass
class TokenFeaturesWritten(Event):
    """Per-drawer write of the token-features list.

    The actual list is large enough that we don't carry it inline —
    it lands in a backend store, referenced by `features_blob_ref`.
    `produced_by_model_passes` records which step+version produced
    each named feature so the dependency tracker can invalidate
    selectively when a step is upgraded.
    """
    EVENT_KIND: ClassVar[str] = "token_features_written"

    drawer_id: str = ""
    token_count: int = 0
    features_blob_ref: str = ""
    produced_by_model_passes: dict[str, str] = field(default_factory=dict)
    written_at_ms: int = 0


@dataclass
class SegmentCreated(Event):
    """A drawer segment is a first-class substrate entity.

    Boundaries (start_ms, end_ms) align with the audio timeline. A
    drawer with N segments has N edges of kind `drawer_has_segment`
    pointing into the segments. Per-segment aggregates
    (dominant_speaker, dominant_affect, accent_distribution) are
    stored as node properties on the segment node, not on this
    event — this event just declares existence + bounds.
    """
    EVENT_KIND: ClassVar[str] = "segment_created"

    segment_id: str = ""
    drawer_id: str = ""
    start_ms: int = 0
    end_ms: int = 0
    created_at_ms: int = 0


@dataclass
class AttestationChainBroken(Event):
    EVENT_KIND: ClassVar[str] = "attestation_chain_broken"

    chain_kind: str = ""              # "device" | "model" | "heartbeat" | "audit_log"
    failure_reason: str = ""


# =============================================================================
# Erase + burn — Track 6D-E
#
# Per USER_VIEW_AND_DELETE_DESIGN.md §"Two-tier deletion":
#   Tier 1 — Invalidate (cheap, reversible) — already shipped.
#   Tier 2 — Erase (expensive, one-way). RequestErase kicks an async
#            compaction job that walks the log, rewrites events to
#            tombstone form, updates snapshots; emits EraseCompleted.
#   Tier 3 — Burn the palace. Phone destroys Phone Master Key. Cloud
#            box session bundles can no longer be refreshed; on TTL
#            expiry they idle-zero. On-chain IntegrityLockout fires
#            per R3 §7.6 (event mirrored as IntegrityLockout below).
# =============================================================================


@dataclass
class RequestErase(Event):
    """User requested irreversible erasure of a substrate object.

    Kicks an async compaction job that:
      1. Walks the cold log, identifying events referencing the target.
      2. Rewrites those events to tombstone form (keeps offset / kind /
         batch_id; removes ciphertext payload).
      3. Rewrites every snapshot/backup containing the target's
         ciphertext.
      4. Updates DD views to reflect tombstones.
      5. Emits EraseCompleted on success.
    """
    EVENT_KIND: ClassVar[str] = "request_erase"

    target_kind: str = ""             # "drawer" | "node" | "edge"
    target_id: str = ""
    requested_by_user: bool = True
    requested_at_ms: int = 0
    erasure_job_id: str = ""          # ties the request to the job


@dataclass
class EraseProgress(Event):
    """Progress report from an erasure compaction job.

    Multiple of these may be emitted during a long-running job. The
    schema is open — `scanned` / `rewritten` are the canonical
    counters but the job may add custom fields per phase.
    """
    EVENT_KIND: ClassVar[str] = "erase_progress"

    erasure_job_id: str = ""
    target_kind: str = ""
    target_id: str = ""
    phase: str = ""                   # "scan" | "rewrite_log" | "rewrite_snapshots" | "update_views"
    scanned: int = 0
    rewritten: int = 0
    progress_pct: float = 0.0
    progress_at_ms: int = 0


@dataclass
class EraseCompleted(Event):
    """An erasure compaction job completed.

    Once this event is logged, the target's ciphertext is gone
    irrecoverably. Structural references (e.g. derived_from edges
    on assertions) remain but are tombstoned — they point at the
    erased target via a tombstone marker rather than the original
    drawer_id.
    """
    EVENT_KIND: ClassVar[str] = "erase_completed"

    erasure_job_id: str = ""
    target_kind: str = ""
    target_id: str = ""
    completed_at_ms: int = 0
    events_tombstoned: int = 0
    snapshots_rewritten: int = 0
    bytes_freed: int = 0
    """Approximate ciphertext bytes that were removed."""


@dataclass
class EraseFailed(Event):
    """Erasure job aborted before completion.

    Distinct from EraseCompleted: the target's ciphertext may STILL
    be present (partially or fully). Operator intervention required;
    the job is idempotent so re-running picks up where it left off.
    """
    EVENT_KIND: ClassVar[str] = "erase_failed"

    erasure_job_id: str = ""
    target_kind: str = ""
    target_id: str = ""
    failed_at_ms: int = 0
    failure_reason: str = ""
    phase_at_failure: str = ""


@dataclass
class IntegrityLockout(Event):
    """The cloud-box can no longer refresh session bundles.

    Mirror of the on-chain IntegrityLockout PDA from R3 §7.6.
    Triggered by:
      - Burn-palace flow (phone destroyed Phone Master Key).
      - Repeated bundle-verification failures.
      - Daemon attestation chain broken.

    Once this fires, the cloud-box transitions to LOCKED_FAILED;
    no further decryption is possible until a new bundle is loaded
    (which requires the phone, which is gone if it was a burn).
    """
    EVENT_KIND: ClassVar[str] = "integrity_lockout"

    reason: str = ""
    """Human-readable reason. "burn_palace" | "bundle_refresh_failed" |
    "attestation_chain_broken" | etc."""

    triggered_by: str = ""            # "phone" | "daemon" | "operator"
    locked_out_at_ms: int = 0


# =============================================================================
# Walk audit (Track 3 — adaptive search policy)
#
# Per HANDLES_DESIGN.md v2 §"Search policy", the policy is itself
# learned: the substrate observes which interleaving patterns produce
# which result-quality signatures over time, and the heuristics improve.
# WalkCompleted is the audit record that future learned-policy code
# consumes.
# =============================================================================


@dataclass
class WalkCompleted(Event):
    """Audit record of one query's walk.

    Emitted by the handle layer when a search policy terminates a
    walk (either because the policy returned `terminate(...)` or
    because the budget was exhausted). The directive-trace lets a
    future learned-policy implementation correlate interleaving
    patterns with result-quality.
    """
    EVENT_KIND: ClassVar[str] = "walk_completed"

    handle_id: str = ""
    query_hash: str = ""
    """Stable hash of the originating query, for cross-walk
    correlation."""

    # Per-step directive trace (one entry per next_step() call).
    # Each entry: {"kind": "...", "rationale": "...",
    #              "fanout": N, "frame_id": "...", ...}
    # Free-form dict so the schema can evolve without a migration.
    directive_trace: list[dict[str, Any]] = field(default_factory=list)

    # Final state
    total_hops: int = 0
    final_top_frame_id: str = ""
    final_top_frame_confidence: float = 0.0
    terminate_reason: str = ""

    # Quality signals (filled in by the result-rendering layer if
    # available; empty otherwise). Names are open-ended; future
    # learned-policy code keys off whatever is present.
    quality_signals: dict[str, float] = field(default_factory=dict)

    # Cluster pattern signature at termination — for cross-walk
    # comparison and Track 4B cache projection.
    final_cluster_signature: str = ""

    completed_at_ms: int = 0


# =============================================================================
# Cache projection (Track 4B — HANDLES_DESIGN.md v2 §"Cluster-pattern caching")
#
# The default-distinct cache (Track 4A) keys on (query_hash,
# ranker_name, cluster_signature). When the projection mechanism
# observes K consistent equivalence observations across distinct
# cluster_signatures, it promotes the keys to a shared bucket.
# Divergence demotes back to distinct.
#
# Same online-learning shape as R3 §8.3: cooldowns, caps,
# reversibility, audit events.
# =============================================================================


@dataclass
class CacheProjectionPromoted(Event):
    """A cache key pair was promoted to a shared key after K consistent
    equivalence observations.

    The new lookup key uses `PROJECTED_CLUSTER_SIGNATURE` ("__projected__")
    in place of the actual cluster_signature; subsequent rankers under
    either cluster pattern read from the same shared entry.
    """
    EVENT_KIND: ClassVar[str] = "cache_projection_promoted"

    query_hash: str = ""
    ranker_name: str = ""
    promoted_cluster_signatures: list[str] = field(default_factory=list)
    """The signatures that were merged into the shared key."""

    observation_count: int = 0
    """How many equivalence observations triggered the promotion."""

    observation_window_ms: int = 0
    """The window over which the observations accumulated."""

    promotion_count: int = 0
    """How many times this key pair has been promoted (cumulative).
    The 3-strike cap on instability counts these."""


@dataclass
class CacheProjectionDemoted(Event):
    """A previously-promoted projection demoted back to distinct keys
    after observed divergence.

    Demotion happens when any divergence between projected entries
    appears within the demotion window (default 30 days since last
    promotion).
    """
    EVENT_KIND: ClassVar[str] = "cache_projection_demoted"

    query_hash: str = ""
    ranker_name: str = ""
    demoted_cluster_signatures: list[str] = field(default_factory=list)

    divergence_detail: str = ""
    """Human-readable description of what diverged. For audit."""

    days_since_last_promotion: float = 0.0


@dataclass
class CacheProjectionUnstable(Event):
    """A cache key pair has hit the promote ↔ demote cap (default 3
    cycles within a year) and is flagged as unstable.

    No further auto-promotion attempts; manual review surface only.
    The substrate records the event so the operator can investigate
    why the projection keeps oscillating — usually a sign that the
    cluster pattern actually matters and shouldn't be projected.
    """
    EVENT_KIND: ClassVar[str] = "cache_projection_unstable"

    query_hash: str = ""
    ranker_name: str = ""
    cycle_count: int = 0
    """Number of promote/demote cycles within the observation window."""

    flagged_within_days: float = 0.0


# =============================================================================
# Append rejection (Part 1.5)
# =============================================================================

@dataclass
class AppendRejected(Event):
    """A would-be append was rejected by validation.

    Logged so the log is self-describing and never silently corrupted.
    """
    EVENT_KIND: ClassVar[str] = "append_rejected"

    rejected_kind: str = ""
    rejected_payload_summary: str = ""
    error: str = ""


# =============================================================================
# Batch framing events (Phase 1)
#
# Multi-event operations open a batch with BatchStarted, emit their
# events with the same `batch_id`, and close with BatchCommitted on
# success or BatchAborted on failure. Recovery scans the log on startup
# for BatchStarted with no matching close — those are torn batches and
# their outputs are quarantined or discarded per consumer policy.
#
# `batch_id` on every Event ties events to a batch. Empty batch_id means
# the event is its own atomic single-event implicit batch (legacy /
# single-shot writes).
# =============================================================================

@dataclass
class BatchStarted(Event):
    """A multi-event batch has begun.

    `consumer_id` identifies the writer (e.g. "graph.assert_triple",
    "miner:class1", "migrate.converter"). `expected_count` is 0 if the
    writer doesn't know in advance.

    `input_summary` is a small structured description of what the batch
    is operating on. Three conventional shapes:
      - {"kind": "event_range", "start_offset": N, "end_offset": M}
      - {"kind": "frontier", "offset": N, "run_uuid": "..."}
      - {"kind": "external", "trigger_id": "..."}
    """
    EVENT_KIND: ClassVar[str] = "batch_started"

    consumer_id: str = ""
    expected_count: int = 0
    input_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchCommitted(Event):
    """A batch closed cleanly. All `actual_count` events under
    `batch_id` are valid.
    """
    EVENT_KIND: ClassVar[str] = "batch_committed"

    consumer_id: str = ""
    actual_count: int = 0


@dataclass
class BatchAborted(Event):
    """A batch was torn — either by an exception during execution or by
    the recovery protocol on restart finding an open batch with no close.

    `partial_count` is the number of events that did land before the
    abort. They are still in the log (it's append-only) but consumers
    should treat them per their quarantine/discard policy.

    `reason` is a short code: "exception" / "recovery_orphan" / "user_cancel" / etc.
    """
    EVENT_KIND: ClassVar[str] = "batch_aborted"

    consumer_id: str = ""
    partial_count: int = 0
    reason: str = ""
    detail: str = ""


@dataclass
class BatchCheckpointed(Event):
    """Mid-batch checkpoint marker (PHASE1 §J — sub-batch
    checkpointing).

    A long-running batch can emit `BatchCheckpointed` at safe
    intermediate boundaries. Recovery treats events between
    `BatchStarted` and the latest `BatchCheckpointed` as durable —
    only the trailing fragment after the last checkpoint is torn
    on crash.

    Example: a 10,000-event mining batch checkpoints every 1,000
    events. On crash at event 9,500, recovery rolls forward to the
    9,000-event checkpoint instead of replaying all 10,000.

    `output_index_so_far` is the batch's `output_index` at the
    moment of checkpoint (matches the count of events emitted
    under this batch_id between `BatchStarted` and this
    checkpoint, exclusive).

    `reason` is a short code: "periodic" / "boundary" / "manual" /
    etc. Diagnostic only.
    """
    EVENT_KIND: ClassVar[str] = "batch_checkpointed"

    consumer_id: str = ""
    output_index_so_far: int = 0
    reason: str = "periodic"


# =============================================================================
# Switchboard SDK events (ORACLE_THREAT_MODEL.md §4)
#
# These mirror on-chain `submit_finding`, `assign_resolver`,
# `submit_subject_blind_finding`, `challenge_subject_blind_finding`
# instructions. The off-chain listener observes the chain, derives
# Python events, and the dataflow bridge updates DD views accordingly.
# =============================================================================


@dataclass
class SwitchboardAssignmentReceived(Event):
    """A new resolution-job assignment arrived from the chain.

    Emitted by the SwitchboardNodeListener when it observes a
    `ResolverAssigned` on-chain event addressed to this resolver.
    Downstream `ResolutionJob` execution consumes this event.
    """
    EVENT_KIND: ClassVar[str] = "switchboard_assignment_received"

    assignment_id: str = ""
    """The PDA address of the on-chain ResolverAssignment."""

    market_id: str = ""
    privacy_mode: str = ""
    """One of: LOCAL_ONLY, SANDBOX, EXTERNAL, SUBJECT_BLIND
    (matches RegisteredOracleNode.PRIVACY_MODE_BIT_*)."""

    slice_request_spec: dict[str, Any] = field(default_factory=dict)
    """Per ORACLE_THREAT_MODEL §4.1: the spec for what slice the
    resolver should request from the subject palace. Empty for
    PUBLIC_LLM_RESOLVABLE markets."""

    k_threshold: int = 0
    n_resolvers: int = 0
    consensus_rule: str = ""
    """One of: median_distance, exact_match, any_value (mirrors
    ResolverAssignment.consensus_rule on-chain)."""


@dataclass
class SwitchboardFindingSubmitted(Event):
    """This resolver submitted a finding on-chain.

    Emitted after a successful `submit_finding` instruction. Lets
    DD views (match_cache, matched_against) record that this palace
    has produced a finding for this market, and lets the audit
    log close the loop with the finding payload.
    """
    EVENT_KIND: ClassVar[str] = "switchboard_finding_submitted"

    assignment_id: str = ""
    market_id: str = ""
    resolver_pubkey: str = ""
    resolver_index: int = 0
    """Index of this resolver within the ResolverAssignment.resolvers
    array (0-based)."""

    value_i128_micros: int = 0
    """Finding value in micros (1e-6 scale). For binary markets,
    1_000_000 = true / 0 = false. For continuous markets, the
    actual value scaled by 1e6."""

    confidence_interval_micros: int = 0
    num_samples: int = 0

    in_agreement: bool = True
    """Whether the on-chain `check_agreement` accepted this submission
    against the running consensus. False means the finding was
    recorded but didn't count toward K-of-N."""

    submitted_count: int = 0
    agreement_count: int = 0
    just_resolved: bool = False
    """True iff this submission caused K-of-N to hit (the market is
    now resolvable)."""

    consensus_value_i128_micros: int = 0
    """When just_resolved=True, the median (or per-rule consensus)
    of the in-agreement submissions. Zero otherwise."""

    on_chain_signature: str = ""
    """The Solana transaction signature for the submit_finding tx.
    Audit reference."""


@dataclass
class SubjectBlindDecloakOpened(Event):
    """A subject-blind finding entered its challenge window.

    Per ORACLE_THREAT_MODEL §4.4: after a resolver submits a
    subject-blind finding, the subject (or anyone with the
    derivation seed and original slice) has CHALLENGE_WINDOW_SLOTS
    to verify the finding by recomputing it. This event marks the
    window opening — observers (especially the subject's palace)
    use it to schedule a verification job.
    """
    EVENT_KIND: ClassVar[str] = "subject_blind_decloak_opened"

    finding_pda: str = ""
    """The on-chain SubjectBlindFinding PDA address."""

    market_id: str = ""
    resolver_pubkey: str = ""
    """Who submitted the finding."""

    derivation_seed_hash: str = ""
    """Hex of the resolver's commitment to the seed they used.
    Verifier reveals the actual seed to challenge."""

    slice_hash: str = ""
    """Hex of the slice the resolver claims to have processed.
    Verifier checks this against the slice it shipped."""

    finding_hash: str = ""
    """Hex of the finding payload's hash. Verifier checks the
    recomputed payload against this."""

    submitted_at_slot: int = 0
    challenge_window_ends_at_slot: int = 0


@dataclass
class SubjectBlindFindingChallenged(Event):
    """A subject-blind finding was successfully challenged.

    The challenger revealed the seed and slice, recomputed the
    finding, and got a different result. This event records the
    challenge; downstream code triggers the resolver's
    IntegrityLockout via `trigger_app_integrity_lockout`.
    """
    EVENT_KIND: ClassVar[str] = "subject_blind_finding_challenged"

    finding_pda: str = ""
    market_id: str = ""
    resolver_pubkey: str = ""
    challenger_pubkey: str = ""

    challenged_at_slot: int = 0
    recomputed_finding_hash: str = ""
    """The challenger's recomputed result hash. Different from the
    finding_hash on the original submission — that difference is
    what proves the resolver's submission was wrong."""

    integrity_lockout_triggered: bool = False
    """True iff this event also triggered the on-chain
    `trigger_app_integrity_lockout` for the resolver. The challenge
    handler doesn't do that directly (the challenger doesn't hold
    the resolver's lockout PDA seeds); off-chain finalizers do, and
    they update this flag once the trigger transaction lands."""


# =============================================================================
# Event registry — kind → class
#
# Used by validators and by the Rust side via JSON schema bundle.
# =============================================================================

EVENT_KIND_TO_CLASS: dict[str, type[Event]] = {
    cls.EVENT_KIND: cls
    for cls in (
        DrawerCaptured, DrawerAmended, DrawerHashCollision,
        NodeCreated, NodePropertySet, EdgeCreated, EdgeInvalidated,
        DrawerInvalidated, DrawerRevalidated,
        NodeInvalidated, NodeRevalidated, EdgeRevalidated,
        InterpretationAssigned, SchemaInduced, RecurrenceClusterMember,
        ContradictionAsserted, ContradictionResolved, AssertionOrphaned,
        JobScheduled, JobStarted, JobProgress, JobCompleted, JobFailed,
        JobPaused, JobResumed, ViewOffsetAdvanced, FeedbackRecorded,
        MatchRequestReceived, SandboxProvisioned, ForeignPalaceLoaded,
        MatchLayerCompleted, FindingEmitted, SandboxTornDown,
        HandleAllocated, HandleRefined, HandleResolved, HandleClosed,
        CanonicalPromoted, CanonicalRejected, CanonicalizationReverted,
        ModelLoaded, ModelInferenceCompleted, HeartbeatReceived,
        PhoneOffModeChanged,
        VoiceStepCompleted, TokenFeaturesWritten, SegmentCreated,
        AttestationChainBroken,
        RequestErase, EraseProgress, EraseCompleted, EraseFailed,
        IntegrityLockout,
        WalkCompleted,
        CacheProjectionPromoted, CacheProjectionDemoted,
        CacheProjectionUnstable,
        AppendRejected,
        BatchStarted, BatchCommitted, BatchAborted, BatchCheckpointed,
        SwitchboardAssignmentReceived, SwitchboardFindingSubmitted,
        SubjectBlindDecloakOpened, SubjectBlindFindingChallenged,
    )
}


# Type alias for "any event type" — used in client and subscriber signatures.
AnyEvent = Union[
    DrawerCaptured, DrawerAmended, DrawerHashCollision,
    NodeCreated, NodePropertySet, EdgeCreated, EdgeInvalidated,
    DrawerInvalidated, DrawerRevalidated,
    NodeInvalidated, NodeRevalidated, EdgeRevalidated,
    InterpretationAssigned, SchemaInduced, RecurrenceClusterMember,
    ContradictionAsserted, ContradictionResolved, AssertionOrphaned,
    JobScheduled, JobStarted, JobProgress, JobCompleted, JobFailed,
    JobPaused, JobResumed, ViewOffsetAdvanced, FeedbackRecorded,
    MatchRequestReceived, SandboxProvisioned, ForeignPalaceLoaded,
    MatchLayerCompleted, FindingEmitted, SandboxTornDown,
    HandleAllocated, HandleRefined, HandleResolved, HandleClosed,
    CanonicalPromoted, CanonicalRejected, CanonicalizationReverted,
    ModelLoaded, ModelInferenceCompleted, HeartbeatReceived,
    PhoneOffModeChanged,
    VoiceStepCompleted, TokenFeaturesWritten, SegmentCreated,
    AttestationChainBroken,
    RequestErase, EraseProgress, EraseCompleted, EraseFailed,
    IntegrityLockout,
    WalkCompleted,
    CacheProjectionPromoted, CacheProjectionDemoted,
    CacheProjectionUnstable,
    AppendRejected,
    BatchStarted, BatchCommitted, BatchAborted, BatchCheckpointed,
    SwitchboardAssignmentReceived, SwitchboardFindingSubmitted,
    SubjectBlindDecloakOpened, SubjectBlindFindingChallenged,
]
