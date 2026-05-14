# Substrate-signal analysis (Track 2)

**Status:** Analysis complete — output of Track 2 per
IMPLEMENTATION_ROADMAP.md and HANDLES_DESIGN.md v2 §"Deferred shape —
`InterpretiveFrame` fields."

**Output:** This doc (the analysis) + a code patch promoting
`InterpretiveFrame.fields: dict[str, Any]` to a typed dataclass with
the axes identified below.

## What this is

HANDLES_DESIGN.md v2 deferred the typed shape of `InterpretiveFrame`
until a substrate-signal analysis pass had identified what axes the
substrate actually produces. The opaque `fields: dict[str, Any]`
shape was placed as a stop-gap so traversal could proceed without
the taxonomy decision blocking it.

This pass:

1. Catalogs every substrate signal the codebase produces today.
2. Filters to the signals that are **query-time-relevant** — i.e.,
   could meaningfully differ between queries on the same substrate.
3. Groups the query-time-relevant signals by axis.
4. Proposes a typed `InterpretiveFrame.fields` shape replacing the
   opaque dict.

## 1. Substrate-signal catalog

What the codebase actually writes. By producer module.

### 1.1 DrawerCaptured (mempalace/drawer/capture.py)

Per-drawer fields. Always-on for the drawer; query-time-relevant
when used as a filter.

```
content_hash             stable identity (always-on)
capture_recorded_at      world time of capture (always-on)
duration_ms              acoustic duration (always-on)
interactional            "memo_to_self"|"two_party"|... (filter dim)
state_context            location, hands, gaze, posture, ... (filter dim)
goal_markers             list[str] from voice/text patterns (filter dim)
self_other_world         "self"|"other"|"world" (filter dim)
direct_participants      list[entity_id] (filter dim, most-used)
subjects_of_discussion   list[entity_id] (filter dim)
implicit_references      list[entity_id] (filter dim, fuzzy)
audience                 list[entity_id] (filter dim)
embedding_model_id       provenance (always-on)
acoustic_blob_ref        URI (always-on)
```

Plus encryption-at-edge fields (Track 5C): `encryption_schema_version`,
`verbatim_ciphertext`, `verbatim_dek_handle`, `verbatim_attestation_sig`,
`audio_blob_dek_handle`, `audio_blob_attestation_sig`,
`session_bundle_generation`. These are storage-substrate, not
query-time-relevant in themselves.

### 1.2 NodeCreated / NodePropertySet (substrate maintenance)

Per-node, always-on for the node:

```
node_id                  identity (always-on)
node_kind                THEME|PERIOD|EVENT|ENTITY|SCHEMA|... (filter)
properties               kind-specific, free-form (filter dim)
canonical                bool (filter dim — canonical-only views)
canon_path               canonical path string (filter)
importance               continuous [0,1] (rank input — Conway-relevant)
```

Track 6C added invalidation: `invalidated_at` (filter dim — exclude
hidden).

### 1.3 EdgeCreated / EdgeInvalidated

Per-edge:

```
edge_kind                CONTAINS|REFINES|ASSERTED_SUBJECT|...
                         + voice-stack additions: DRAWER_HAS_SEGMENT,
                         VOICE_MATCHES_REFERENCE, PARALINGUISTIC_EVENT_AT,
                         INTERPRETATION_MEMO_FOR(_SEGMENT)
source_node_id           (filter dim)
target_node_id           (filter dim)
valid_from / valid_to    bitemporal validity (filter dim)
weight                   continuous (rank input)
confidence               continuous (rank input)
derivation               OBSERVATION|INFERENCE|ASSUMPTION|CANONICAL|
                         AUTO_MERGE|USER_MERGE (filter — provenance class)
properties               edge-kind-specific (filter dim)
invalidated_at           Track 6C (filter)
```

### 1.4 InterpretationAssigned (miner outputs)

Per-node/field, supersedes-style:

