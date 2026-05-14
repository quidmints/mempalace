# MemPalace — Design Glossary & Architecture Reference

A reference for the v5 greenfield architecture. Documents the
core concepts, the architectural shape, where we deviated from
the original `mempalace-develop` codebase, and why.

---

## 1. Overview

MemPalace is a substrate for personalized long-term memory. It
captures lived experience (drawers), structures it into an
assertion graph, and supports retrieval that respects how human
memory actually works — generative, scope-shaped, stance-aware.

The v5 architecture is a ground-up rebuild. The original
`mempalace-develop` (v3.1 / v4.0-alpha) treated memory as a
ChromaDB vector store with a SQLite knowledge graph alongside.
The greenfield treats memory as an **append-only event log** with
**derived views** materialized on top. ChromaDB still exists below
the embedding boundary, but it's no longer the source of truth —
it's one of two substrates for retrieval, alongside the assertion
DAG.

## 2. Architectural shape

```
ENTRY POINT
    mempalace/__init__.py  →  exports Palace
    mempalace/palace.py    →  Palace facade

CONSUMER VERBS                 (palace.capture, palace.search,
                                 palace.assert_, palace.tick,
                                 palace.temporal_query, ...)

CONSUMER SURFACES              (drawer, retrieve, rank, derived,
                                 features, handle, stack, miner,
                                 resolve, federate, signatures,
                                 migrate, switchboard, query,
                                 multiplex, mcp)

ARCHITECTURE LAYER             views/    DD views, master DAG
                                 │
                                 ▼
                               secure/   phone SE, key manager,
                                         burn flow, phone-off SM
                                 │
                                 ▼
                               log/      append-only event log,
                                         batch framing, recovery
                                 │
                                 ▼
                               schema/   event taxonomy, identifiers,
                                         kinds — the foundation
```

Arrows point in the depends-on direction. `schema/` depends on
nothing; every other folder depends on it (53 cross-folder
import edges total).

## 3. Glossary

### A — assertion

The data unit. An 8-part frame per R3 §1.3:

  - subject, predicate, object — the WHO/WHAT/WHOM
  - time — `valid_from_ms` / `valid_to_ms` for temporal scoping
  - source — the asserting palace (self or external)
  - stance — autobiographical, etc.
  - confidence — in [0, 1]
  - provenance — `derived_from` edges to drawers (with optional
    span-pointers per R3 §9.3)

Replaces the W3C SPO "triple." See `TRIPLES_REFRAME.md` for the
rationale and the preserved senses ("triangulation" by witness
count; n-tuple arity; temporal-triple proximity).

### A — anchor boundary

The federation ingress decoration mechanism. When external
assertions arrive about a self-entity, they don't *overwrite*
the self's view — they *decorate* it. Triangulation count =
how many independent palaces have asserted the same claim. See
`mempalace/federate/anchor_boundary.py`.

### B — batch framing

Every multi-event operation runs inside an explicit
`BatchStarted` / `BatchCommitted` envelope. Crash mid-batch is
recognizable on recovery as a torn batch. PHASE1 §J adds
**sub-batch checkpointing**: long batches emit
`BatchCheckpointed` markers so recovery rolls forward to the
last checkpoint instead of aborting the whole batch.

### B — burn

The Tier-2 deletion flow. The phone destroys its master key,
which renders all phone-encrypted drawers permanently
unreadable — even by the daemon. The on-chain
`IntegrityLockout` mirrors this: any palace claiming continued
operation after burn is provably lying.

### C — canonicalizer

Normalizes aliases / dialect / spelling variations into stable
canonical forms. Reversibility is via event-supersession: if
`canonical_for("car")` was wrongly set to "automobile," emit
the inverse. The log keeps both versions; the latest wins.
`mempalace/canonicalizer.py`.

### C — characteristic (temporal)

One axis of a `TemporalQuery`: a description of what the query
reaches for at the past, present, or future. Carries a
substrate bias (`dag_weight` + `chroma_weight`) that controls
which substrate produces seed nodes for that region. See
`mempalace/retrieve/temporal.py`.

### C — Chroma hop

A traversal hop type. Embedding-space nearest-neighbor jump
between two drawer nodes that aren't connected in the DAG but
are similar in vector space. Costs more than a DAG hop because
it's unanchored — but legitimate for crossing structural gaps.
The walker prefers DAG hops when available; Chroma is the
fallback for the "geography vs geometry" case where two
drawers feel similar but aren't structurally linked.

