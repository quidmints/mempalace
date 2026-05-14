# MemPalace Architecture Specification

**Status:** Working draft consolidating the design decisions from the full thread.
**Scope:** Within-palace data substrate, retrieval, federation pathway (gigabox-mediated), composition layer, multiplexer.

---

## Summary of architectural commitments

Before the parts, these are the load-bearing decisions the document depends on. If any of these are wrong, much of the rest is wrong.

1. **Substrate is an append-only event log of graph deltas, not a relational or KV database.** Differential Dataflow (Rust) maintains incremental views. Python consumers (rankers, miners, ML, application logic) read from views and append events through a thin PyO3 boundary.
2. **Two distinct storage commitments coexist:** the master DAG (substrate-faithful, log-backed, source of truth) and derived representations (consumer-optimized, dataflow-maintained, invalidated by master events). Master is consumer-agnostic; derived representations are consumer-specific.
3. **Substrate and interpretation are different objects with different temporal semantics.** Substrate (what was captured) is immutable in the strong sense. Interpretation (what the system thinks about what was captured) is versioned, pass-attributed, supersedable.
4. **Assertions, not triples.** The 8-part frame: `(subject, predicate, object, valid_from, valid_to, recorded_at, invalidated_at, derivation)`. Multi-source provenance via `derived_from` edges. Confidence is a cached scalar over the source distribution, recomputed when sources change.
5. **Identical content is a glitch.** Content-hash collision raises an event for upstream resolution; storage does not silently dedup. Recurrence (high similarity below identity) is a first-class signal via recurrence clusters.
6. **Drawers are facet bundles.** Seven facets: verbatim, acoustic, semantic-embedding, paralinguistic, structural, interactional, state-context. Each independently indexed; capture-time fidelity matters because re-extraction from audio is expensive and lossy.
7. **Features are persisted; rankers are swappable functions over features.** No hardcoded scoring formula. The retrieval pipeline is staged (coarse retrieval → feature gather → re-rank → resolution) and the ranker at stage 3 is pluggable, stance-conditional, and consumer-specific.
8. **Federation matching is layered triangulation, not a single operation.** Layer 1 structural, layer 2 derivation-chain (KisMATH-style), layer 3 substrate-level. Privacy gates between layers; drawer exposure is proportional to prior-stage match probability.
9. **Heat and canon are bidirectional flux, not a ratchet.** Promotion is a sustained-state regime with hysteresis. Canon nodes participate in heat dynamics; sustained loss of grounding falls them back to ephemeral.
10. **The daemon is a multiplexer.** Many concurrent jobs (miners, rankers, retrievers, signature extractors, sandbox workers) over the same log. Snapshot consistency per consumer; no contention. Job state is itself in the log.
11. **No V1/V2 mindset.** Architecture supports trainable embedding models, learned rankers, derived representations, and federation matching from day one. First implementations of each may be simple; the *interfaces* are the durable commitment.

---

## Part 1 — Event taxonomy

The log is the source of truth. Every change to the palace is an event appended to the log. Reading the palace at any point in time is replaying events up to that point (with snapshot acceleration; see Part 2).

Events fall into three top-level classes, distinguished by tag and by their replay semantics.

### 1.1 Substrate events

Immutable in the strong sense. Record what was captured from the world. Cannot be invalidated; can only be superseded by a new substrate event that points at a different blob.

- `drawer_captured` — a voice memo, transcript, or other artifact entered the system. Carries: `drawer_id`, `content_hash`, `recorded_at` (capture wall-time), `source_uri`, `duration_ms`, `interactional` (memo-to-self / dictation / conversation / overheard / audio-letter), `state_context` (sleep, stress, location-cluster, prior-activity tag), `goal_markers` (currently-active sub-goal markers from upstream), facet pointers (acoustic feature blob, semantic embedding blob, paralinguistic feature blob).
- `drawer_amended` — same drawer_id, new content_hash, with a reason (re-transcription, audio re-encoded). Original substrate event preserved; new event supersedes for current-state queries.
- `drawer_hash_collision` — raised when a `drawer_captured` event presents a content_hash that already exists. The event records both drawer_ids and the collision context. Resolution (keep, replace, merge with note) is a downstream user/system action that emits its own event.

### 1.2 Interpretation events

Versioned, pass-attributed, supersedable. Record what the system thinks about substrate.

- `node_created` — creates a non-drawer node (theme, period, event, entity, schema, assertion). Carries kind, properties (per-kind-validated JSON), `recorded_at`, `created_by` (capture / agent / miner-pass-version / user).
- `node_property_set` — supersedes a property's prior value. Includes `supersedes` pointer if applicable.
- `edge_created` — creates an edge with kind, source, target, valid_from, valid_to, weight, confidence, derivation, properties.
- `edge_invalidated` — sets `invalidated_at` on an edge. Mutation is forbidden; this is the only way to "remove" an edge.
- `interpretation_assigned` — assigns an interpretive field (memory_type, importance, confidence) to a node, with pass-attribution and supersedes-pointer to the prior interpretation. This is the backbone of the substrate/interpretation split.
- `schema_induced` — emits a new schema-version node with derivation pointers to the events/assertions/drawers that supported the induction. Schema versions are linked by `supersedes` edges so the history is queryable.
- `recurrence_cluster_member` — adds a drawer to a recurrence cluster (or creates the cluster).
- `contradiction_asserted` — emits a `contradicts` edge between two assertions plus a reference to what triggered the detection.
- `contradiction_resolved` — annotates an existing contradicts edge with a resolution state (outweighed, justified, closed, superseded) and timestamp.

### 1.3 Job-state events

Job orchestration lives in the same log. This makes resumability and audit trivial.

- `job_scheduled`, `job_started`, `job_progress`, `job_completed`, `job_failed`, `job_paused`, `job_resumed` — orchestration. Carries `job_id`, `job_kind`, `consumer`, parameters.
- `view_offset_advanced` — a consumer's dataflow view advanced to a new log offset. Used for backpressure detection and resumption.
- `feedback_recorded` — downstream feedback (montage candidate kept/discarded, draft shipped/abandoned, match outcome). Tagged with the consumer that emitted the feedback and the interpretation versions that were active when the relevant artifact was produced. This is what makes credit assignment to the right interpretation generation possible.

