# Handles — Design (v2)


## What handles are today

Handles are what `mempalace/handle/` produces and `mempalace/retrieve/`
consumes. A `HandleState` carries: an opaque ID, the originating
query, the user's stance, an accumulated list of `Refinement` objects,
and the current candidate node set. It's the unit that flows through
the multi-step retrieval pipeline.

What handles do well today: capture intent. Carry the query +
refinements through ranker calls.

What handles don't do well today: carry the **routing intelligence**
that emerges during a query's lifetime. The ranker that runs on hop 5
sees the same input as the one on hop 1 — no accumulated "we've been
walking through period→entity edges, keep doing that" signal, no
"this query keeps confirming voice cues, weight voice candidates
higher," no "two competing interpretations are both alive, the
ranking should reflect both." That signal is what handles need.

## What handles need to be

A handle is a **query in flight** carrying:

- The original intent and its refinements (already there).
- A working representation of "what's warm" — which substrate
  regions, axes, and patterns this query is finding signal in. Not
  just nodes; not just kinds; *trajectories through the substrate*.
- Multiple competing interpretations, each with its own activation
  pattern, weighted by accumulated evidence.
- A search policy that interleaves breadth/depth dynamically based
  on whether one interpretation dominates, several compete, or the
  walk is stuck.

### Outer skeleton — what's stable

These pieces are stable because they don't depend on the internal
taxonomy:

```python
@dataclass
class HandleContext:
    """Routing intelligence accumulated during a query's lifetime."""

    # See "deferred shape" below. Internal fields will be filled in
    # after the substrate-signal analysis pass.
    interpretations: list[InterpretiveFrame] = field(default_factory=list)

    # Search policy controlling per-hop breadth/depth interleaving.
    # See "Search policy" below.
    search_policy: SearchPolicy = field(default_factory=lambda: SearchPolicy.adaptive())

    # Cluster traversal pattern — recent K hops; used by ranker
    # context updates and by cache-key derivation. Stable shape.
    cluster_pattern: ClusterTraversalPattern | None = None

    # Per-frame and per-cluster derived caches. Promoted/demoted by
    # the projection mechanism described under "cluster-pattern
    # caching" below. Stable shape.
    projection_cache_status: dict[str, ProjectionStatus] = field(default_factory=dict)
```

```python
@dataclass
class ClusterTraversalPattern:
    """Sliding window of recent hops + derived dominance signals."""
    recent_hops: deque[Hop]               # bounded; default K=8
    dominant_edge_kinds: list[str]
    dominant_node_transitions: list[tuple[str, str]]

    def cluster_signature(self) -> str:
        """Stable hash of the pattern."""
        ...
```

```python
@dataclass
class Hop:
    from_node_id: str
    to_node_id: str
    edge_id: str
    edge_kind: str
    edge_confidence: float
    chosen_by: str
    chosen_at_step: int
```

### Deferred shape — `InterpretiveFrame` fields

The v1 shape was:

```python
@dataclass
class InterpretiveFrame:
    frame_id: str
    confidence: float
    description: str
    seed_nodes: list[str]
    preferred_edge_kinds: dict[str, float]
    preferred_node_kinds: dict[str, float]
    associative_weight_overrides: dict[str, float]
    voice_pulls_inherited: dict[str, float]
    derived_from_refinements: list[int]
```

This is what graph search engines do. It is not what falls out of the
substrate. The right shape comes from analyzing what signals the
substrate already produces and specializing them to a per-query
"region of interest." Candidate substrate signals to draw frame
fields from:

- **Signature dimensions specialized to the query.** The signature
  module (R3 §8.2) already tracks mean position, velocity field,
  schema fingerprint, contradiction-resolution profile,
  fork-significance distribution. A frame might be a *targeted region
  in signature space* for this query rather than a list of preferred
  kinds.
- **Conway-rate alignment.** Frames may carry a consolidation rate
  matching where their evidence lives. A "raw audio cue" frame is
  Class-1-rate; a "thematic interpretation" frame is Class-3-rate.
  The same query carries multiple frames at different rates that
  refine on different cadences.
- **Observed co-activation patterns.** Frames may inherit from
  regions of substrate co-activation that the miner has already
  produced. This is what makes signal-to-noise improve over time —
  the same query under more accumulated co-activation gets sharper
  frames.