### C — content_hash

BLAKE2b-32 of the drawer's verbatim transcript + structured
facets. Drawers with the same content_hash are dedup-collisions
that the substrate must handle explicitly (not silently merged).

### C — Conway's three selves

The retrieval architecture's theoretical anchor (Conway 2005).

  - Working self → present-state cues, what's active now
  - Autobiographical self → episodic record, the SPGM hierarchy
  - Conceptual self → goals, projected futures, becoming-states

The handle protocol (`mem_allocate` / `mem_refine` /
`mem_resolve` / `mem_close`) is generative retrieval almost
word-for-word from Conway. The **temporal triple** maps the
three selves onto query characteristics — past / present /
future — and the walker traverses across them.

### D — DAG

The assertion graph. Nodes are entities, periods, events,
assertions, drawer-refs, schemas, themes, recurrence-clusters.
Edges are typed (REFINES, CONTRADICTS, SUPPORTS, DERIVED_FROM,
PRECEDES, PURSUES, AIMED_AT, SAME_AS, etc.). The DAG knows
**what a drawer is about**.

Distance in the DAG is structural: short paths share periods,
events, asserter-identity. Two drawers about "geography class"
and "geometry class" are DAG-close (shared school period,
shared subject-of-interest predicate) but Chroma-far (different
embedding clusters). See **substrate dispatch**.

### D — DD views

Differential-dataflow style derived views. Each consumer
projects a subset of the event log into a focused state shape
(e.g., the topology view, the current-node view, the ranker
cache). Phase 5 (DD frontier alignment) ensures consumers can
read consistently across views.

### D — drawer

The unit of capture. A timestamped, content-hashed bundle of
verbatim transcript + acoustic facet + semantic embedding +
structural facet + social facet (per R3 §0). Drawers are the
substrate's lowest-level evidence; assertions derive from them.

### E — event log

The append-only source of truth. Every state change is an
event. Views are projections; the log is the ground truth. If
the views become inconsistent, replay the log to rebuild them.
`mempalace/log/client.py`.

### F — federation

Cross-palace operations: matching, anchor-boundary decoration,
RHYME similarity. A palace can express compatibility with
another without revealing private substrate, via signed
attestations + on-chain commitments. See `mempalace/federate/`.

### F — frontier

The largest log offset that all consumers have processed.
Consumers can't read past their own frontier; cross-consumer
consistent reads bound by the **lowest** frontier across
consumers. Phase 3 of the five-phase correctness story.

### H — handle

A query-in-flight. Allocated via `mem_allocate(scope, stance)`,
refined via `mem_refine`, resolved via `mem_resolve`, closed
via `mem_close`. Holds an `InterpretiveFrame` (or a stack of
them) that describes what the query is reaching for.

The handle protocol is the v5 architecture's load-bearing
keystone (introduced in the very first conversation, May 2,
2026; 35+ mentions by batch1). It IS Conway's generative
retrieval, mapped to API. See `HANDLES_DESIGN.md`.

### H — hop

A single step in a temporal-triple traversal. Three kinds:

  - `DAG_EDGE` — typed edge in the assertion graph. Cost 1.0.
  - `CHROMA_NN` — embedding nearest-neighbor jump. Cost 1.6,
    decreases with similarity.
  - `PROJECTION` — virtual hop to a hypothesized future-node.
    Cost 2.5. Last resort when no observed substrate satisfies
    the future characteristic.

### I — InterpretiveFrame

> "The InterpretiveFrame stays mostly the same. Its job is to
> project the query onto handle-state. The frame describes the
> query's region (where to start); the walker handles traversal
> between regions."

The frame holds five typed axes per `SUBSTRATE_SIGNAL_ANALYSIS.md`:
signature_region, conway_rate, co_activation_pattern,
refinement_cues, voice_flavor. It does NOT carry the temporal
triple itself — that's the walker's job. The frame describes
where; the walker describes how-to-get-from-here-to-there.

### M — miner

The pass infrastructure that turns substrate (drawers) into
structure (assertions, themes, periods, schemas). Three classes:

  - Class 1 — streaming, single-drawer extraction
  - Class 2 — cross-drawer, looking back across recent history
  - Class 3 — schema induction, clustering, recurrence detection
  - Class 4 — (planned) projected-trajectory pre-computation