### 1.4 Federation events

Sandbox lifecycle and finding emission. Most details in Part 9; events listed here for completeness.

- `match_request_received`, `sandbox_provisioned`, `foreign_palace_loaded` (with cleartext-decryption-inside-sandbox marker), `match_layer_completed` (per layer 1/2/3), `finding_emitted` (to switchboard), `sandbox_torn_down`.

### 1.5 Per-event schema discipline

Each event kind has a strict JSON schema validated at append time. Append failures (schema mismatch, FK violation against current view, etc.) are themselves logged as `append_rejected` events with the offending payload. This keeps the log self-describing and never silently corrupted.

---

## Part 2 — Log + DDflow substrate

### 2.1 The log

Append-only, durable, ordered. Each event has a monotonic offset (`log_offset`). The log is the only durable storage of master state. Everything else — node tables, edge tables, derived representations — is a *materialized view* over the log, reconstructible by replay.

Implementation: Rust, embedded, file-backed with fsync discipline for durability. Compaction policy in 2.3.

### 2.2 Differential Dataflow views

DDflow expresses queries and views as dataflow operators. When the input log advances, output views update incrementally with cost proportional to the change, not the full graph. This is what makes the architecture viable at the million-node scale.

Master views maintained at all times:

- **`current_nodes`** — for each node_id, the latest non-invalidated state (kind, properties, canonical, heat, importance, etc.).
- **`current_edges`** — for each edge_id, the latest non-invalidated state.
- **`current_interpretations`** — for each (drawer_id, field), the latest non-superseded interpretation value, with pass-attribution.
- **`current_schemas`** — for each schema, its latest induced version with stability and coverage scores.
- **`heat_field`** — for each node, current heat (continuous, updated by access events with hysteresis).
- **`velocity_field`** — for each node and theme/period/schema aggregate, velocity over rolling windows (7-day, 30-day, 90-day).
- **`recurrence_clusters`** — drawers grouped by substantive similarity, with cluster representatives.
- **`active_periods`** — periods where state ∈ {open, recently_closed} with precedence ordering.
- **`active_iams`** — for the self-entity, the role-edges currently active (in open or recent periods).
- **`open_contradictions`** — contradicts edges with no resolution event.
- **`canon_set`** — nodes with `canonical=true`, with their canon_path and structural-leverage scores.
- **`pending_proposals`** — miner outputs marked provisional and awaiting confirmation.

Each view is defined once as a dataflow program; DDflow handles incremental update.

### 2.3 Compaction and snapshots

Replaying the entire log from origin gets prohibitive. Periodic snapshots checkpoint the materialized views at a log offset. New replays start from snapshot + tail-replay rather than origin.

Snapshot policy:
- Time-based: daily snapshot at low-activity hour.
- Size-based: when log has grown N events since last snapshot.
- Event-based: after major schema-induction passes (Class 3) so post-induction state is checkpointed.

Snapshots are themselves stored as immutable artifacts indexed by log_offset. Restore is "load snapshot at offset K, replay events K+1..current."

Compaction does not delete events. The full log is preserved for audit, time-travel queries, and bitemporal correction. Snapshots accelerate replay; they don't replace the log.

### 2.4 The Rust/Python boundary

DDflow lives in Rust. Python consumers cross via PyO3.

Python side:
- All ML inference (rankers, signature extraction, miner LLM calls, embedding models)
- Application logic (MCP tool handlers, multiplexer scheduling, sandbox orchestration)
- Most consumer code

Rust side:
- Log append, durability, snapshot/compaction
- DDflow view maintenance
- Hot-path query execution against views

The boundary is at the view-read and event-append API. Python calls `view.query(...)` and `log.append(event)`. PyO3 marshals the data; the cost is real but bounded for our access patterns (mostly batch reads, batch appends, not per-edge round trips).

### 2.5 Existing MemPalace components — kept, replaced, or rewritten

- `palace.py` (collection access, closet building, mining locks): **replaced**. The collection abstraction goes away; locks are unnecessary because appends are serialized at the log layer; closet building moves into a derived representation.
- `palace_graph.py`, `knowledge_graph.py` (SQLite KG operations): **replaced**. All KG operations become DDflow view reads or log appends.
- `mcp_server.py` tool handlers: **rewritten**. Tool signatures mostly preserved for compatibility; bodies route through views instead of SQL.
- `miner.py`, `convo_miner.py`, `general_extractor.py`: **rewritten**. Miners append interpretation events instead of writing SQLite rows directly. Pattern-extraction logic is largely reusable; the storage interface changes.
- `searcher.py`, `layers.py`: **rewritten**. Retrieval pipeline becomes the staged pipeline in Part 6. `MemoryStack.wake_up()` becomes the wake-up composer (Part 6.5).
- `embedding.py`, ChromaDB integration: **mostly preserved**. ChromaDB stays as the embedding store; its role is narrowed (Part 4). The default embedding function is replaced by a trained model (Part 7).
- Hooks (`hooks/`, `.claude-plugin/hooks/`): **lightly modified**. Hook scripts now call into the multiplexer's job-submission API instead of running mining as a subprocess.

---

## Part 3 — Master views (the structural model)

This part specifies the node and edge kinds that compose the master DAG, with strict per-kind property schemas.

### 3.1 Node kinds

**`theme`** (formerly "wing")
- Properties: `name` (str, required), `parent_theme` (theme_id, optional), `importance` (float, 0–1), `color` (str, optional, for UI).
- Themes nest. The root themes partition the palace; sub-themes refine.

**`period`**
- Properties: `theme_id` (required), `name` (str, required), `started_at` (timestamp, required), `ended_at` (timestamp, nullable for open), `state` (`open` | `closed` | `sealed`), `summary` (str, gist for retrieval), `precedence` (int, for overlap resolution), `characteristic_set` (cached aggregate from miner: defining-people, defining-locations, defining-activities — IDs only, not embedded text).
- Periods can overlap on the same theme. Higher precedence wins for goal-context inheritance.
- Sealing is one-way; closed periods can re-open by appending a state-change event, but sealed cannot.