The implementation work to pin this shape is described in
§"Implementation that's left" below. Until then, code that needs to
construct frames uses a placeholder `InterpretiveFrame` with just
`frame_id`, `confidence`, `description`, and an opaque
`fields: dict[str, Any]` for everything else. The opaque-dict shape
lets traversal proceed without the taxonomy decision blocking it;
when the analysis lands, the dict gets typed.

### Cluster-pattern caching — both, with auto-projection

The v1 question was "should two ranker calls under different cluster
patterns share a cache or have distinct cache entries." The answer is
both, with the nuance that prevents collisions:

**Default**: cache keyed by `(query_hash, ranker_name, cluster_signature)`.
Distinct entries — collision-safe. The same node under a different
cluster pattern gets a different cached score.

**Layered on top**: a projection cache observes when two distinct
keys produce equivalent outputs over time. After K consistent
equivalence observations within a window (default K=10 over 7 days),
the projection promotes them to a shared key with `cluster_signature`
dropped. Subsequent rankers under either cluster pattern read from
the shared key.

**Demotion**: any divergence between projected entries within a
window (default 30 days since last promotion) demotes back to
distinct keys. The demotion is recorded as a
`cache_projection_demoted` event so the substrate can audit.

**Constraints (mirroring R3 §8.3 signature triage feedback loop):**
- A single dimension can't be projected away to zero — the cluster
  pattern is always a contributor to the cache key, even when
  projected; promotion just makes its contribution a shared bucket
  rather than its own bucket.
- Cooldowns: a single equivalence observation can't trigger another
  promotion attempt within 30 days for the same key pair.
- Caps: a key pair can only be promoted ↔ demoted N times (default
  N=3) within a year before the substrate flags it as unstable and
  refuses further auto-promotion (records as
  `cache_projection_unstable`; manual review surface).

This is the same online-learning shape as the signature triage
feedback: cooldowns, caps, reversibility, audit events. The shared
cache is what most ranker calls hit; the distinct cache is the
safety net for cases where the cluster pattern actually matters.

### Search policy — breadth ↔ depth interleaving

The v1 traversal was static: "walk K hops, update context, repeat."
The substrate is bottom-up; the search shouldn't be top-down-static.

```python
@dataclass
class SearchPolicy:
    """Controls per-hop breadth/depth interleaving during a walk."""

    @staticmethod
    def adaptive() -> "SearchPolicy":
        """Default policy: data-dependent interleaving."""
        ...

    def next_step(
        self,
        context: HandleContext,
        budget: SearchBudget,
    ) -> StepDirective:
        """Decide what kind of step to take next.

        Returns:
          StepDirective.expand_breadth(N=...)   # rank N candidates side-by-side
          StepDirective.commit_depth(frame_id)  # depth-first into one frame
          StepDirective.alternate(frame_a, frame_b, depth=K)
                                                # alternating depth-K walks for compare
          StepDirective.terminate(reason=...)   # walk is done
        """
        ...
```

The decision is data-dependent, not a global config:

- **High dispersion across frames + budget remaining** → expand_breadth.
  The walk is exploring; pull more candidates per step.
- **One dominant frame (confidence > threshold) + others fading** →
  commit_depth into the dominant frame. Stop spending budget on the
  losing frames.
- **Two close-confidence frames + dwindling budget** → alternate
  depth-K walks. Compare the converged endpoints. Whichever produces
  the higher-quality result wins; the loser's frame confidence drops.
- **Walk is stuck (cluster pattern stable for many hops, no
  confidence change)** → terminate, surface what we have.

The policy is itself learned. The substrate observes which interleaving
patterns produce which result-quality signatures over time
(audit-tracked via `walk_completed` events with quality metrics)
and the policy heuristics improve. Same bottom-up loop as everything
else in the system.

The first cut of `SearchPolicy.adaptive()` uses static thresholds;
the learned version is implementation-deferred work.

### How rankers read and write `HandleContext`

Same protocol extension as v1:

```python
class Ranker(Protocol):
    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        handle_context: HandleContext | None = None,
    ) -> list[ScoredCandidate]:
        ...

    def update_context(
        self,
        handle_context: HandleContext,
        ranked: list[ScoredCandidate],
        chosen: list[str],
    ) -> None:
        """Update context based on what got chosen this hop."""
        ...
```