Each pass is content-version-stamped and idempotent; reruns
produce the same outputs.

### P — palace

The integrated whole. The `Palace` class in `mempalace/palace.py`
is a thin facade that bundles the subsystems and exposes the
common verbs. Created via `Palace.create()` or
`Palace.open(palace_dir=...)`.

### P — phone-off graceful degradation

State machine for daemon operation when the phone is offline.
Three modes:

  - ONLINE — full operation, fresh attestation, valid keys
  - READ_ONLY — heartbeat stale OR keys TTL-expired; serve
    queries, no writes
  - LOCKED_OUT — 3 consecutive missed heartbeats; daemon stops;
    on-chain `trigger_app_integrity_lockout` callable

Recovery from LOCKED_OUT requires re-enrollment. See R3 §7.6
and `mempalace/secure/phone_off.py`.

### P — projection (future)

When a `TemporalQuery` has no future characteristic, or the
future characteristic doesn't resolve to any seed nodes in the
substrate, the walker **projects**:

> "possible futures are always projection of that present's
> lens on the past tempered by existing calibrations for the
> future intent"

Algorithm:
  1. Apply present's embedding lens to past episodes — find
     past analogues
  2. Walk forward in time from analogues — where did similar
     situations lead?
  3. Temper by existing future-intent calibrations (PURSUES,
     AIMED_AT, INTENDS-style assertions); intersect or fall
     back to calibrations directly
  4. If neither analogues nor calibrations land, hypothesize a
     virtual `projected_<hash>` node. The `↣ PROJECTION` hop
     into that node carries an inference-confidence penalty.

Narrative answers explicitly flag when projection occurred.

### P — proximity (temporal-triple)

The retrieval primitive proper to v5. NOT a single distance —
**a coherent traversal** through the substrate that touches
one node from each temporal region (past, present, future).
The path IS the answer; a synthesized prose response cites the
path as provenance.

Score combines path length, mix of DAG/Chroma/Projection hops
(more DAG anchoring = higher coherence), and triple
completeness. Pure-Chroma chains rank lowest; full-DAG paths
with all three regions touched rank highest.

### R — RHYME

Refraction-Resonance similarity. Captures "similar but not
identical" maximally, peaking at sim ≈ 0.577 via
`4·sim·(1−sim²)/peak`. Pure duplicates score lower (too
similar); pure independents score lower (no resonance). Used
in federation matching and near-duplicate detection.

### S — schema

A node kind representing an inferred recurring pattern across
drawers. Class 3 mining produces these. Schemas are the
substrate's emergent ontology — what topics matter, what
episodic shapes recur.

### S — secure element

The phone-side encryption surface. Drawers can be encrypted at
edge (v2 schema) so the cloud-box log only sees ciphertext.
Read path requires phone decrypt. v0 schema is plaintext for
test/development.

### S — span-pointer

A token-offset range on a `derived_from` edge indicating
*which specific portion* of a drawer backed an assertion. Per
R3 §9.3, enables **substrate verification**: comparing the
assertion text to the actual substrate text at the cited span,
flagging coherence-overwrite (assertions that drift from what
the drawer actually said).

### S — stack / step

The unified primitive for ranker stacks, inference stacks,
miner pass stacks, federation matching layers, and resolution
pipelines (R3 §1, six consumers). A `Step` is pure; a
`StepManifest` declares its inputs/outputs; a `StackContext`
threads I/O between steps; a `TrustedAggregator` combines
multiple stacks' outputs (R3 §6 ranker isolation builds on
this).

### S — substrate dispatch

Different temporal characteristics naturally favor different
substrates:

  - **Past** biases DAG. Events are structurally located in time;
    assertions carry valid_from/valid_to. The DAG knows what a
    drawer is about.
  - **Present** biases Chroma. Current-state isn't yet structured
    into the assertion graph (mining lags real-time). Embeddings
    know what a drawer feels like.
  - **Future** is mixed. Explicit goal assertions live in DAG;
    aspirational language sits in embedding clusters.

`Characteristic.dag_weight` + `Characteristic.chroma_weight`
encode this per-axis bias. The walker honors it during region
resolution, and the hop-cost weights bias traversal choice.