**`event`**
- Properties: `period_id` (required), `name`, `gist`, `occurred_at` (point or range start), `occurred_to` (range end, nullable), `importance`.
- Events aggregate drawers (via `contains` edges to drawer_refs). They are agent-asserted at write-time when the agent has high confidence; otherwise miner-built in the Class 2 pass.

**`entity`**
- Properties: `name`, `entity_type` (`person` | `place` | `object` | `sui_generis`, optional — sui_generis allows particulars without forced typing), `attributes` (kind-specific JSON).
- The **self-entity** is a designated entity node (kind=entity, special-cased in API as `kg.self()`). I-am bindings are role-edges from self-entity.
- Aliases handled via separate `entity_aliases` table (Part 3.5) for indexed lookup.

**`schema`** — the conceptual-self layer
- Properties: `name`, `schema_kind` (`trait` | `relational` | `possible_self` | `self_guide` | `value`), `description`, `canonical` (bool), `canon_path` (str, nullable), `stability_score`, `coverage_score`, `induction_version`.
- Versioned: each Class 3 induction emits a new schema node; old versions linked by `supersedes` edges. Querying a schema at a historical timestamp replays to the version active then.

**`assertion`** (formerly "triple")
- Properties: `predicate` (str, the relationship type, canonicalized), `predicate_surface` (str, the original surface form), `confidence` (cached scalar from source distribution).
- Connected to subject and object via `asserted_subject` and `asserted_object` edges.
- Bitemporal validity carried on those edges (see edge kinds).
- Multi-source provenance via `derived_from` edges to drawer_refs (with weights and derivation types).

**`drawer_ref`** — the indexed projection of a drawer's structural facet
- Properties: `drawer_id` (the substrate identity), `content_hash`, `recorded_at`, `duration_ms`, `interactional`, `state_context` (JSON), `goal_markers` (list, references to goal-target nodes).
- Other facets (acoustic, semantic, paralinguistic) live in feature stores keyed by drawer_id; see Part 5 for the facet bundle.

**`recurrence_cluster`**
- Properties: `name` (auto-generated from cluster representative), `representative_embedding` (vector ref), `member_count`, `first_seen`, `last_seen`.
- Members linked via `member_of` edges from drawer_refs. Linear in cluster size, not quadratic in pairwise relations.

### 3.2 Edge kinds

Hierarchy and structural composition:
- `contains` — theme→period, period→event, event→drawer_ref. Tree-of-containment, but a drawer_ref can be contained in multiple events (e.g., overlapping periods).
- `participates_in` — entity→event. Role string in properties.
- `located_at` — event→entity (place).
- `succeeds`, `precedes` — period→period, event→event. Temporal sequencing.

Conceptual layer:
- `instantiates` — event→schema, assertion→schema. "This event is an instance of this trait."
- `refines` — schema→schema, assertion→assertion. New supersedes old (with both preserved).
- `supersedes` — generic supersession edge for versioning (schemas, interpretations).

Goals (as edges, not particulars):
- `pursues` — period→entity_or_schema, self_entity→entity_or_schema. The self pursues this state.
- `aimed_at` — event→entity_or_schema. This event was aimed at this outcome.
- `blocks` — pursues-edge → pursues-edge (between conflicting pursuits).
- `enables` — between sub-goal achievements and parent-goal progression.

Provenance and inference:
- `derived_from` — assertion→drawer_ref, schema→event, schema→assertion. Multi-source provenance. Carries `derivation` type (OBSERVATION | INFERENCE | ASSUMPTION | CANONICAL) and a weight.
- `asserted_subject`, `asserted_object` — assertion→entity_or_schema. With validity bounds.

Tension and resolution:
- `contradicts` — assertion→assertion. Symmetric in semantics.
- `supports` — drawer_ref→assertion, schema→schema, assertion→assertion. Positive evidence.

Identity and association:
- `same_as` — node→node (same kind). Strong identity. Reserved for cases like alias resolution; not used for content dedup (which is a hash-collision event, not an edge).
- `recurrence_with` — drawer_ref→recurrence_cluster (member). Aggregated, not pairwise.

Inhibition:
- `inhibits` — context_node → target_node. Suppresses retrieval of target under that context. Distinct mechanism from decay; contextual rather than global. The context is typically a stance-vector value or a period.

Self-entity bindings:
- `role_in_period` — self_entity → entity_or_schema. The active role in this period. Carries role string and validity bounds. Powers I-am queries.

### 3.3 Bitemporal edges

Every edge with semantic content (not just structural FK) carries:
- `valid_from`, `valid_to` — when the edge is true in the world.
- `recorded_at` — when the system learned it.
- `invalidated_at` — when the system unlearned it (mutation forbidden; only append `edge_invalidated` events).

Two query stances:
- "What's true now (or at world-time T)?" → filter by valid window.
- "What did the system believe at system-time T?" → filter by recorded/invalidated window.

These give correct answers to both "what was Kai's role in March 2024" (world time) and "what did the system think Kai's role in March 2024 was, as of October 2024" (belief time).

For most assertions, the temporal-overlap check is *not* a hard precondition for triangulation — it's only enforced when query stance demands temporal-sensitivity. See Part 9.

### 3.4 Forbidden patterns (validated at append time)

- An `asserted_subject` edge whose source is not an `assertion` node.
- A `pursues` edge with no target (goals are edges to states, not standalone).
- A `canonical=true` node without a `canon_path`.
- A `same_as` edge between nodes of different kinds.
- An `edge_invalidated` event for an edge that's already invalidated.
- A `node_created` event for an id that already exists (use amend events).

### 3.5 Indexed projections

A few small auxiliary tables exist for indexing where pure DAG walks are too slow:

```
entity_aliases (entity_id, alias, confidence) — for resolution lookup
content_hashes (drawer_id, content_hash, first_seen) — for collision detection
canon_files (canon_path, schema_id, last_rendered) — for FOYER renderer
```

These are derived from the master views and reconstructible from the log.

---