```
field_name               "memory_type"|"importance"|"confidence"|...
new_value                kind-specific
miner_pass_version       provenance (always-on)
confidence               continuous (rank input)
```

The set of `field_name` values is open; common ones from the miners:
`memory_type`, `interactional`, `self_other_world`, `goal_marker`,
`assertion_*`, `contradiction_*`.

### 1.5 SchemaInduced (Class 3)

Per-schema-version snapshot:

```
schema_kind              TRAIT|RELATIONAL|POSSIBLE_SELF|...
name, description        always-on
miner_pass_version       provenance
derived_from_events      list[event_id]
derived_from_assertions  list[assertion_id]
derived_from_drawers     list[drawer_id]
stability_score          continuous [0,1] (rank input, Conway-relevant)
coverage_score           continuous [0,1] (rank input)
supersedes_schema_id     supersession chain (always-on)
```

### 1.6 SignatureSnapshot (R3 §8.2)

This is the **richest** signature-region source:

```
mean_position_by_theme       theme_id → centroid vector (D ~ 256–768)
velocity_by_theme            theme_id → velocity scalar (drawers/period)
schema_fingerprints          list[str] active in period
contradiction_profile        contradictions_seen, _resolved,
                             mean_resolution_latency_days,
                             resolution_strategy_split
fork_distribution_by_theme   theme_id → bucketed length-5 distribution
drawer_count                 always-on
assertion_count              always-on
captured_at_ms / window      always-on
```

Per HANDLES_DESIGN.md v2 §"Deferred shape," this is where frame fields
should pull from — frames are *targeted regions in signature space*
specialized to a query.

### 1.7 CanonicalEntry (canonicalizer)

```
canonical_id, surface       identity
embedding                   vector
aliases                     list[str]
member_count                cluster size (rank input)
domain                      PREDICATES|MEMORY_TYPES|SCHEMA_NAMES|
                            ENTITY_ALIASES|PERIOD_NAMES
```

The aliases + member_count fields drive disambiguation under cluster
patterns: a query that matches multiple aliases reveals that the
canonical is well-supported.

### 1.8 Features (mempalace/features/registry.py)

Computed per-node, query-time-relevant by definition:

```
drawer_recency_score             rate-weighted recency
drawer_heat                      retrieval-burnt heat field
drawer_velocity_30d              30-day rate of new drawers in this region
theme_canonicality               continuous, schema-stability-derived
event_fork_significance          fork distribution position
assertion_substrate_faithfulness substrate-grounding score
period_velocity_coupling         period-rate coupling to theme rate
stance_alignment_score           query-stance ↔ candidate-substrate alignment
```

These ARE the first-pass per-node signals a frame should bias toward
or against. A frame that "wants Class 1 rate" weights `drawer_heat`
and `drawer_recency_score` more; a frame that "wants Class 3 rate"
weights `theme_canonicality`.

### 1.9 Miner proposals (Class 1, Class 2, Class 3)

These are pre-confirmed; converted to `InterpretationAssigned` events
via the proposal store. Already covered above (1.4, 1.5).

### 1.10 Voice substrate (Track 1A)

Per-token features and per-segment aggregates:

```
TokenFeatures:
  token, onset_ms, offset_ms       always-on
  prosody (pitch, energy, rate)    rank input
  affect (categorical distribution) rank input
  speaker_label                    filter dim
  speaker_label_confidence         rank input
  produced_by_model_pass           per-feature provenance

DrawerSegment:
  segment_id, drawer_id, bounds    always-on
  dominant_speaker_label           filter dim
  dominant_affect                  filter dim + rank input
  dominant_affect_confidence       rank input
  accent_distribution              filter dim (soft)

Paralinguistic events:
  laughter, sigh, breath, code_switch, pause, filler_um, filler_uh
  attached via PARALINGUISTIC_EVENT_AT edges
```

Memos override: when an `INTERPRETATION_MEMO_FOR(_SEGMENT)` edge
points at a voice region, the prosody/affect produced by that pass is
overridden. The memo itself is substrate.