The opaque-dict `InterpretiveFrame.fields` means rankers that want to
write into a frame use known keys ("voice_match_strength",
"theme_pull_strength", etc.) rather than typed fields. After the
substrate analysis lands, those keys become typed.

### Multi-hop traversal protocol

The traversal driver:

```python
def traverse(
    handle: HandleState,
    *,
    budget: SearchBudget,
    rankers: list[Ranker],
) -> TraverseResult:
    """Walk the DAG starting from frame seeds, ranking candidates at
    each hop, updating context, terminating per the search policy."""

    while not budget.exhausted():
        directive = handle.context.search_policy.next_step(
            handle.context, budget,
        )
        match directive:
            case ExpandBreadth(n):
                candidates = expand_candidates(handle, n=n)
                ranked = rank_with_context(candidates, handle.context, rankers)
                chosen = ranked[:n]
                for r in rankers:
                    r.update_context(handle.context, ranked, [c.node_id for c in chosen])
                handle.context.cluster_pattern.advance(chosen)
            case CommitDepth(frame_id):
                # depth-first into one frame
                ...
            case Alternate(a, b, depth):
                ...
            case Terminate(reason):
                break
        budget.consume(directive)
    return collect_result(handle)
```

The driver handles bookkeeping; the policy handles strategy.

### Voice — the rewrite