## Part 4 — ChromaDB integration (narrowed role)

ChromaDB stays. Its role narrows.

### 4.1 What ChromaDB does

- Stores the **trained embedding** for each drawer's verbatim content. (Default ChromaDB embedding function is *replaced* by the locally-trained model. See Part 7.)
- Provides fast approximate-nearest-neighbor (HNSW) for embedding similarity queries.
- Serves as the "coarse retrieval" stage of the staged retrieval pipeline (Part 6).

### 4.2 What ChromaDB no longer does

- It is not "the search engine." Final ranking happens in the ranker layer with many features, of which embedding cosine is one input.
- It does not store closets. The regex-extracted topic-pointer-line "closets" of current MemPalace go away; their function (cheap topic-level retrieval) is subsumed by the structural facet of drawer_refs and by event-level views.
- It does not store interpretations or any non-substrate metadata.

### 4.3 Coordination with the log

When a `drawer_captured` event is appended to the log, the embedding-write to ChromaDB happens as part of the same logical operation. Failure modes (log-appended, ChromaDB-write-failed) are handled via reconciliation: a periodic sweeper compares log offsets to ChromaDB collection counts and re-emits embedding writes for any drawers missing from ChromaDB.

The embedding itself is treated as a **derived representation** at the substrate level — derivable from the verbatim content + the embedding model version. Re-embedding the entire collection when the model changes is a known operation (expensive, but bounded).

---

## Part 5 — Drawer facet bundle and the resonance constraint

A drawer is not a content blob; it's a bundle of facets that consumers slice along.

### 5.1 The seven facets

1. **Verbatim text** — transcript. The textual substrate.
2. **Acoustic** — prosodic features supporting phonetic-rhyme matching: pitch contour samples, syllable-level rhythm, formant trajectories. Captured at write time; expensive to re-extract from raw audio later.
3. **Semantic embedding** — the trained-embedding-model vector. The geometric face (Part 7).
4. **Paralinguistic** — affect, inflection, mood, energy, valence/arousal trajectories. Smaller-dimensional than acoustic, more interpretable, separately queryable.
5. **Structural** — KG-attached metadata: `period_id` (multiple, for overlap), `event_id` (where applicable), participants, `interactional`, `state_context`, `goal_markers`.
6. **Interactional** — `memo-to-self` | `dictation` | `conversation-recording` | `overheard` | `audio-letter`. Enum; indexed; changes what other facets *mean*.
7. **State context** — sleep state, stress, time-of-day, location-cluster, prior-activity tag. Captured at write or it cannot be reconstructed.

### 5.2 The resonance constraint

The original push from your geology metaphor was that the data model has to expose all the surfaces a composer might want to slice along — without proliferating fields to the point of analysis paralysis.

Seven facets is the chosen ceiling for now, justified by:

- Each facet *changes the meaning* of cross-drawer joins. Acoustic rhyme between two memos means one thing under interactional=memo-to-self and a different thing under interactional=conversation-recording. State-context aligns differently than period-context.
- Each facet has *independent extraction cost*. Re-extracting paralinguistic features from audio is expensive; re-extracting state-context is impossible. They can't be deferred.
- Each facet is *separately consumable*. A montage tool wants acoustic + paralinguistic + interactional. A matching kernel wants semantic + paralinguistic + signature. A retrieval consumer wants verbatim + structural. Splitting them lets each consumer pull only what it needs.

Sub-facet detail (e.g., specific paralinguistic features, specific state dimensions) lives within the facet's JSON properties. Adding more *facets* requires architectural change; adding more *fields within a facet* does not.

### 5.3 What's not a facet