### 1.11 TriageIndicator + DriftReport (signature consumers)

These are derived from signatures, not new substrate. But their
outputs feed handle context:

```
TriageScore:
  pair_score                       overall similarity in [0,1]
  per_axis_similarity              dict[axis_name, float]
                                   (axes from signature dims)
  weights_used                     query-policy snapshot
  gate_passed                      bool

DriftReport:
  per_axis drift values            for each signature dim
  baseline_window_meets_minimum    eligibility flag
```

These ARE candidates for frame fields: a `triage_score` field on a
frame would be the per-axis similarity of this query's signature
region to candidate-region signatures.

### 1.12 Derived caches (foyer, transition, ranker_output, realtime_index)

Caches over upstream signals. Not new substrate; their stability
contributes to frame confidence (a frame backed by warm caches is
"warmer" than one whose deps are dirty).

## 2. Filter to query-time-relevant

A signal is **query-time-relevant** if two queries on the same
substrate could see different values for it.

| Signal | QTR? | Why |
|---|---|---|
| `content_hash`, `node_id`, `edge_id` | No | Substrate identity. Same for every query. |
| `node_kind`, `edge_kind` | Filter dim | Same per-substrate, but queries filter on it. |
| `direct_participants`, `subjects_of_discussion` | Filter dim | Same per-substrate, but queries scope on it. |
| `interactional`, `self_other_world`, `goal_markers` | Filter dim | Same. |
| `state_context` | Filter dim | Same per-substrate, but queries match against it. |
| `valid_from`/`valid_to` | Filter dim | Time-scope filtering. |
| `weight`, `confidence` (edge) | Yes — rank input | Same value on substrate, but rankers weigh it differently per query. |
| `importance`, `canonical`, `canon_path` (node) | Yes — rank input | Same. |
| `derivation` | Filter dim | Provenance class filtering. |
| **`mean_position_by_theme`** | **Yes — frame axis** | Per-query distance to query's theme region. |
| **`velocity_by_theme`** | **Yes — frame axis** | Frames carry a "rate" slot that maps onto this. |
| **`schema_fingerprints`** | **Yes — frame axis** | Query may target an active schema or not. |
| **`contradiction_profile`** | **Yes — frame axis** | Query in the resolved-contradiction zone vs. unresolved. |
| **`fork_distribution_by_theme`** | **Yes — frame axis** | Frame may target stable vs. forking themes. |
| `stability_score`, `coverage_score` (schema) | Yes — frame axis | Conway-rate alignment. |
| **`drawer_recency_score`** | **Yes — feature** | Per-query weight via stance. |
| **`drawer_heat`** | **Yes — feature** | Same. |
| **`drawer_velocity_30d`** | **Yes — feature** | Same. |
| **`theme_canonicality`** | **Yes — feature** | Same. |
| **`event_fork_significance`** | **Yes — feature** | Same. |
| **`assertion_substrate_faithfulness`** | **Yes — feature** | Same. |
| **`period_velocity_coupling`** | **Yes — feature** | Same. |
| **`stance_alignment_score`** | **Yes — feature** | Per-query by definition. |
| `prosody`, `affect`, `speaker_label` (voice) | Filter + rank | Voice-flavored frames consume these. |
| `accent_distribution` | Filter dim | Frame may target a region. |
| Paralinguistic events (laughter, etc.) | Filter + rank | Frame may target events of a kind. |
| `pair_score`, `per_axis_similarity` (triage) | Yes — frame field | Per-query similarity to candidate substrate region. |
| `per_axis drift values` | Yes — frame field | Drift snapshot at query time. |
| `coherence` (CoherenceEntry) | Yes — rank input | Per-(source, target) pair coherence at query time. |
| `foyer rendered_at_ms` | No | Cache provenance only. |

## 3. Group by axis

The query-time-relevant signals cluster into five axes. Each axis is
a **dimension** along which an `InterpretiveFrame` carries
specialization. A handle's set of frames, taken together, is the
query's accumulated routing intelligence.