### S — substrate verification

R3 §9.3 mechanism for detecting confabulation. At retrieval
time, compute a **faithfulness score** for each retrieved
assertion: how closely does the assertion text match the
substrate text at the cited spans? Low score = possible
coherence-overwrite; high score = well-grounded.

Default scorer is token-set Jaccard; pluggable via custom
`text_similarity` callable for embedding-based upgrade.

### T — temporal triple

Three characteristics (past, present, future) held in union by
a traversal. The user reframe of "triple" — preserves the
sense of three things-in-relation, applied to time. See
`mempalace/retrieve/temporal.py` and the **proximity** entry.

### T — TRIPLES_REFRAME.md

The doc that catalogues the senses of "triple" preserved after
the assertion rename:

  1. Higher-arity associations (n-tuple sense) — pairs, triples,
     quintuples as patterns over assertion-node-with-edges
  2. Triangulation (witness sense) — three independent palaces
     asserting the same claim = strong cross-palace signal
  3. Temporal triple (proximity sense) — past/present/future
     characteristics held in union by a traversal

### V — views

The derived state layer. `current.py` materializes node/edge
state from the log. `graph.py` is the assertion-aware accessor
(add_assertion, query helpers). `topology.py` exposes drawer
metadata without plaintext (encryption boundary).

---

## 4. Where we deviated from `mempalace-develop`

The original codebase shipped with v3.1.1 stability + v4.0-alpha
(PostgreSQL/LanceDB backends, hybrid search, time-decay scoring,
MCP tools). The greenfield architecture overwrites it where
relevant. Specific deviations and rationale:

### 4.1 Source of truth: ChromaDB → Event log

**Original**: ChromaDB + SQLite KG were the sources of truth.
Reads went directly to the vector store and graph DB.

**Greenfield**: Append-only event log is the source of truth.
ChromaDB remains, but as one of two retrieval substrates (the
embedding side); the assertion DAG is the other. Views are
projections of the log.

**Why**: Event log gives us crash recovery, content-version
stamps, dependency tracking, and migration safety for free.
The original architecture had to roll its own consistency
machinery (per ROADMAP.md #664 BLOB seq_id auto-repair, #346
HNSW index bloat prevention) — those failure modes don't apply
to an event log.

### 4.2 Single `palace.py` → 21-folder package

**Original**: One ~425-line `palace.py` orchestrating
everything; flat top-level Python files (palace_graph.py,
knowledge_graph.py, miner.py, searcher.py, embedding.py, etc.).

**Greenfield**: 21 subsystem folders with focused
responsibilities. Top-level `Palace` class in `palace.py` is a
thin facade that bundles them.

**Why**: The original had ~50 files but cross-cutting concerns
(security, batching, frontier coordination) couldn't be
localized. The decomposition makes each subsystem testable in
isolation. The Palace facade restores the single entry point
without re-monolithizing.

### 4.3 Triples → Assertions

**Original**: `Graph.assert_triple(s, p, o)` — W3C SPO style.

**Greenfield**: `Graph.add_assertion(...)` — 8-part frame per
R3 §1.3. Subject, predicate, object, time-window, source,
stance, confidence, provenance. Plus span-pointer support per
R3 §9.3.

**Why**: SPO triples can't carry time, source, or confidence
without sidecar tables. The 8-part frame is what assertions
actually need to be useful for reflective retrieval. See
`TRIPLES_REFRAME.md` for the rename + the preserved senses of
"triple."

### 4.4 No handles → Handle protocol as load-bearing keystone

**Original**: `searcher.py` (~763 lines) — search as a function
call returning hits.

**Greenfield**: Handle protocol —
`mem_allocate / mem_refine / mem_resolve / mem_close`. A
handle is a query-in-flight, with state, refinement history,
fidelity level, stance, and (now) substrate-verification flag.

**Why**: Conway's generative retrieval ISN'T a function call.
It's a process — construct the cue, refine as context shapes,
resolve when committed, close when done. The handle abstraction
maps that process directly. Six consumers in R3 §1 use it
(ranker stacking, inference stacking, miner pass stacking,
federation matching layers, composition layer, wake-up
composer); a function-call API can't carry the state these
consumers share.

### 4.5 Hardcoded layers → Emergent themes