- *Self-other-world axis* (Conway's three-way split) is a structured field on the **interactional** facet, not its own facet. The interactional kind already discriminates these cases.
- *Visual* (image/video) is not a facet because the current capture surface is voice-only. If visual capture is added later, it becomes the eighth facet at that point.
- *Goal-state-at-capture* is part of the structural facet (`goal_markers`), inherited from the period's then-active goals, refined by miner.

### 5.4 Composition substrate — the transition cache

Tetris-stacking memories without breaking the chain of reasoning needs more than per-drawer features. It needs ordered-pair coherence — *can these two be sequenced*.

Three approaches considered:
- Compute on demand (slow, flexible)
- Cache pairwise (memoize)
- Train a model to predict (eventual goal)

The committed v1: a `transition_cache` derived representation (Part 7) keyed on `(source_drawer, target_drawer, coherence_kind)` with a score and a timestamp. Coherence kinds: `semantic`, `acoustic`, `paralinguistic`, `structural`, `conceptual_rhyme`, `phonetic_rhyme`. The composer queries the cache when planning a sequence; cache misses fall back to live computation.

The cache is populated lazily by the composer's actual queries (real workload generates the cache) and refreshed in background passes for hot regions.

This is the answer to "have we solved tetris-stacking?" — the storage exposes the surfaces (facets), the derived representation exposes pair coherence on demand, and the composer chooses which kinds of coherence to emphasize per task. The model didn't need built-in coherence-prediction; it needed the substrate for an evolving cache plus a learnable function on top.

---

## Part 6 — Feature catalog and the staged retrieval pipeline

### 6.1 Features as persisted state

Every signal a ranker might consume is computed by a master view (or a derived representation) and stored. Rankers consume features; they do not compute them at query time.

Feature catalog (per node, where applicable):

- **Heat** — continuous score, updated by access events with hysteresis. Scaled to [0,1] for normalization; underlying integration is unbounded.
- **Per-dimension pull** — a vector of per-edge-kind connectivity weights. `provenance_pull` (inbound `derived_from`), `abstraction_pull` (inbound `instantiates`), `structural_pull` (inbound `contains`), `relational_pull` (inbound `asserted_subject` / `asserted_object`), `tension_pull` (inbound `contradicts`).
- **Velocity** — multi-window (7d, 30d, 90d) rate of change in access frequency.
- **Recurrence-cluster membership** — cluster_id and within-cluster representativeness score.
- **Canonical status** — bool plus structural-leverage score (how much downstream depends on it).
- **Stance-affinity vectors** — per-node, how aligned the node is with each stance dimension. Computed by miner from properties + edges.
- **Confidence** — for assertions, the cached scalar over derived_from distribution.
- **Bitemporal validity flags** — fast precomputed flags for "valid now" and "valid as of T" for common T values.
- **Schema-version pointers** — current version; supersession chain.

### 6.2 Stance vectors

The query carries a stance specifying the cognitive task and the relevant biases. Stance dimensions include:

- `correspondence_vs_coherence` — fact-finding vs. self-understanding.
- `temporal_sensitivity` — does the query care about valid-window overlap?
- `contradiction_weight` — how much to amplify contradictions vs. confirmations.
- `recency_bias` — how strongly to weight velocity vs. heat-integrated.
- `canonicality_floor` — should canonical content dominate, or compete equally with ephemeral?
- `consumer_kind` — claude-thread / montage / matching / FOYER / agent-with-tag.

Different rankers may expose different stance dimensions. The framework is open — stances are JSON, ranker reads what it needs.

### 6.3 The four-level fidelity ladder

When a handle is resolved, the consumer specifies fidelity per facet:

- **Traversal** — local graph topology only. Connected node IDs, edge kinds, distances. No content. Cheapest. (Added in the DAG-native pivot.)
- **Meta** — node fields about a single node. Kind, name, properties, indexed values. ~30 tokens.
- **Summary** — synthesized gist for the node, possibly facet-specific. For drawers, a paraphrase. For events, the gist. For schemas, the description. ~200 tokens.
- **Full** — verbatim content at the requested facet. Audio reference, transcript, full feature vector. Variable size.

Fidelity is per-facet. A handle can resolve verbatim at `full` and acoustic at `meta` — a montage tool that already has acoustic features cached but needs the transcript fresh.

### 6.4 The staged pipeline

```
Stage 1 — Coarse retrieval
  Candidate-set generation. Default: ANN over embeddings (ChromaDB).
  Alternative: traversal from seed nodes, LSH over signature basis,
  derived-representation lookup.
  Returns ~100–500 candidates.

Stage 2 — Feature gather
  For each candidate, pull feature vector from views.
  Batched DDflow read; scales with candidate count, not graph size.

Stage 3 — Re-rank
  Active ranker computes scores from (query, candidate, context, features).
  Stance-conditional dispatch: which ranker handles this stance?
  Returns ordered top-K.

Stage 4 — Resolution
  Materialize top-K at requested per-facet fidelity.
  Returns the handle's payload to the consumer.
```

### 6.5 Wake-up as composer

Wake-up is a structured composer over master views, not a single retrieval. It assembles:

- Identity (canonical schemas / CRESTS in the FOYER, rendered from `canon_files`)
- Currently-active periods with goal-sets, precedence-ordered (from `active_periods`)
- Currently-active I-ams (from `active_iams` over self_entity)
- Recent high-velocity nodes (from `velocity_field`, top-K)
- Open contradictions awaiting resolution (from `open_contradictions`)
- Pending miner proposals from previous session (from `pending_proposals`)

Each component has a fidelity setting; the composer assembles a token-budgeted result. Cheap wake-up is identity + active periods (~600 tokens). Rich wake-up is everything (~3000 tokens). The MCP `mempalace_wake_up` tool exposes this with parameters for budget and component selection.

---

## Part 7 — Ranker ecosystem and trained representations

### 7.1 Ranker protocol

A ranker is a function:

```
rank(query, candidates, context, feature_matrix) → scored_results
```

Where:
- `query` is the original query text or structured query.
- `candidates` is the candidate set from coarse retrieval.
- `context` includes the stance vector, consumer kind, current view offset.
- `feature_matrix` is the dense feature vector per candidate.

The output is an ordered list with scores and rationales.

Rankers are registered with the multiplexer; they declare which stance dimensions they handle and which features they consume. Stance-conditional dispatch routes queries to the right ranker.

### 7.2 The ranker stack

Multiple rankers can be in flight simultaneously, for different consumers and stances:

- **Cosine-only** — cold start, falls back to ChromaDB cosine when no other features are available.
- **Factored multiplicative** — `similarity × decay × ltp × tagging × ...` with learned per-axis weights. Cheap, interpretable, trains on retrieval-utility signal.
- **Neural with cross-terms** — a small neural net (or attention over feature vectors). Captures interactions. Trains on richer signal as it accumulates.
- **Stance-conditional dispatch** — a meta-ranker that selects which underlying ranker to use per stance.
- **Composer-specific rankers** — montage rankers care about transition coherence; matching rankers care about geometric alignment; market rankers care about prediction-relevant signals.

Rankers can compose. A first-pass cosine ranker produces a candidate set; a second-pass neural ranker re-ranks the top-N; a third-pass composition-specific ranker re-orders for sequencing. This stacking is *not* winner-takes-all — each ranker's output can feed the next, and the stack itself is configurable per consumer.

### 7.3 The trained embedding model

ChromaDB's default embedding function is replaced. The embedding model is *locally trained against downstream signals* — montage retention, draft completion, prediction-market resolution. It is not a fixed pretrained encoder.

The training story:

- **Cold start**: a frontier-class embedding (e.g., a public model) bootstraps the system in the first month. Provides reasonable embeddings while feedback accumulates.
- **Local fine-tuning**: as feedback events accumulate (montage kept/discarded, draft shipped/abandoned, match outcomes), the embedding model fine-tunes on this signal. Embeddings drift toward what the user's actual workload finds useful.
- **No global baseline required**: there is no pretrained population manifold the system depends on. The "discriminative basis" emerges from training, not from PCA against a fixed prior. The unusual axes are whatever the trained model has learned to make discriminative because they predicted matching success — a richer signal than statistical outlierness.

### 7.4 Discriminative basis and signature extraction

The signature is not extracted by classical PCA over a fixed embedding space. It emerges from the trained model's learned representation.

Signature components:

- **Mean position** in the embedding space across recent drawers (per theme, per period). The "where this person is" coordinate.
- **Deviation vector** — how the user's mean position differs from the local population's mean (population = whatever the federation has accumulated, optional and possibly empty in cold start).
- **Covariance structure** — how the dimensions co-vary within the user across sessions. This is critical and was missing from earlier framings: it's not enough to know where each dimension sits; we need how dimensions move together.
- **Velocity field** — per-theme velocity over recent windows. The phase-coupling primitive for federation.
- **Schema fingerprint** — the canonical schemas, projected at canonical-projection level.
- **Contradiction-resolution profile** — statistical features over how the user resolves contradictions (outweighing / justification / closure / supersession ratios).

The signature is a **derived representation** (Part 8). It's not stored in the master DAG; it's computed by a dataflow consumer that subscribes to relevant master events and updates incrementally.

### 7.5 Population baseline — deemphasized

The baseline is one variable, not a load-bearing precondition. Deviation against population is *one signal among many*; the model's training signal (downstream prediction-market resolution, montage success, etc.) is the primary thing the embedding model learns from. If the federation later accumulates an empirical population manifold and that improves matching, fine. If it doesn't, the system still works.

This is consistent with "no V1/V2 mindset": the architecture supports population baseline as an optional signal from the start; whether to use it is a tunable, not a structural commitment.

---

## Part 8 — Derived representations and the registry

### 8.1 The pattern

Master DAG is the source of truth. Derived representations are functions of master that are optimized for specific consumers.

Each derived representation has:

- **Name and owner** — which consumer.
- **Derivation function** — a dataflow program subscribed to relevant master events.
- **Storage backing** — where the materialized state lives (in-memory, on-disk, specialized index).
- **Invalidation policy** — what master changes affect this representation; incremental update or full rebuild.

### 8.2 Specific derived representations

**Signature representation** (Part 7.4)
- Subscribes to: drawer_captured, schema_induced, contradiction_resolved, velocity-field updates.
- Storage: in-memory tensors + on-disk snapshot; LSH index over discriminative basis on disk.
- Invalidation: incremental on per-drawer additions; rebuild on embedding-model version change.

**Montage transition cache** (Part 5.4)
- Subscribes to: drawer_captured, recurrence_cluster events, structural-edge changes.
- Storage: B-tree keyed on `(source_drawer, target_drawer, coherence_kind)`.
- Invalidation: per-drawer when that drawer or its neighbors change.

**Real-time retrieval index**
- Subscribes to: heat-field, velocity-field, canon_set updates.
- Storage: pre-warmed handle cache for likely queries.
- Invalidation: TTL-based (warm queries decay) plus event-driven.

**FOYER renderer cache**
- Subscribes to: canon_set, canonical-schema updates.
- Storage: rendered canon files on disk + last-render timestamps.
- Invalidation: re-render whenever canon_set or any canonical-schema content changes.

**Match-resolution attribution**
- Subscribes to: feedback_recorded events tagged from match outcomes.
- Storage: append-only attribution chain mapping artifact components back to source drawers and interpretation versions.
- Invalidation: never (append-only by design — finalized artifacts don't change).

### 8.3 Hierarchy of representations and their interaction

Concretely, there are five distinct representations, and consumers typically draw from several:

```
1. Master DAG            — substrate-faithful, log-backed, versioned.
2. ChromaDB embeddings   — substrate-derived (verbatim → embedding).
3. Signature store       — schema/velocity/covariance/discriminative basis.
4. Transition cache      — pairwise coherence for composition.
5. Real-time indices     — wake-up, hot queries, FOYER pre-render.
```

Interaction map:

- **Real-time retrieval consumer** → coarse via ChromaDB → features from master → ranker → resolve facets from master + ChromaDB.
- **Montage composer** → seed via ChromaDB → coherence from transition cache → sequence; queries master for verbatim only when materializing the final montage.
- **Matching kernel (federation)** → signature comparison first (signature store) → derivation-chain comparison (master) → substrate comparison (master + ChromaDB), gated layer-by-layer.
- **FOYER renderer** → canon_set view + canonical-schema content from master → render to files; cache reads come from FOYER cache.
- **Wake-up** → reads multiple master views (Part 6.5) plus ChromaDB for embeddings of recent drawers if velocity-section needs them.

The representations form a fan-out: master is consumer-agnostic; each representation is optimized; consumers pull the combination they need.

---

## Part 9 — Federation pathway

### 9.1 Trust topology

Three trust zones:

- **User's palace** — full fidelity, lives where the user lives. Source of truth.
- **Gigabox isolation environment** — receives encrypted chunks of foreign palace, holds ephemerally, runs comparison, emits findings, destroys foreign data. User's palace is also accessible here for the comparison.
- **Switchboard** — receives only the findings (or transformed derivatives). Sees no palace content.

Findings are the privacy boundary: they're what's surfaced to switchboard. Sandbox is trusted; switchboard is not.

### 9.2 Layered triangulation

Matching is *not* a single operation. It's a layered protocol with explicit privacy gates between layers; drawer exposure is proportional to prior-stage match probability.

**Layer 1 — structural triangulation (breadth-first, drawer-blind)**
- Inputs: signature objects (derived representation, no episodic content), schema fingerprints (canonical-schema-only, user-consented to share).
- Operations: signature similarity in the discriminative basis subspace; schema overlap and structural alignment; covariance comparison; velocity-field coupling tensor.
- Output: a coarse compatibility score plus dimensions of promising alignment.
- Decision gate: if clearly negative, no further layers run. Sandbox tears down.

**Layer 2 — derivation-chain compatibility (depth probe, KisMATH-aware)**
- Triggered when layer 1 surfaces candidate matches.
- Inputs: assertion-level data with derivation chains. Drawer embeddings (not verbatim text yet) for path-level comparison.
- Operations: extract derivation graphs (KisMATH-style CCGraphs over assertion → drawer derivation paths). Compare derivation-graph structure across the two palaces' assertions. Are intermediate assertions in the chain compatible? Do supporting drawer embeddings cluster compatibly?
- Output: depth-compatibility score per candidate match.
- Decision gate: only candidates passing depth check go to layer 3.

**Layer 3 — substrate-level compatibility**
- Triggered for high-confidence candidates only.
- Inputs: verbatim drawer content, paralinguistic features, full geometric similarity.
- Operations: deep substrate comparison; full ranker stack as if intra-palace.
- Output: structured findings — match topology (peer / mentor-asymmetric / complementary / divergent / orthogonal), strength per dimension, dimensions of resonance.

Findings are the *only* output that crosses to switchboard. The full graph state of the foreign palace, the verbatim content, the per-layer scores — all destroyed when the sandbox tears down.

### 9.3 Sandbox lifecycle

1. **Match request received** — switchboard or another agent identifies a candidate pair.
2. **Sandbox provisioned** — isolated encrypted environment spun up.
3. **Foreign palace loaded** — encrypted chunks transferred via libp2p, decrypted inside sandbox boundary, materialized as a *temporary transformation* of the master DAG (graph-shaped, queryable by rankers, but not persisted).
4. **User's palace mounted** — read-only access from the user's machine to the sandbox for comparison.
5. **Matching runs through layers 1 → 2 → 3** with gates.
6. **Findings emitted** to switchboard.
7. **Sandbox torn down**, foreign palace state destroyed, user's palace unmounted.

The foreign palace gets loaded as a temporary transformation because rankers need queryable graph-and-feature state to function. It's not persisted; it's not given a "palace identity" beyond the sandbox lifetime.

### 9.4 Privacy story per tier

**Signature tier** is geometric. Discriminative directions, deviation vectors, covariance, velocity. Lossy projection of behavior into a low-dimensional space; hard to invert into specific drawer content. Lowest leakage; first to cross sandbox boundary.

**Schema tier** is the canonical-self layer. CRESTS the user has explicitly marked shareable. Cleartext. Medium leakage; user-consented.

**Derivation-chain tier** exposes derivation-graph structure plus drawer *embeddings* (not verbatim). Higher leakage; only invoked when prior tiers indicated a match.

**Substrate tier** is verbatim content + paralinguistic features. Highest leakage; only invoked when prior tiers strongly indicated a match. The privacy story for this tier is: *exposure is gated by prior-stage match probability*, and exposure happens inside an encrypted sandbox that is destroyed after matching, with only structured findings crossing to switchboard.

This is not a fully solved problem — substrate-tier exposure is real exposure even if temporary. The mitigations are: cryptographic isolation (libp2p-encrypted transport, OS-sandboxing of decryption, secret-sharing schemes for the encryption keys), audit (every layer-3 invocation is logged), and consent (the user opts in per-match to layer-3 exposure, not blanket).

The harder problem of paralinguistic-tier privacy is that audio-derived features can be more inverting than text. This is mitigated by aggregating paralinguistic features over windows (not per-utterance) before they cross to layer 3, and by feature-level differential privacy with calibrated noise. Real cryptographic engineering remains; the architecture supports it but doesn't fully specify it here.

### 9.5 libp2p transport

The transport layer is libp2p. Specifics (encryption schemes, secret-sharing, key rotation) are plumbing per your earlier framing and not specified in this document. The architectural commitment is:

- Foreign palace data crosses to gigabox encrypted, libp2p-routed.
- Decryption happens inside the sandbox boundary, not before.
- Sandbox teardown includes key destruction.
- Findings crossing to switchboard are signed but not encrypted (they're the privacy-respecting output).

### 9.6 Match outcomes feeding training

Match outcomes are feedback signals tagged with the interpretations active when the match was generated. The credit-assignment chain:

```
substrate drawers → miner outputs → matching candidates → 
  layer-1/2/3 progression → findings → switchboard → 
  market resolution outcome
```

Feedback flows backward through this chain. The feedback_recorded event carries the outcome, the artifact's component tree, and pointers to the interpretation versions that were active. The miner's training signal (Part 10) reads these events and updates extraction priorities.

Critically, the feedback applies to the *interpretation generation that was active when the artifact was produced*, not to the current generation if interpretations have been re-induced in between. This is what the substrate/interpretation versioning makes possible.

**Cross-period retrieval with state-context filters** is one example of how match outcomes shape training. A successful match might involve drawers that were captured under similar state-context (sleep, stress, activity) across different periods; the training signal weights state-context as a useful retrieval feature. An unsuccessful match might reveal that state-context was misweighted; the training signal corrects. The state-context dimension of the stance-vector and the state-context features on drawers were both designed to support this.

---

## Part 10 — Multiplexer and the miner

### 10.1 The multiplexer

The daemon orchestrates many concurrent jobs against the same palace. Voluntarily contributed compute, load balanced.

Concurrent jobs include:
- Class 1 streaming miner (continuous, near write time)
- Class 2 periodic miner (hourly / end-of-day / period-end)
- Class 3 schema-induction pass (daily, asynchronous)
- Signature extractor (incremental on master events)
- Montage transition cache builder (lazy + background refresh)
- Matching kernel (sandbox jobs)
- Real-time retrieval (per-query)
- FOYER renderer (on canon changes)
- Velocity computation (windowed batch)
- Reconciliation sweepers (ChromaDB consistency)

### 10.2 Concurrency story

- Many readers, many writers, no contention (log-append + dataflow views).
- Each consumer gets snapshot consistency at its current view offset.
- Job state is in the log (job_scheduled / started / progress / completed / failed events).
- Backpressure: consumers falling behind (view offset lagging log head) are detected automatically; multiplexer can allocate more compute, drop the consumer, or alert the operator.

### 10.3 Resource allocation

The multiplexer schedules jobs against available CPU, GPU, and I/O budgets. Priority order:

1. Real-time retrieval (interactive latency budget).
2. Class 1 miner (must keep up with capture rate or backlog grows).
3. Sandbox matching (when active, time-bounded).
4. FOYER and wake-up rendering.
5. Class 2 miner.
6. Signature extractor.
7. Class 3 schema induction.
8. Background reconciliation, transition-cache refresh.

Higher-priority jobs can preempt lower-priority ones; preempted jobs resume from their last view-offset checkpoint.

### 10.4 View eviction

In a long-running daemon with many consumers, materialized views accumulate. Inactive consumers' views are evicted from memory (their dataflow programs are paused; on reactivation, they replay from the last checkpoint).

### 10.5 The miner — three classes, three cadences, multi-loss

**Class 1 — drawer-level enrichment (streaming, near write-time).**
Refines memory_type, computes affect derivatives, resolves entities to KG entity IDs, classifies interactional and self-other-world, marks goal-state markers. Per-drawer; fast.

**Class 2 — cross-drawer aggregation (periodic).**
Detects event boundaries, asserts triples across drawers, surfaces contradictions, updates velocity, proposes period state transitions. Cross-drawer; expensive.

**Class 3 — schema induction (rare, asynchronous).**
Induces traits, relational schemas, self-with-other units, possible selves. Cross-event abstraction; most expensive; runs daily during downtime.

**Re-mining strategy.**
Schemas are *versioned*, not rewritten. Each Class 3 pass produces a snapshot. Comparing snapshots gives stable / drifting / broken classifications. Stable schemas don't need attention; drifting schemas surface as "your understanding refined"; broken schemas trigger split / replace / retire flow with user adjudication.

**Loss function (artifact-specific).**
The miner doesn't optimize a single loss. It optimizes a weighted combination of per-consumer losses:
- Retrieval-utility loss (weakest, available immediately) — does extracted structure improve retrieval on subsequent queries.
- User-confirmation loss (medium) — does the user accept proposals, edit them, or reject them.
- Downstream-feedback loss (strongest, accumulating) — propagated from montage retention / draft completion / match outcomes through the credit-assignment chain.

Different artifact types feed back at different cadences and through different mediation. Per-(miner-output, consumer-type) feedback ledger preserves the granularity. The miner trains against the weighted combination; weights shift as stronger signals accumulate.

**Cold start vs trained.**
Frontier-class LLM bootstraps Class 1 and Class 2. Local fine-tuning takes over as feedback accumulates. This is the same path the embedding model takes.

### 10.6 Provisional / confirmed / rejected lifecycle

Every miner output is provisional until confirmed (by user action, by sufficient corroboration, or by downstream feedback). Lifecycle states:

- `provisional` — initial state on miner emit.
- `confirmed` — user action or auto-promotion threshold met.
- `rejected` — user action or strong contradicting evidence; node is `inhibited`, not deleted.

Rejected outputs persist in the log as feedback for miner training. They don't surface in retrieval (inhibition edges suppress them under all stances).

---

## Part 11 — MCP layer and migration

### 11.1 MCP tool surface

Existing 19 tools (see analysis of `mcp_server.py` from the codebase walk) are mostly preserved in *signature*; bodies are rewritten to route through views.

New or expanded tools:

- `mempalace_handle_allocate(query, scope, stance, facets, fidelity)` — RLM handle protocol. Returns a handle ID + preview.
- `mempalace_handle_resolve(handle_id, fidelity_overrides)` — materialize.
- `mempalace_assert(subject, predicate, object, derived_from, valid_from, valid_to, derivation)` — assertion writes (the renamed triple write).
- `mempalace_promote_to_canon(node_id, canon_path)` — promotion.
- `mempalace_canon_amend(node_id, ..., force=True)` — gated edit.
- `mempalace_iams()` — current self-entity I-am role-set.
- `mempalace_period_open(theme_id, name, goal_set)` and `period_close`, `period_seal`.
- `mempalace_event_assert(period_id, name, ...)` — agent-asserted events.
- `mempalace_velocity(scope)` — velocity field readout.
- `mempalace_signature(level)` — signature export at requested projection level.
- `mempalace_match_request(target_palace_id, level)` — initiate matching via gigabox.
- `mempalace_findings(match_id)` — retrieve findings post-match.

Old tools route to new internals where the operation maps cleanly; deprecate where it doesn't.

### 11.2 Migration from current MemPalace

A one-shot offline converter:

1. Read current SQLite KG (`entities`, `triples`, `attributes`).
2. Read current ChromaDB (drawers, closets, metadata).
3. Synthesize log events:
   - Each entity row → `node_created` event with kind=entity.
   - Each triple → `node_created` event for the assertion node, plus `edge_created` events for `asserted_subject` / `asserted_object` / `derived_from` (best-effort from `source_closet` / `source_file`).
   - Each attribute → either an assertion or a node property (heuristic).
   - Each drawer → `drawer_captured` event with a synthesized source_uri and the existing wing/room as initial structural context.
   - Wing strings → `theme` nodes; room strings → events under an implicit "legacy" period per theme.
4. Replay log to materialize new master views.
5. Validate against an invariant suite (every drawer has a containing event; every assertion has at least one `derived_from`; etc.).
6. Swap in the new daemon.

Existing palaces are backed up before conversion. Re-conversion is idempotent against the same input.

Closets are dropped (their function is subsumed). The `mempalace_closets` collection is not migrated.

### 11.3 Compatibility window

For the first N weeks after migration, the new daemon supports a compatibility shim that exposes the old MCP tool surface with deprecation warnings. After the window closes, only the new surface is supported. Old plugins / hooks that haven't been updated emit clear errors with migration guidance.

---

## Closing notes

This document is a working draft consolidating the design discussion. Several areas were named as deferred or out-of-scope (prediction-market mechanics, switchboard internals, libp2p-specific cryptography, paralinguistic-tier privacy engineering). Those need their own documents.

The architecture commits to: log-and-dataflow substrate, master DAG with strict typed kinds, substrate/interpretation split with versioning, assertions with the 8-part frame, drawers as 7-facet bundles, features-as-storage with swappable rankers, layered federation triangulation, the gigabox sandbox model, the multiplexer for concurrent jobs, the three-class miner with multi-loss training, and the new MCP tool surface.

What remains, in priority order for implementation:

1. The log substrate and DDflow boundary in Rust.
2. The master view definitions and their dataflow programs.
3. The Python event-append and view-read API via PyO3.
4. The MCP tool handlers (rewritten).
5. The miner classes (Class 1 streaming first).
6. The signature representation.
7. The transition cache.
8. The matching kernel and sandbox.
9. The training feedback loops.
10. The migration converter.

Order is approximate; some items can run in parallel.