### Axis 1 — **Signature region**

A targeted region in signature space. Specialization of R3 §8.2
dimensions to a query.

**Sources:** `mean_position_by_theme`, `velocity_by_theme`,
`schema_fingerprints`, `contradiction_profile`,
`fork_distribution_by_theme`, `pair_score`/`per_axis_similarity`
from TriageScore.

**Frame field shape:**
```python
@dataclass
class SignatureRegion:
    centered_on_theme_ids: list[str]          # which themes
    target_position: dict[str, list[float]]   # theme_id → centroid
    target_velocity_band: tuple[float, float] # min, max velocity
    schema_fingerprints_required: list[str]   # which schemas active
    contradiction_zone: str                   # "resolved"|"unresolved"|"any"
    fork_distribution_target: list[float] | None  # length-5 bucket dist
```

A frame with this filled means "queries should weight candidates that
sit in this region of signature space."

### Axis 2 — **Conway rate**

The Conway 2005 framework distinguishes Class 1 (episodic-buffer,
fast) from Class 3 (autobiographical-knowledge, slow). Frames carry
a consolidation rate matching where their evidence lives.

**Sources:** `stability_score`, `coverage_score` (schemas);
`drawer_recency_score`, `drawer_heat`, `drawer_velocity_30d`
(features).

**Frame field shape:**
```python
@dataclass
class ConwayRate:
    target_rate: int                # 1, 2, or 3
    rate_confidence: float          # how strongly committed
    rate_features_weight: dict[str, float]
        # weights to apply to per-feature rank inputs:
        # rate=1 → boost recency, heat
        # rate=3 → boost canonicality, schema-stability
```

A query that's specifically about a recent event has rate=1 frames.
A query about long-running themes has rate=3 frames. Multiple frames
at different rates are the norm — a query "what did we learn about
running last quarter" has both.

### Axis 3 — **Co-activation pattern**

Frames inherit from regions of substrate co-activation that the
miner has already identified. This is what makes signal-to-noise
improve over time.

**Sources:** `recurrence_cluster_member` events, `derived_from_*`
edges, `RecurrenceClusterMember` data, schema `derived_from_drawers`.

**Frame field shape:**
```python
@dataclass
class CoActivationPattern:
    seed_recurrence_cluster_ids: list[str]
        # ground the frame in clusters miner already produced
    seed_drawer_ids: list[str]
        # specific exemplars
    co_active_node_kinds: dict[str, float]
        # node_kind → preference weight (e.g. THEME: 0.8, EVENT: 0.5)
    co_active_edge_kinds: dict[str, float]
        # edge_kind → preference weight
```

This is the closest thing to v1's `preferred_edge_kinds`/
`preferred_node_kinds`, but grounded in observed co-activation rather
than a guessed taxonomy.

### Axis 4 — **Refinement-derived**

Signals from the user's refinements during the query — voice cues
indicating which speaker is meant, more-like / less-like nudges,
stance adjustments.

**Sources:** `RefinementSignal` (existing in retrieve/handle.py),
voice match candidates from Track 1A's SpeakerMatchStep, paralinguistic
events.

**Frame field shape:**
```python
@dataclass
class RefinementCues:
    more_like_node_ids: list[str]
    less_like_node_ids: list[str]
    stance_pulls: dict[str, float]
        # axis_name → adjustment in [-1, 1]
    voice_match_pulls: list[tuple[str, float]]
        # (entity_id, confidence) — speaker-match candidates
    voice_affect_pulls: dict[str, float]
        # affect category → preference weight
    paralinguistic_event_filters: list[str]
        # event_kind values to gate on
```

### Axis 5 — **Voice-flavored**

Voice signals from Track 1A integrated as a first-class axis, not as
ad-hoc overrides. Per VOICE_STACK_DESIGN.md, voice features at
per-token granularity matter for routing.