**Original**: `layers.py` (~502 lines) — `hall_events`,
`hall_facts`, `hall_advice` taxonomy hardcoded in the schema.

**Greenfield**: Themes are emergent, induced by Class 3 mining.
The taxonomy IS the substrate's induced schema, not a fixed
ontology. Period nodes layer hierarchically (lifetime →
period → event → drawer per Conway SPGM).

**Why**: The hardcoded taxonomy doesn't generalize — a
math-grad-student's hall_facts aren't a parent's hall_facts.
Emergent themes adapt per palace.

### 4.6 Silent dedup → Explicit collision handling

**Original**: `dedup.py` (~237 lines) — silently merge
duplicates by similarity threshold.

**Greenfield**: `mempalace/drawer/collision.py` — content_hash
collisions are explicit. The substrate records the prior
drawer ID; downstream chooses (replace, both-keep,
canonical-merge).

**Why**: Silent merges erase audit trails. If the user said
"I went to the dentist" twice on different days, those should
be two drawers, not one. The collision is explicit so policy
can decide.

### 4.7 Single ranker → Ranker stack with isolation

**Original**: One ranker function called per query.

**Greenfield**: Ranker stack with process isolation,
behavioral monitoring, signed loader (R3 §6). Per-ranker
resource limits via POSIX rlimits; sandbox profile hooks for
bwrap/sandbox-exec wrapping.

**Why**: Multi-ranker is needed for ensembles, A/B testing,
and downloaded ranker specialization (RHYME, stance-conditional,
etc.). Process isolation prevents one bad ranker from
corrupting another.

### 4.8 No federation → Federation with anchor boundary

**Original**: Single-palace; no cross-palace operations.

**Greenfield**: Full federation stack — matching layers (1, 2a,
2b, 3), anchor boundary (decorate-not-overwrite for external
assertions), RHYME similarity, asserter identity carrying
through to assertion provenance.

**Why**: A palace alone is incomplete — relationships and
discourse require cross-palace queries. The anchor boundary
ensures privacy: external claims about self don't overwrite
self-state, they decorate it.

### 4.9 No phone secure element → Encryption-at-edge

**Original**: Plaintext throughout.

**Greenfield**: `PhoneSecureElement` protocol; v0 (plaintext)
and v2 (phone-encrypted) drawer schemas; burn flow for Tier-2
deletion. Phone-off graceful degradation state machine for
daemon resilience.

**Why**: Memory substrate IS the most sensitive data a person
has. Cloud-box compromise must not reveal verbatim drawers.
Phone holds master keys; daemon has TTL'd session keys.

### 4.10 Single search call → Temporal-triple proximity retrieval

**Original**: Search returns hits ranked by hybrid similarity.

**Greenfield**: For reflective queries, temporal-triple
proximity — find a coherent traversal through past + present +
future regions of the substrate. Both substrates participate
(DAG hops + Chroma hops + projection hops). Result format is
both the path (structural) and a synthesized narrative
(citing the path).