Voice retrieval cues only carry information through the DAG if the
substrate has the per-token features available. The v1 doc had
voice as a single `voice_pulls: dict[str, float]` keyed by speaker_id.
That's both too coarse (speaker_id alone misses prosody, accent,
affect) and improperly modeled (voice was treated as a property of a
drawer, but it's a per-token property of an audio segment).

#### Per-token features

The substrate change: per-token rather than per-drawer. Each token
the ASR produces carries an aligned feature vector:

```python
@dataclass
class TokenFeatures:
    token: str
    onset_ms: int
    offset_ms: int

    # Per-token paralinguistic features. Each carries a confidence so
    # downstream consumers don't have to invent thresholds.
    prosody: ProsodyVector | None = None              # tone, pitch, energy
    affect: AffectDistribution | None = None          # categorical with confidences
    speaker_label: str | None = None                  # diarization label, scoped to this drawer
    speaker_label_confidence: float | None = None

    # Provenance: which model pass produced these fields.
    produced_by_model_pass: dict[str, str] = field(default_factory=dict)
    # Keyed by feature name → model_pass_id. Lets the dependency
    # tracker invalidate when a model is upgraded.
```

#### Segments are first-class

A drawer has child segments — first-class substrate entities:

```python
@dataclass
class DrawerSegment:
    segment_id: str
    drawer_id: str
    start_ms: int
    end_ms: int

    # Aggregate signals over the segment
    dominant_speaker_label: str | None = None
    dominant_affect: str | None = None
    dominant_affect_confidence: float | None = None

    # Voice-match candidates: which reference voices this segment
    # might be impersonating or sounding like. Edges into the DAG,
    # not embedded data.
    # See `voice_matches_reference` edge kind.
```

Edge kinds added:
- `drawer_has_segment` (drawer → segment)
- `voice_matches_reference` (segment → entity reference, with
  confidence). The "reference" is just another entity in the DAG —
  a colleague you spoke with, a celebrity, a fictional character
  you've referenced before. No global accent registry; the references
  come from your own DAG.
- `paralinguistic_event_at` (segment → paralinguistic-event node).
  Laughter, sighs, breath, code-switching are first-class events,
  not noise to filter out.

#### Memos as override signals — segment-targeted

A drawer authored shortly after another can be linked via
`interpretation_memo_for`. Edge can target a drawer OR a segment:

```python
# Drawer-level memo
@dataclass
class InterpretationMemoFor(Edge):
    target_drawer_id: str
    # Memo applies to the whole drawer. "I was being sarcastic in
    # the previous take."

# Segment-level memo (more useful)
@dataclass
class InterpretationMemoForSegment(Edge):
    target_segment_id: str
    # Memo applies to a specific segment. "The angry tone in seconds
    # 12-18 was theatrical."
```

The refinement engine treats memos as ground truth for the targeted
drawer/segment's interpretation. When you append "I was impersonating
X in seconds 30-45," the inference layer doesn't get to second-guess
that with prosody analysis; the memo wins.

#### Voice retrieval cues in HandleContext

The `voice_pulls` axis becomes one possible field in
`InterpretiveFrame.fields` (under the deferred-taxonomy approach
above). Likely shape, pending the analysis pass:

```python
{
    "voice_pulls": {
        # Keyed by (reference_id, segment_signature). NOT speaker_id —
        # the reference is the entity in the DAG; segment_signature
        # captures the prosody/affect of the segment that pulled.
        ("ent_colleague_alice", "sig_assertive_excited"): 0.7,
        ("ent_celebrity_bob", "sig_drawl_southern"): 0.4,
    }
}
```

The shape lets a frame say "queries that pull through this kind of
voice context" rather than "queries about speaker X."

### Where frames come from (revised)

A frame is born from one or more refinement signals + the initial
query. The bootstrap ranker (or a dedicated `FrameSeeder` component)
examines the query and the first refinement and proposes 1-3 frames.
Triggers (the catalog grows as more substrate signals come online):

- "Topic frame": query mentions a theme name → frame seeded at theme node
- "Person frame": query mentions an entity name → frame seeded at entity
- "Time frame": query mentions a period → frame seeded at period
- "Voice frame": query carries paralinguistic cues OR was authored
  shortly after a voice-rich drawer → frame seeded at voice-match
  candidates
- "Memo-explicit frame": query targets a drawer/segment with an
  active interpretation memo → frame seeded at the memo's stated
  interpretation, with high confidence

Subsequent refinements either strengthen an existing frame or spawn a
new one. Frames die when their confidence falls below threshold
(default 0.1) — but death is reversible: a frame can be revived if a
later refinement bumps it back over threshold within a window.

## How this fits the existing 5-phase substrate

Same composition story as v1 with one update for the projection cache:

- **Phase 1 (batch framing)**: traversal is a multi-event batch.
  `BatchStarted("retrieve.traverse", ...)` opens the batch; each
  Hop lands as an event under the batch_id; close on natural
  termination. Torn traversal detectable; partial walk discarded.
- **Phase 2 (versioning)**: `VersionStamp.content_hash` includes the
  cluster_signature when the projection cache says distinct, OR omits
  it when projected to shared. The projection state itself is a
  versioned input — when projection_cache_status changes, the
  affected ranker outputs invalidate.
- **Phase 3 (frontier)**: traversal reads from
  `consistent_read_snapshot([consumer_ids])`.
- **Phase 4 (dependency tracking)**: ranker outputs depend on
  substrate fields, model pass versions (per-token features carry
  their `produced_by_model_pass`), and the projection cache state.
  When any of those shift, the dependency tracker invalidates.
- **Phase 5 (DD on Rust)**: the DD views `current_nodes`,
  `current_edges`, `velocity_field`, `recurrence_clusters` are read
  by the traversal driver. Voice-segment edges (`drawer_has_segment`,
  `voice_matches_reference`) flow through `current_edges` like any
  other edge kind — no new view needed.

## Implementation that's left

This v2 design has three things deferred to implementation work that
should land together as a coherent patch:

### 1. Substrate-signal analysis pass

The decision deferred: what fields does `InterpretiveFrame` actually
carry. The work:

- Catalog every signal the substrate produces today: drawer fields,
  assertion fields, schema fields, signature dimensions, miner
  outputs, canonicalizer outputs, ranker outputs.
- Identify which signals are query-time-relevant (i.e., could
  meaningfully differ between queries) vs. always-on (the same value
  for every query).
- Group the query-time-relevant ones by axis (signature region,
  rate, co-activation pattern, refinement-derived).
- Propose a typed `InterpretiveFrame` shape that captures the axes
  with the strongest signal.

Output of this work is a follow-on doc proposing the typed shape and
a code patch promoting `InterpretiveFrame.fields: dict[str, Any]` to
a typed dataclass.

### 2. Voice stack

The voice rewrite (per-token features, segments first-class, memos
as override) implies a sibling of `mempalace/stack/text/`:
`mempalace/stack/voice/`. Stack of independent steps, each writing
its outputs as substrate fields the DAG can index. Each step is an
`AttestedStep` per R3 §1.4, runs LOCAL_ONLY (no network egress),
emits a `model_attestation` event per pass:

| Step | Output | Substrate write |
|---|---|---|
| ASR | tokens with onset/offset | `TokenFeatures.token`, `TokenFeatures.onset_ms`, `TokenFeatures.offset_ms` |
| Diarization | speaker labels per token range | `TokenFeatures.speaker_label` |
| Speaker matching | maps labels → entity references | `voice_matches_reference` edges from segment to entity, with confidence |
| Prosody / affect | per-token prosody and affect distributions | `TokenFeatures.prosody`, `TokenFeatures.affect` |
| Accent / origin | per-segment accent distribution | `DrawerSegment` field; soft distribution, not a label |
| Paralinguistic event | laughter, sighs, breath, code-switching | `paralinguistic_event_at` edges from segment to event nodes |

Each step:
- Has a stable `step_id` and `model_pass_version`.
- Writes `produced_by_model_pass[feature_name] = step_id@version` so
  dependency tracking can invalidate when the step is upgraded.
- Runs as `AttestedStep` with privacy mode LOCAL_ONLY.
- Outputs flow through the existing event log and become DD-view-
  visible like any other write.

The stack is ordered (ASR before diarization; diarization before
speaker matching; both before higher-level affect interpretation),
but individual steps are independently swappable. Upgrading one
step only invalidates the substrate fields it produced.

The implementation work:
- Module skeleton at `mempalace/stack/voice/` with the stack
  composition wired through.
- Schema: add `TokenFeatures`, `DrawerSegment`, the new edge kinds,
  the `model_attestation` event for each voice step.
- Step implementations: each step calls a local model. Initial
  cut: stub models that return canned outputs from test fixtures;
  real model integration is its own follow-on.
- Migration: existing drawers without per-token features stay valid
  (the voice fields are all optional). New captures go through the
  full stack.
- Tests: per-step contract tests, full-stack composition test,
  override-memo precedence test.

### 3. Adaptive search policy

The v2 doc commits to a `SearchPolicy.adaptive()` factory and a
`StepDirective` ADT but the actual policy implementation is a static
heuristic until the learned version lands. Implementation work:

- First cut: static thresholds for the four directives (expand /
  commit / alternate / terminate). Tests against synthetic walks
  with known correct directives.
- Quality-signal collection: every `walk_completed` event carries
  the policy's per-step decisions and the eventual result quality
  (confirmed match / surfaced result / abandoned). DD view aggregates
  per-policy-decision quality.
- Learned version: a small per-user adjustment matrix over the
  static thresholds, updated from the quality DD view via the same
  online-learning shape as R3 §8.3 (cooldowns, caps, reversibility).

The first cut ships as the policy default; the learned version is
the second iteration and lands once the substrate has accumulated
enough walk-quality data (probably weeks-to-months of real use).

### Sequencing of the three

These three pieces interlock:

- Substrate-signal analysis (1) tells us what fields frames carry —
  which is what the search policy (3) reads to decide
  expand/commit/alternate.
- The voice stack (2) is one major source of new substrate signals
  that the analysis (1) needs to consider.
- (3) needs (1) to be done before its decisions can branch on typed
  frame fields rather than opaque dicts.

Sensible order: ship voice stack (2) first — it's largely additive
substrate work that doesn't block on other decisions. Then run
substrate-signal analysis (1) with voice signals included. Then
implement adaptive search policy (3) against the typed frames the
analysis produces.

Each is its own session, similar to how the DD wiring was 8 sub-slices.

## What I'd want you to confirm before any of this gets built (revised)

1. **Defer-the-taxonomy is the right approach?** v1 tried to pin the
   `InterpretiveFrame` shape; v2 pushes it to the substrate-signal
   analysis pass. Confirm.
2. **Voice rewrite shape?** Per-token features, segments first-class,
   per-feature confidences, model-pass provenance, segment-targeted
   memos. Confirm or push back.
3. **Sequencing?** Voice stack → substrate-signal analysis → adaptive
   search policy. Or do you want a different order?
4. **Cluster-pattern caching shape?** Default-distinct + auto-projection
   + cooldown-cap-audit. Same online-learning shape as R3 §8.3.
   Confirm.
5. **Search policy first cut as static heuristic?** Learned version
   ships later from accumulated walk-quality data. Acceptable, or do
   you want learned from the start?

This design is a sketch. The data shapes are reasoned from the
substrate; the protocol is conservative-extension over what's there.
Edits before any code.