**Sources:** `TokenFeatures.prosody`, `TokenFeatures.affect`,
`DrawerSegment.dominant_speaker_label`, `DrawerSegment.accent_distribution`,
`paralinguistic_event_at` edges, `voice_matches_reference` edges.

**Frame field shape:**
```python
@dataclass
class VoiceFlavor:
    target_speaker_entities: list[str]          # voice match targets
    target_affect_distribution: dict[str, float]   # category → weight
    accent_region_pulls: dict[str, float]       # category → weight
    target_paralinguistic_event_kinds: list[str]
    prosody_target: dict[str, tuple[float, float]] | None
        # feature_name → (min, max) target band
        # e.g. "pitch_hz": (180, 260) for "raised pitch"
    confidence: float
        # how strongly this frame leans on voice (vs degrades to
        # text-only signals when voice is absent)
```

## 4. The typed `InterpretiveFrame` shape

Replacing the v2 design's `fields: dict[str, Any]` placeholder:

```python
@dataclass
class InterpretiveFrame:
    """A query-in-flight's specialization to a region of substrate.

    Multiple frames per handle, accumulating as refinements come in.
    Each frame holds typed slots per axis from the substrate-signal
    analysis (Track 2 output). Slots that don't apply are None.
    """

    # Identity + meta
    frame_id: str
    confidence: float                       # in [0, 1]
    description: str                        # human-readable
    derived_from_refinements: list[int]     # indices into HandleState.refinements

    # The five axes
    signature_region: SignatureRegion | None = None
    conway_rate: ConwayRate | None = None
    co_activation_pattern: CoActivationPattern | None = None
    refinement_cues: RefinementCues | None = None
    voice_flavor: VoiceFlavor | None = None
```

The five axis types are themselves dataclasses (above). `None` means
"this axis doesn't apply to this frame" — a frame that's purely about
voice has only `voice_flavor` populated.

## 5. Open questions deferred to later tracks

- **How frames combine when ranking.** The trusted aggregator (per
  R3 §1.2) needs to consume a list of frames and produce a single
  weighted ranking. The v1 design hand-waved this as
  `associative_weight_overrides`. The right solution probably uses
  per-frame `confidence` to weight, but the formula is
  ranker-specific. Track 3 (search policy) is the natural home.

- **How frames are produced.** Initial frame production is on
  `mem_allocate` (one frame per scope element) and `mem_refine`
  (refinement-derived frames). The miner produces co-activation
  patterns; the signature module produces signature regions; voice
  steps produce voice-flavor frames. Wiring is per-track.

- **Frame retirement / obsolescence.** Frames that haven't been
  exercised in K hops get demoted in confidence; frames whose
  evidence is invalidated (Track 6C) get retired. Mechanic
  not yet specified — same shape as cluster-pattern projection
  cache demotion (Track 4B).

## 6. What ships with this analysis

The companion code patch ships:

1. New module `mempalace/handle/frame.py` containing the typed
   dataclasses: `InterpretiveFrame`, `SignatureRegion`, `ConwayRate`,
   `CoActivationPattern`, `RefinementCues`, `VoiceFlavor`.
2. Each axis-type dataclass with sensible defaults so frames can be
   constructed incrementally as evidence accumulates.
3. Tests for the dataclass shapes — round-trip serialization,
   defaults, axis composition.

The opaque `fields: dict[str, Any]` from v2 is gone.

The `HandleState` itself doesn't yet exist as code (only design); when
the handle module is implemented, it imports `InterpretiveFrame` from
this new module.

## 7. Sequencing

Track 2 unblocks:

- **Track 3** (adaptive search policy) needs the typed frame shape to
  decide what each axis means for breadth-vs-depth at each hop.
- **Track 4B** (cache projection) needs the typed shape to compare
  cached entries across cluster patterns — the projection mechanism
  uses frame fields as part of its equivalence test.

Tracks 4A, 5, 6 don't depend on Track 2; they shipped prior.
