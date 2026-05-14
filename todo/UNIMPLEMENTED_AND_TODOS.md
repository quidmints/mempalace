# MemPalace — Unimplemented Scope, TODOs, and Verification Backlog


### 1.3 examples/switchboard end-to-end with localnet

The Python testbench (`examples/switchboard/`) runs end-to-end
in-process — it simulates each privacy mode by emitting events
directly to the log via the `chain_observer` functions. The
**Rust-side companion** that would:

  1. Spin up `solana-test-validator` with the program deployed
  2. Call `assign_resolver` → emit on-chain `ResolverAssigned`
  3. Call `submit_finding` → emit on-chain `FindingSubmitted`
  4. Verify the indexer correctly turns each on-chain event into
     the corresponding typed Python event

is not yet written. Blocked on (1.1) — once the `quid`
integration lands, the localnet harness can sit alongside it.

---

## 2. Shipped, but needs verification when MCP/real services available

### 2.1 Chroma nearest-neighbor as a hop primitive

`HopKind.CHROMA_NN` is implemented in
`mempalace/retrieve/temporal.py` with cost weighting,
similarity-aware cost decay, and beam-search expansion. Tests
exercise it against `InMemoryBackend` (the dev/test vector store).

**To verify when real ChromaDB is wired:**

  - Confirm `EmbeddingStore.query(vector, k)` returns
    `SearchResult` objects with the expected
    `drawer_id` / `similarity` shape from the production Chroma
    backend (not the in-memory shim)
  - Benchmark hop cost: real Chroma queries are non-trivial
    latency-wise compared to in-memory. The `_expand` function
    embeds the current node's text and queries Chroma per
    frontier expansion. With beam_width=8 and avg 3 hops, that's
    ~24 Chroma queries per `traverse()` — verify this is
    acceptable and add caching if not
  - Verify embedding model consistency: the embedding service
    used at capture-time must match the one used at retrieval-time
    (or the vectors won't be in the same space). The
    `mempalace.embed.reconcile` machinery exists for migration
    but production hookup needs verification
  - Confirm the `InMemoryBackend → ChromaBackend` swap via
    `set_default_store()` works without code changes elsewhere

**Why it matters:** The temporal-triple walker treats DAG hops
and Chroma hops as **first-class peers**. If real Chroma queries
behave differently from the in-memory shim (different distance
metric semantics, different similarity range, batching
requirements), the hop-cost calibration may need re-tuning and
the beam-search behavior may change.

### 2.2 Substrate verification with embedding-based scorer

Default scorer is token-set Jaccard — fine for token overlap, bad
for semantic faithfulness. Per the `substrate_verification.py`
docstring, callers can supply a custom `text_similarity` callable.

**To verify when LLM/embedding services available:**

  - Plug in an embedding-based scorer (cosine of assertion-text
    embedding vs substrate-text embedding). Run on a corpus where
    you know which assertions are well-grounded vs confabulated.
    Validate the threshold defaults
    (DEFAULT_LOW_FAITHFULNESS_THRESHOLD=0.3,
    DEFAULT_HIGH_FAITHFULNESS_THRESHOLD=0.7) actually separate
    those classes
  - Calibrate `DRAWER_LEVEL_DISCOUNT=0.7` against ground-truth
    span-pinned vs drawer-only assertions. Currently set
    heuristically

### 2.3 LLM-driven synthesizer for temporal_query

`synthesize_answer()` in `temporal.py` is template-based — it
produces structured prose summarizing the path. Production
deployments would replace this with an LLM-driven synthesizer
that takes (query, paths) → response, using paths as grounding
context for retrieval-augmented generation.

**To wire when LLM client production-ready:**

  - Implement `LLMSynthesizer` that:
    - Takes the `TemporalQuery` and ranked paths
    - Constructs a prompt with the path nodes + their substrate
      text as context
    - Asks the LLM to synthesize a response that cites specific
      path elements
    - Validates the response cites at least one path node per
      time-axis (otherwise fall back to template)
  - Add a `palace.temporal_query(query, synthesizer=...)` kwarg
    so callers can choose template vs LLM-driven
  - Cache synthesizer outputs by (query_hash, path_hash) — same
    query against same substrate state produces same answer

### 2.4 Phone-off TTL refresh against real phone heartbeat

The `PhoneOffStateMachine` is wired and tested against a manual
clock. When real phone client is available:

  - Verify the heartbeat cadence parameter
    (DEFAULT_HEARTBEAT_CADENCE_MS = 1 hour) works in practice —
    phones lose connectivity often; 1 hour might be too short
    (constant READ_ONLY churn) or too long (lockout exposure)
  - Confirm `record_keys_issued()` is called from the right place
    in the bundle-refresh flow
  - Verify the `IntegrityLockout` event emitted on transition
    propagates correctly to the on-chain
    `trigger_app_integrity_lockout` instruction (depends on 1.1
    and 1.2)
  - Test re-enrollment recovery path
    (`force_unlock_after_reenrollment`) end-to-end with a real
    phone

### 2.5 Ranker isolation with real sandbox profiles

`SandboxProfile.bwrap_minimal()` returns a starting-point bwrap
wrapper. Production deployment needs:

  - Tune the bwrap profile per the actual ranker's needs:
    - Bind /lib + /usr/lib for shared libraries
    - Bind a scratch dir if the ranker needs scratch space
    - Drop all capabilities (`--cap-drop ALL`)
  - macOS: write a `.sb` policy for `sandbox-exec` and add
    `SandboxProfile.sandbox_exec_minimal()`
  - Windows: implement Job Object-based isolation (the
    `_build_preexec_fn` returns None on Windows — needs a
    different code path)
  - Verify `RLIMIT_AS` actually caps memory on Linux (some
    distros disable it; need fallback)

### 2.6 Sub-batch checkpoint recovery in production

`BatchCheckpointed` events advance the recovery frontier
correctly per tests, but the tests use synthetic torn batches.
Production verification:

  - Crash a real long-running mining batch mid-flight after at
    least one checkpoint
  - Verify recovery rolls forward to the latest checkpoint
    (not back to BatchStarted-1)
  - Confirm consumers downstream of the partial events handle
    the partial-state gracefully
  - Tune checkpoint frequency: too often = log bloat; too rare =
    no recovery benefit

## 3. Future architectural work

### 3.1 Federation matching with temporal-triple proximity

> "When two palaces match on assertions, they currently match
> on graph-proximity. Adding temporal-triple proximity means we
> can say 'this person and that person both had a similar
> past-characteristic / present-characteristic /
> future-characteristic triangle around the same topic' — which
> is a much stronger compatibility signal than predicate overlap
> alone."

**What to build:**

  - `mempalace/federate/temporal_match.py`:
    - For each candidate match, extract the temporal triple of
      both palaces (their past/present/future characteristics
      for the matching topic)
    - Compute pairwise similarity per axis (e.g., RHYME between
      both palaces' past characteristics, both presents, both
      futures)
    - Combine into a triple-similarity score that augments the
      existing layer 2b/2c matching pipeline
  - Add a new `TemporalTripleSimilarity` step to the federation
    matching kernel, sitting alongside the existing
    `DerivationGraphSimilarity` (which now blends in the
    typed-discourse signal from R3 §9.5)
  - Integration with the matching cascade: the temporal-triple
    score gates whether to proceed to deeper layers

**Why it matters:** Two palaces having similar predicate vocab
isn't compatibility — it's just topical overlap. Two palaces
with similar **trajectory triangles** (similar pasts, similar
present-states, similar projected-futures) around the same topic
indicates *deep* compatibility. That's the right signal for
mentor matching, peer matching, complementary-skill matching.

### 3.2 Substrate-honest retrieval (cost goes where it earns)

> "Instead of every query running the same hybrid scoring,
> queries that lean past use the DAG more; queries that lean
> present use Chroma more. Cost goes where it earns."

**What to build:**

  - **Query classifier**: given a query (verbatim text or
    `Characteristic`), classify its temporal lean. Past-leaning
    queries skip embedding-store entirely on the past axis,
    present-leaning queries don't bother with DAG predicate-match
    on the present axis, etc.
  - **Per-substrate budget**: each `TemporalQuery` carries a
    cost budget (default unlimited; configurable per consumer).
    The walker tracks substrate cost separately:
    - DAG hops: O(local edges) — cheap
    - Chroma hops: O(embed call + k-NN query) — expensive
    - Projection: O(forward walk + calibration scan) — moderate
  - **Adaptive backoff**: if a query's beam keeps producing
    Chroma hops with low similarity (no genuine cross-link),
    reduce Chroma exploration and stay on the DAG side. If DAG
    paths keep dead-ending, expand Chroma exploration

**Why it matters:** Currently every traversal call does both DAG
and Chroma expansion at every frontier. For a palace with
millions of drawers and tens of millions of edges, that's
potentially expensive. Substrate-honest retrieval pays the cost
only where it actually contributes. A pure-recall query
("what was for dinner Tuesday") shouldn't trigger embedding
inference at all.

### 3.3 Class 4 miner pass for projected trajectories

Right now the `_project_future_region` function runs at query
time, every time. For "hot" regions (frequently queried subjects)
this is wasteful — the projected future doesn't change unless
the substrate underneath does.

**What to build:**

  - `mempalace/miner/class4_projection.py`: a Class 4 miner pass
    that:
    - Identifies hot subjects (assertions / themes with high
      query frequency)
    - Pre-computes projected trajectories for each
    - Stamps them as `ProjectedTrajectory` nodes in the DAG
      (a new node kind) with TTL
    - Re-runs when the underlying past+present substrate
      changes
  - When `traverse()` runs a query whose future axis lands in a
    hot region, it can short-circuit: instead of running the
    projection algorithm in-flight, it consumes the
    pre-computed `ProjectedTrajectory`
  - Cost of this miner pass amortizes over query frequency

**Why it matters:** Reflective queries are slow. A user asking
"where am I heading with X" multiple times shouldn't pay the
projection cost each time. Class 4 makes projection a
cache-warmable surface.

### 3.4 Discourse pattern integration into composition layer

Discourse-pattern extraction is wired into federation matching
layer 2b. It's NOT wired into:

  - The composition layer (when synthesizing answers from
    multiple sources, prefer paths whose discourse signal aligns)
  - The mining feedback loop (when contradictions resolve via
    SUPERSEDES, the discourse-pattern of the resolver carries
    signal about the user's reasoning style)
  - The cluster_pattern walker (in `mempalace/handle/cluster_pattern.py`
    — currently uses cluster_signature only; could weight by
    discourse coherence)

**What to build:** Integration tests covering each consumer.
Then expose discourse-pattern as a feature available to any step
in the stack via the `mempalace.features.compute` pipeline.

### 3.5 The migration script

Not needed for fresh deployments (the user explicitly chose
"greenfield overwrites where relevant; we're not linking
existing deployments"). But if you ever need to onboard from a
legacy v3.1/v4 ChromaDB+SQLite palace:

  - Write `mempalace/migrate/from_v4.py` that:
    - Reads the legacy ChromaDB collection
    - Reads the legacy SQLite KG
    - Emits equivalent `DrawerCaptured` + `NodeCreated` +
      `EdgeCreated` events into the greenfield log
    - Validates the resulting view-state matches expected counts

Currently in scope per `MEMPALACE_FILE_ACCOUNTING.md` only as a
hypothetical. **Do not build unless someone needs it.**

### 3.6 Stance-conditional ranker dispatch

`mempalace/rank/dispatch.py` exists but the stance-conditional
routing logic is partially implemented. Production use cases
need:

  - A registry of (stance vector → preferred ranker IDs) that
    the dispatch consults
  - Default rankers per stance dimension for the cold-start case
  - A fallback when no ranker matches the stance — currently
    returns a blended ensemble; verify this is the right default

### 3.7 Real voice models for the voice stack

The voice stack (`mempalace/stack/voice/`) currently runs against
test fixtures with synthetic ASR/diarization/prosody outputs.
Hooking it to real Whisper/PyAnnote/SpeechBrain models is a
hardware + RN-side concern (per `REACT_NATIVE_TODO.md`).

### 3.8 Learned search policy (Track 3C)

`mempalace/handle/search_policy.py` implements rule-based
explore/commit directives. The R3 spec called for an eventually-
learned policy that adapts to the user's retrieval patterns.
Requires months of telemetry data first; deferred indefinitely.

### 3.9 Local NLP for room detection

`mempalace-develop` had `room_detector_local.py` (#507 in their
ROADMAP). The greenfield doesn't have an equivalent. Decide
whether room detection is needed in v5 — if yes, add a Class 1
miner-pass step that detects room boundaries.

### 3.10 MCP integration

`mempalace/mcp/` exists as a planned consumer surface but has
minimal content. The original `mempalace-develop` had a 1939-line
`mcp_server.py`. Building the v5 MCP server means:

  - Map each `Palace` verb (capture, search, assert_,
    temporal_query, ...) to MCP tool definitions
  - Wire stdio transport
  - Handle authentication / session
  - Expose subsystem-level access for power users
    (palace.federate, palace.switchboard, palace.phone_off, etc.)

This is operational work that doesn't require new architecture —
the `Palace` facade is the right surface to build the MCP server
against.

### 3.11 CLI

Same shape as MCP integration. The original had `cli.py`
(1294 lines). The greenfield's CLI would wrap the `Palace`
facade with argparse / click. No architecture work needed.

### 3.12 Replay / time-travel debugging

The append-only log makes time-travel debugging trivial in
principle: snapshot the log at offset N, replay views from there.
Not yet exposed as an API. Useful for debugging confabulation
issues, miner-pass regressions, etc.

---

## 4. Items called out for documentation and follow-up

These came up in conversation as important to track explicitly:

### 4.1 "Chroma nearest-neighbor as a hop primitive equal to follow-DAG-edge"

**Status: shipped (in `mempalace/retrieve/temporal.py`).** Verify
when real Chroma backend is wired (see 2.1 above).

### 4.2 "Better federation matching via temporal-triple proximity"

**Status: not yet shipped.** Designed in 3.1 above. The
temporal-triple primitive itself is shipped; what's missing is
the federation-matching kernel that uses two palaces' triples to
score compatibility.

### 4.3 "Substrate-honest retrieval"

**Status: partially shipped.** The substrate dispatch via
`Characteristic.dag_weight` + `chroma_weight` is in. The
hop-cost weights are calibrated. What's missing is the adaptive
cost-budgeting and per-query lean classification (3.2 above).

## 5. Tests that are currently skipped

19 tests in the regression suite skip for various reasons:

  - Tests that depend on Rust toolchain (skip when no `cargo`)
  - Tests that need real ChromaDB (skip when `InMemoryBackend`
    is the default)
  - Tests that need a real LLM client (skip in offline mode)
  - Tests for not-yet-implemented features (skipped with
    `unittest.skip`)

When the environment provides those services, run the full suite
without skip-conditions and confirm the previously-skipped tests
pass.

## 6. Things to double-check after this session

In case I missed something:

  - Run `pytest --collect-only` and confirm all test files import
    cleanly (no orphaned imports from refactored modules)
  - Run `python -c "from mempalace import Palace"` and verify
    no warnings
  - Inspect `mempalace/__init__.py` — make sure it doesn't grow
    transitive imports that slow down startup
  - Inspect `mempalace/palace.py` — the subsystem-property
    accessors use lazy imports; verify those don't break under
    `from mempalace import *`

## 7. Vestigial cleanup

Low-priority but worth a sweep:

  - `mempalace/log/client.py:302` — comment string
    `"graph.assert_triple"` should be `"graph.add_assertion"`
  - `mempalace/log/recovery_hook.py:22` — same
  - `mempalace/derived/ranker_cache.py:108` — comment uses
    "triple" in the SPO sense
  - `mempalace/migrate/converter.py` — `LegacyTriple` is the
    correct name (it's literally the legacy triple we're
    converting); leave alone

5-minute task; doesn't affect runtime.