**Why**: Reflective queries (like "should I go to grad school
for math") aren't single-document answers. The answer is the
trajectory the substrate composes. Hybrid scoring on isolated
documents misses the trajectory.

### 4.11 No on-chain integration → Switchboard SDK

**Original**: No blockchain.

**Greenfield**: Oracle SDK (per ORACLE_THREAT_MODEL.md) for
prediction-market resolution. Four event kinds for resolver
assignment, finding submission, subject-blind decloak,
challenge. K-of-N consensus; subject-blind path with
challenge window; integrity-lockout enforcement.

**Why**: The substrate's value compounds when palaces can
make verifiable claims about themselves to markets. The
on-chain side enforces stake on those claims. Off-chain
indexer turns chain events into typed Python events for the
substrate.

### 4.12 No discourse structure → Layer 2b discourse patterns

**Original**: No.

**Greenfield**: Discourse-pattern extractor (R3 §9.5) walks
REFINES chains, contradiction-resolution patterns,
supports/opposes balance. Integrates into federation matching
layer 2b — typed-discourse signal blends with generic graph
similarity at 30% weight.

**Why**: Two palaces matching on assertion-graph structure
alone can miss a deeper compatibility. If both have similar
refinement chains, similar contradiction-resolution rates,
similar support/oppose balance — that's a much stronger
relational signal than predicate overlap.

---

## 5. Architectural commitments preserved verbatim

These passages describe load-bearing decisions. Preserving them
verbatim because the wording is what makes them clear.

### On the InterpretiveFrame

> "The InterpretiveFrame stays mostly the same. Its job is to
> project the query onto handle-state. The frame describes the
> query's region (where to start); the walker handles traversal
> between regions."

### On substrate dispatch

> "Different temporal characteristics naturally favor different
> substrates:
>   - Past biases DAG. Events are structurally located in time;
>     assertions carry valid_from/valid_to. The DAG knows what a
>     drawer is about.
>   - Present biases Chroma. Current-state isn't yet structured
>     into the assertion graph (mining lags real-time).
>     Embeddings know what a drawer feels like.
>   - Future is mixed. Explicit goal assertions live in DAG;
>     aspirational language sits in embedding clusters."

### On geography vs geometry

> "DAG distance: Drawer about geography class ↔ Drawer about
> geometry class — short (shared period node, shared 'school'
> theme, shared 'subject_of_interest' predicate). Chroma
> distance: long (geography → maps/travel/places vocab; geometry
> → proofs/abstraction/spatial-reasoning vocab — different
> embedding clusters). DAG says close, Chroma says far.
>
> Drawer 'I struggled in geometry' ↔ Drawer 'Quitting violin
> was hard' — Chroma distance: short (struggle/quitting/
> hard-decision shape — same emotional embedding). DAG distance:
> long (no shared subject, different periods, no shared
> assertions). DAG says far, Chroma says close.
>
> Both observations are valid, just answering different
> questions. The DAG knows what *thing* the drawer is *about*;
> Chroma knows what the drawer *feels like*."

### On future projection

> "Possible futures are always projection of that present's
> lens on the past, tempered by existing calibrations for the
> future intent. If there are none, generate a hypothesized
> future-node via inference (projected triple)."
>
> *— user spec, ratified*

### On the path as primitive

> "Proximity here isn't a single number; it's whether you can
> draw a coherent line through all three regions, and how short
> / how well-grounded that line is. A traversal is a sequence
> of typed hops. A 'good' path mixes DAG and Chroma — DAG
> anchors keep it honest, Chroma hops let it cross between
> structurally-disconnected drawers that actually relate. A
> path made of pure Chroma hops gets penalized; one with at
> least two DAG anchors is high-confidence."

### On Conway's three selves mapping to the temporal triple

> "Working self → present-state cues, what's active now.
> Autobiographical self → episodic record, the SPGM hierarchy.
> Conceptual self → goals, projected futures, becoming-states.
>
> The handle protocol IS Conway's generative retrieval almost
> word-for-word. The temporal triple makes the three selves
> first-class instead of implicit."

### On the substrate-verification confabulation check

> "Schema-driven gap-filling is a confabulation risk. The miner
> can assemble an assertion from multiple drawers in ways that
> drift from what the drawers actually said — producing claims
> that look well-formed but aren't grounded in the substrate.
> R3 §9.3 calls this 'coherence-overwrite.' The mitigation: at
> retrieval time, the consumer can request substrate
> verification. Retrieved assertions come with their supporting
> drawer references AND a substrate-faithfulness score
> indicating how closely the assertion text matches the
> substrate text at the cited spans."

### On the asserter being provenance, not separate

> "Asserter is provenance on assertion node properties.
> Backwards-compatible default = self. When `asserter` is
> non-empty, the assertion records that a third party
> (`asserter.palace_id`) made this claim — used for cross-palace
> mentions; the matching layer's strongest alignment criterion."

### On the oracle commitment

> "Oracle commitment is the palace itself, not lamports.
> palace_genesis_slot is depth signal; assigners weight K-of-N
> selection by it."

---

## 6. Test count progression

Cumulative across the v5 build:

  - Phase-by-phase batches → 724 tests
  - + 4 oracle SDK event kinds → 724 (smoke only)
  - + substrate verification + spans → 758
  - + sub-batch checkpointing → 768
  - + phone-off graceful degradation → 787
  - + ranker isolation extensions → 797
  - + discourse pattern extraction → 809
  - + discourse layer 2b integration → 813
  - + Palace facade + entrypoint → 828
  - + temporal-triple retrieval → 845

**845 tests passing, 19 skipped, zero regressions.**
