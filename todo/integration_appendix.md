# MemPalace Integration Appendix

**Companion to** `mempalace_spec.md` (the 11-part architecture).
**Subject:** Triage of the `predictions/` files against the spec; generalization of the canonicalizer pattern; semantic-evidence and third-party-mention market protocols.

---

## 0. How to read this document

The spec defines the architecture; this appendix maps the existing implementation onto it. Every Python file in `predictions/` is treated as **incomplete**. The triage is:

- **Keep** — the file's intent and approach are already aligned. Minor refactors to fit the new substrate.
- **Refactor** — the file solves a real problem in the spec but the implementation needs significant rework (different storage, different interface, broader applicability).
- **Discard** — the file's approach is obsoleted by spec decisions. The capability it provided is delivered differently.
- **Extract** — there's a useful kernel inside a file that gets lifted into a new home.

The default disposition is **rewrite**, per your instruction not to be attached to the current implementation.

The two Rust files (`state.rs`, `acta.rs`) describe the on-chain evidence model. They are **kept as reference** — the on-chain layer doesn't change with this appendix, but it gets **extended** with new instructions and new data structures to support the two new market classes.

---

## 1. Triage of the `predictions/` files

### 1.1 Keep (with light refactor)

**`predicate_canonicalizer.py`** — Keep. This is the canonical example of the pattern we generalize in §2. The implementation needs minor changes: store canonical embeddings in the master-views store rather than in lru_cache memory, and emit `node_property_set` events when learned canonicals are accepted (so the canonical set is itself in the log and queryable).

**`kg_sketch.py`** — Keep, retarget. MinHash-over-random-walks gives you a structural-similarity signature of the graph without exposing content. In the new architecture this is a **derived representation** (Part 8) feeding the **layer-1 structural triangulation** of the federation pathway (Part 9.2). Refactors:

- Reads from current_nodes/current_edges views rather than SQLite tables.
- Walk weighting respects edge kinds (don't walk `inhibits` edges; weight `derived_from` differently from `instantiates`). Untyped walks were one of the things we said not to do.
- Sketch stored alongside the signature in the signature store, not standalone.

**`manifold_index.py`** — Keep, retarget. The path-weight EMA, uncertainty estimation, and tunnel-discovery patterns are exactly the kind of derived signal the spec calls for. Refactors:

- The "wing/hall/room" path key becomes "theme/period/event" in the new vocabulary.
- Outcomes feed in as `feedback_recorded` events (Part 1.3) rather than direct API calls.
- Tunnel proposals become provisional `same_as` or `recurrence_with` edges going through the miner's confirmed/provisional/rejected lifecycle (Part 10.6).
- The "PURE OBSERVER" design constraint maps onto our derived-representation pattern: this never writes to master, only proposes.

**`watermark.py`**, **`tee_attest.py`** — Keep as-is. Hardware attestation and watermarking are infrastructure; spec doesn't replace them, only consumes their outputs.

**`state.rs`**, **`acta.rs`** — Keep as reference; extend (see §3).

### 1.2 Refactor (substantial rework)

**`engagement_vector.py`** — Refactor. The 7-dimensional engagement vector is a real derived representation. The problems with the current implementation:

- The static `cosine_similarity` method is the hardcoded scoring you flagged. **It must be replaced by a ranker call**, conforming to the protocol from `ranker_protocol.py`. Engagement vectors become a feature input to rankers, not a self-scoring object.
- The dimensions themselves (temporal_depth, engagement_rate, centrality, affect_mean/variance, initiative_rate, modal_spread) are reasonable starting points but are *one feature subset* of the broader feature catalog (Part 6.1). They feed the signature (Part 7.4), not the matching kernel directly.
- Storage moves from a separate SQLite table to a derived view over master events. Each session's effect on engagement is computed by replaying the relevant events, not by mutating a row.
- **Covariance structure** (which we added to the spec) needs to be computed from the engagement-vector history, not just the latest snapshot. The current `history()` method enables this; the computation needs to be added.

**`question_decomposer.py`** — Refactor. The decomposer is the right shape for the **switchboard-thin-compute layer** you mentioned. The current implementation has the LLM-first / heuristic-fallback pattern, which is right. What needs to change:

- The `dispatch_model("DECOMPOSE", ...)` call should route to Claude (or a locally-trained successor) for resolvability determination — given a market question, can it be resolved from available evidence types?
- The output should include not just sub-questions and pipeline config but a **resolvability classification**: `resolvable_from_tags` | `resolvable_from_semantic_content` | `resolvable_from_third_party_mentions` | `resolvable_only_via_jury`. This classification routes the market to the appropriate evidence model (§3).
- The heuristic fallback stays as a degradation path but its keyword matching is too brittle for production. It should fall back to a small classifier trained on past resolved markets (Class 3 miner output, eventually).

**`relational_extract.py`** — Refactor. The current relational extraction is the entry-point for KG triple writing. In the new architecture this becomes the **assertion-emit** path:

- Output is `node_created` events for assertion nodes plus `edge_created` events for asserted_subject/asserted_object/derived_from. Not direct table writes.
- Predicates run through the canonicalizer (`predicate_canonicalizer.py`).
- Multi-source provenance (Part 3.2 derived_from with weights and derivation types) replaces the single source field.
- The 8-part assertion frame is enforced at write.

**`engagement_update.py`** — Refactor. Currently a per-session incremental update. New shape: a streaming Class 1 miner pass (Part 10.5) that consumes substrate events and emits interpretation events updating engagement features.

**`palace_update.py`** — Refactor. Current implementation manages state mutations on the SQLite KG. New shape: appends events to the log; reads happen through views. Most of the file's logic is replaceable with thin event-emit helpers.

**`pipeline.py`** — Refactor. The orchestrator pattern (preprocessors → processors → postprocessors) maps onto our multiplexer (Part 10), but the current implementation is per-session/per-request. The new shape is *long-running dataflow consumers* subscribing to log events, with the preprocessor-postprocessor distinction collapsed into "what events does this consumer subscribe to" and "what events does it emit."

**`market_state.py`** — Refactor. The off-chain mirror of on-chain market state. Kept conceptually but rewritten to use the event log: market state changes become events; `current_market` is a view over those events; cross-market analytics become DDflow operators.

**`evidence_verify.py`** — Refactor. Verifies StrongBox attestation chains. The verification logic is correct; the data shape changes (`VerifiedEvidence` becomes an event payload, `EvidenceSummary` becomes a derived view). Crucially, this file is **the integration point** with the new semantic-evidence protocol (§3) — it gains a sibling for semantic-tier verification.

**`deterministic_resolve.py`** — Refactor. Tag-formula evaluation. The 19 formula types are still useful for tag-based markets; new formula types are added for semantic-content markets (§3.4). The resolution flow (formula → confidence → outcome) extends to semantic markets.

**`resolution_feedback.py`** — Refactor. Stores resolution outcomes for downstream training. New shape: emits `feedback_recorded` events (Part 1.3) keyed to the interpretation versions active at resolution time. This is the credit-assignment chain root for the matching layer.

**`embedder.py`**, **`embed.py`** — Refactor. Embedding generation. The default embedding model gets replaced by the locally-trained model from spec Part 7.3. Embedding writes become events; embedding reads go through ChromaDB but ChromaDB is now using *our* model not its default.

**`dispatch.py`** — Refactor. Model dispatch (route an inference request to a model). New shape: dispatches go through the multiplexer's job-submission API (Part 10.3) so that resource scheduling is unified across rankers, miners, signature extractors, and matching kernel jobs.

**`model_registry.py`** — Refactor. Maps model IDs to providers/configs. Useful as-is conceptually; needs to track *trained* models (different versions of the local embedding model, ranker checkpoints) alongside external API providers. Add: model_class taxonomy (CLASSIFIER | TRANSCRIBER | EMBEDDER | RANKER | MINER | DECOMPOSER), and per-model training-state metadata.

**`device_context.py`** — Refactor. Device identity and capability advertisement. Extended to advertise **semantic-evidence capability**: does this device run the semantic-tier evidence formulator? At what privacy tier? Does it accept third-party-mention queries? See §3.

### 1.3 Discard (capability delivered differently)

**`dag.py`** — Discard. Custom DAG implementation in Python. The new substrate (DDflow over an event log) provides DAG-shaped state through views. No need for a separate Python DAG module; consumers walk views.

**`compose.py`** — Discard. Current implementation does generic composition. Replaced by the composition layer in the spec (transition cache, montage tools, layered rankers). The *concept* survives; the *file* doesn't.

**`encode_result.py`** — Discard. Encoding to oracle return value. The encoding logic stays in the Rust on-chain side (state.rs has `decode_oracle_value` already); the off-chain side just produces structured outputs that the on-chain encoding consumes. No Python module needed.

**`evaluate.py`** — Discard. Twenty lines, generic eval helpers. Replaced by the per-consumer evaluation logic in derived representations.

**`gate.py`**, **`evidence_gate.py`** — Discard as standalone files; their gating logic becomes preconditions on event emission (validated at log append per Part 1.5). The event log already enforces FK and schema; the explicit "gate" abstraction is unnecessary.

**`step_result.py`**, **`execution_plan.py`** — Discard. Pipeline-step result and plan structures. Replaced by event payloads (Part 1) and dataflow consumer state.

**`main.py`** — Discard. Replaced by the multiplexer's main loop (Part 10).

**`validate.py`** — Discard. Validation logic moves to the per-event-kind schema validators (Part 1.5).

**`doc_verify.py`**, **`web_search.py`**, **`multimodal.py`**, **`visual_classify.py`**, **`classify.py`**, **`transcribe.py`** — These are **external-pipeline integrations** for MODE_EXTERNAL markets. They stay in the resolver layer. They don't need refactoring against the spec because they don't touch the palace; they're inputs to oracle resolution. Mark them as **resolver-side, not palace-side** and leave them alone.

**`resolve.py`** — The orchestration entry point for resolution. Discard the file; the orchestration becomes a multiplexer-scheduled job (`MarketResolutionJob`) that consumes the right events and produces a resolution event. Most of the current code is event-shape conversion that the new event log makes unnecessary.

**`entity_embedding_export.py`** — Refactor as part of the federation pathway, not as a standalone export. The signature-projection logic (what gets exposed across palaces) is now in the federation kernel (Part 9). The file's content moves into the projection layer of the matching protocol; the standalone export is dropped.

**`pipeline.py`** (top-level orchestrator) — Already covered in Refactor.

### 1.4 Extract (kernels that move elsewhere)

**The 19 formula types in `deterministic_resolve.py`** — Extract. These are the resolution-formula taxonomy. They're useful but they belong in a **formula registry** module, not embedded in a resolver. New file: `formula_registry.py`. The existing resolver becomes a *runner* that takes a formula from the registry and an evidence summary as input.

**The MinHash sketch builder from `kg_sketch.py`** — Extract into the signature-store derivation pipeline (Part 8.2). The standalone module stays for utility.

**The Welford's-algorithm online mean/variance update from `engagement_vector.py`** — Extract into a `streaming_stats.py` utility used by Class 1 miner for any incremental statistic.

**The seed-canonical-plus-learned pattern from `predicate_canonicalizer.py`** — Extract as the general **post-extraction normalization** pattern; see §2.

**The libp2p protocol negotiation from device-side code** (referenced in state.rs comments) — Extract as the federation transport layer. New module: `libp2p_transport.py` (or Rust equivalent), specified more fully in §3.

---

## 2. The post-extraction normalization pattern (the canonicalizer principle)

### 2.1 The pattern

Looking at `predicate_canonicalizer.py`, the structure is:

```
1. Seed set       — known canonical entries from prior knowledge
2. Fast path      — exact-match lookup against the seed + learned set
3. Slow path      — embedding similarity against the canonical set
4. Threshold      — if similarity above threshold, collapse to canonical
5. Open-world     — if below threshold, accept as new canonical, learn it
6. Cache          — embeddings cached so cost amortizes
```

This is a *general pattern* for any task where:

- A model produces strings or other identifiers from open vocabulary.
- The strings vary in surface form but cluster around stable underlying concepts.
- Downstream consumers benefit from canonicalization (deduped buckets, consistent retrieval, smaller signature dimension).
- New concepts legitimately enter the vocabulary over time.

We should apply this everywhere it fits.

### 2.2 Where else the pattern applies

**Memory-type classification.** The Class 1 miner assigns one of `{correction, decision, fact, preference, lesson, observation, reflection}` to each drawer. But these are seed canonicals; users may surface needs for additional types (e.g., `prediction`, `commitment`, `regret`). Apply the pattern: seed types are canonical; the miner is allowed to propose new types when classifications cluster outside the seeds; user reviews proposals.

**Schema-name canonicalization.** The Class 3 schema-induction pass produces schemas with names like "trust-requires-asymmetric-vulnerability" or "I-with-Maya-as-collaborator." Different induction passes may produce slightly different surface names for the same underlying schema (same as the predicate problem at the schema level). Apply the pattern: induced schemas check against existing schemas for embedding similarity; collapse when above threshold; accept as new when below.

**Entity resolution.** The current `entity_aliases` table is exact-match. Embedding-similarity matching against entity-name-and-context lets you catch "Kai," "Kai Chen," and "@kai" as the same entity even when they weren't pre-aliased. The pattern: known canonical entity names are the seed; new mentions check against canonicals; threshold-collapse or accept-as-new.

**Period-name canonicalization.** Periods named by the user (or auto-named by the miner) may drift. "Auth migration era" vs "the auth refactor" vs "Q2 auth work." Apply the pattern at period creation; collapse near-duplicates.

**Theme normalization.** Themes are top-level partitions (formerly wings). Same drift problem; same solution.

**Goal-state-marker canonicalization.** Drawers carry `goal_markers` from upstream. These are open-vocabulary tags. Canonicalize at miner time so the downstream feature (goal-state stance affinity) operates over a stable vocabulary.

### 2.3 The general module

A new module `normalization.py` (or `canonicalizer.py` generalized) provides:

```python
class Canonicalizer:
    """
    Generic post-extraction normalization.

    Configurable per-domain (predicate / memory_type / schema / entity / etc.):
      - seed canonicals
      - embedding model (the trained one, not ChromaDB default)
      - threshold per domain
      - learned-canonicals storage backend
      - emission of normalization events to the log
    """
    
    def __init__(self, domain: str, seed_canonicals: list[str],
                 threshold: float, embedding_model_name: str): ...
    
    def canonicalize(self, surface: str) -> tuple[str, float, str]:
        """Returns (canonical_form, similarity, kind) where kind is
        'exact' | 'collapsed' | 'novel'."""
        ...
    
    def review_pending(self) -> list[str]:
        """Returns novel canonicals awaiting user review."""
        ...
    
    def confirm(self, novel: str) -> None: ...
    def reject(self, novel: str) -> None: ...
```

Each domain instantiates this with its own configuration. Confirmation events go through the same provisional/confirmed/rejected lifecycle as other miner outputs (Part 10.6).

### 2.4 What about `general_extractor.py`?

You said this file isn't in the zip but is part of the original MemPalace. Its job is to extract typed memories (DECISIONS / PREFERENCES / MILESTONES / PROBLEMS / EMOTIONAL) from text via regex markers, mapping types to room names.

We've already decided this conflates type with topic and that the room-as-type mapping is wrong (the category error you flagged). The replacement is:

- Memory type as a **drawer property** (versioned, set by Class 1 miner), not a room.
- Topic as **emergent from event/schema participation** (the structural facet), not a separate field.
- The classification itself uses the canonicalizer pattern: the seed memory types are fixed; the miner is allowed to propose new types over time; user reviews.
- The regex-marker approach is replaced by the trained classifier (eventually) and bootstrapped with frontier-LLM extraction (cold start).

So `general_extractor.py` becomes a Class 1 miner module with the canonicalizer pattern applied to memory_type assignment. The regex logic is reusable as a fast-path heuristic feeding the LLM/classifier; the LLM/classifier is the slow path; the canonicalizer gates the output vocabulary.

---

## 3. Semantic-content and third-party-mention market protocols

This is the harder problem and the part with new architecture.

### 3.1 The current evidence model (what works now)

From `state.rs` and `acta.rs`:

- The market creator commits an `EvidenceRequirements` structure with `required_tags`, `min_tag_confidence`, a `resolution_mode`, and `pipeline_routes`.
- Devices submit `EvidenceSubmission` accounts containing an `attestation_hash` (SHA256 of the signed feature vector).
- Devices serve the signed feature vector via libp2p (`/safta/fv/1.0.0`) when the oracle requests.
- The oracle verifies StrongBox signature + attestation chain, decodes `TagOutput[]` from the feature vector, runs the formula on tag confidences/counts.
- Formula resolves; oracle emits result via Switchboard with tag-binding to the market pubkey.

This works because the resolution surface is **a fixed schema of typed tags** (`TagOutput { tag_id, confidence_bps, slot_count }`). The on-chain layer never sees content; the device produces tags from content, ships only the tag scores, and the oracle resolves on tag scores.

### 3.2 What breaks for the new market classes

**Semantic-content market** ("did the topic of X come up in any conversation this week?"): the question doesn't decompose into a fixed tag set. The "topic" is a free-form semantic concept defined at market-creation time. The device cannot enumerate all possible market topics in advance and produce per-topic tags.

**Third-party-mention market** ("was I mentioned in someone else's conversations this week?"): the evidence lives on *someone else's device*, not the asking user's. The privacy property "raw content never leaves my device" needs to extend to "even when I'm asking about someone else, *their* raw content never leaves *their* device, and *I* never even learn what was said."

Both classes share a structural property: they're **semantic queries**, not **structural queries**. They require comparing a query concept against actual content. Tag-based resolution cannot answer them because tags are pre-committed and these queries are post-hoc.

### 3.3 The protocol — semantic evidence via signed signature comparison

The architecture extends the existing pipeline rather than replacing it. The key insight: **we don't need to send semantic content anywhere**. We can compare a query embedding against a corpus of content embeddings, and produce a *semantic-match score*, all on the device that holds the content. The score is the evidence.

The flow:

```
1. Market creation
   Creator includes a `SemanticEvidenceRequirements` extension to the
   normal EvidenceRequirements:
     - query_embedding: bytes (committed at market creation)
     - query_embedding_model_hash: [u8; 32]
     - threshold: u16 (bps; minimum cosine similarity for "match")
     - time_window
     - min_match_count: u16
     - target_subject: Pubkey (for third-party mention markets;
       Pubkey::default() for semantic-content markets)
   
   The query embedding is committed on-chain via a hash; the actual
   embedding bytes are too large for on-chain storage but live in the
   market's IPFS/libp2p anchor.

2. Resolution time, oracle to device
   Oracle issues a libp2p request to the relevant device:
     - For semantic-content markets: to the user's own device.
     - For third-party-mention markets: to the device of the user
       who claims the mention happened (typically the message author).
   
   Request includes:
     - market pubkey
     - query embedding hash (commit reference)
     - time window
     - target subject (if mention market)
     - nonce

3. Device-side semantic evidence formulation
   Inside the StrongBox-protected isolated process:
     a. Verify the request signature (oracle's pubkey).
     b. Fetch the committed query embedding from libp2p, verify its
        hash matches the on-chain commit.
     c. Within the time window, compute cosine similarity between
        the query embedding and each drawer embedding in scope:
          - For semantic-content: every drawer the user captured.
          - For third-party-mention: drawers in conversations where
             target_subject was a participant (interactional facet).
     d. Count drawers with similarity >= threshold = match_count.
     e. Optionally: produce a *covariance summary* of the matching
        drawers' embeddings (mean similarity, variance, peak).
     f. Build a `SemanticEvidenceVector`:
          - match_count: u16
          - mean_similarity_bps: u16
          - peak_similarity_bps: u16
          - variance_bps: u16
          - distinct_session_count: u16
          - time_distribution_bucket_hashes: [u8; 32; 16]
            (which time buckets within the window had matches —
             coarse-grained for privacy)
       This vector contains NO content. Only aggregate statistics
       about how the device's content compares to the query embedding.
     g. StrongBox signs SHA256(SemanticEvidenceVector || nonce ||
        market_pubkey).
     h. The signed vector is served via libp2p to the oracle in the
        same way TagOutput[] is served today.

4. Oracle-side resolution
   Same pattern as the existing flow:
     - Verify StrongBox signature, attestation chain, device enrollment.
     - Verify SHA256(vector) == attestation_hash on-chain.
     - Run a `SemanticFormula` against the vector:
         match_count >= min_match_count
         AND mean_similarity_bps >= threshold
       resolves YES.
     - Encode outcome + confidence as before.

5. On-chain
   No new on-chain logic needed for the resolution itself.
   Encoding is the same value = tag * 1_000_000_000_000 + outcome * 100_000 + confidence.
   Only the on-chain commit structure changes (see §3.5).
```

### 3.4 New formula types in the registry

The formula registry (extracted in §1.4) gains new entries for semantic markets:

- `SEMANTIC_THRESHOLD` — `match_count >= min_count AND mean_sim >= threshold`
- `SEMANTIC_PEAK` — at least one drawer with `similarity >= threshold`
- `SEMANTIC_DISTRIBUTED` — matches in at least N distinct time buckets (catches "topic came up repeatedly" vs. "one isolated mention")
- `MENTION_THRESHOLD` — same as `SEMANTIC_THRESHOLD` but with `target_subject` constraint (drawers must come from interactions with that subject)

Each formula returns YES/NO/INDETERMINATE plus a confidence score, plugging into the existing `ResolutionResult` shape.

### 3.5 On-chain data structures (extension to state.rs)

Three additions, all backward-compatible (separate PDA pattern, existing markets unaffected):

```rust
/// Semantic-evidence requirements for a market.
/// Stored alongside MarketEvidence; created by an init_semantic_evidence
/// instruction at market creation time.
///
/// Seeds: [b"semantic_evidence", market.key().as_ref()]
#[account]
pub struct SemanticMarketEvidence {
    pub market: Pubkey,
    /// SHA256 commit of the query embedding bytes.
    /// Bytes themselves served via libp2p; oracle and devices verify
    /// SHA256 matches before computing similarity.
    pub query_embedding_hash: [u8; 32],
    /// Hash of the embedding model used to produce the query embedding.
    /// Devices must use the same model (or one with a signed equivalence
    /// proof; see §3.6) to produce drawer embeddings.
    pub query_embedding_model_hash: [u8; 32],
    /// Minimum cosine similarity (bps) for a drawer to "match."
    pub threshold_bps: u16,
    /// Minimum number of matching drawers.
    pub min_match_count: u16,
    /// Time window. Mirrors EvidenceRequirements.
    pub time_window_start: i64,
    pub time_window_end: i64,
    /// For mention markets: the device pubkey whose content is queried.
    /// Pubkey::default() = semantic-content market (own device).
    pub target_subject: Pubkey,
    /// Resolution formula type.
    /// 20=SEMANTIC_THRESHOLD, 21=SEMANTIC_PEAK, 22=SEMANTIC_DISTRIBUTED,
    /// 23=MENTION_THRESHOLD.
    pub formula_type: u8,
    pub bump: u8,
}

/// Submission for semantic evidence. Mirrors EvidenceSubmission
/// but the underlying signed object is a SemanticEvidenceVector
/// rather than a TagOutput[].
///
/// Seeds: [b"semantic_evidence_submission", market.key().as_ref(),
///         submitter.key().as_ref(), &[nonce]]
#[account]
pub struct SemanticEvidenceSubmission {
    pub market: Pubkey,
    pub submitter: Pubkey,
    /// SHA256 of the StrongBox-signed SemanticEvidenceVector.
    pub attestation_hash: [u8; 32],
    pub submitted_at: i64,
    pub bump: u8,
}

/// Off-chain structure (NOT on-chain). Carried in the libp2p response.
/// The on-chain layer only sees the attestation_hash.
pub struct SemanticEvidenceVector {
    pub match_count: u16,
    pub mean_similarity_bps: u16,
    pub peak_similarity_bps: u16,
    pub variance_bps: u16,
    pub distinct_session_count: u16,
    /// 16 coarse-grained time buckets within the window.
    /// Each bucket: keccak hash of (bucket_index || match_count).
    /// Lets the oracle verify temporal distribution without learning
    /// exact timing of any specific match.
    pub time_distribution_bucket_hashes: [[u8; 32]; 16],
}
```

New on-chain instructions:

```
init_semantic_evidence — creator commits the SemanticMarketEvidence PDA
                         alongside or instead of MarketEvidence.

submit_semantic_evidence — device submits SemanticEvidenceSubmission with
                            attestation_hash and StrongBox signature.
                            Mirrors submit_evidence's verification logic.
```

Resolution flow on-chain is unchanged: the orchestrator (off-chain) produces the resolution value with tag binding, the on-chain `decode_oracle_value` extracts outcome and confidence as before.

### 3.6 The model-version problem (and its solution)

The query embedding and the drawer embeddings must come from the same model (or models with a signed equivalence proof). Otherwise cosine similarity is meaningless.

The protocol handles this with `query_embedding_model_hash`:

- Each model version has a public hash (the trained-model registry from `model_registry.py`, refactored).
- Markets commit which model produced the query embedding.
- Devices that don't have that model installed reject the request.
- Devices that have a *newer* model can apply a model-equivalence transform if one is signed and available; otherwise they reject.
- The trained embedding model versions are themselves a federation concern (Part 7.3 mentions cold-start with a frontier model); the registry is shared across all participating devices.

If a device cannot produce evidence at the requested model version, the oracle treats it as "no evidence available from this submitter" — same as the existing behavior for missing tags.

### 3.7 Privacy story per market class

**Semantic-content market** (`target_subject == Pubkey::default()`):
- Query embedding leaves the market creator (committed on-chain via hash, bytes via libp2p).
- The user's own drawers stay on the user's device.
- The user's device produces a `SemanticEvidenceVector` from their own content. Aggregate statistics, no content.
- Oracle sees the vector. Resolution proceeds on aggregates.
- Privacy property: the user's drawer content never leaves their device. The oracle, switchboard, and on-chain layer learn only how aggregate-statistically the user's recent content matches the query embedding.

**Third-party-mention market** (`target_subject` is set):
- Same query embedding flow.
- Crucially: the *asking user* is not the device that produces evidence. The *target subject's* device produces evidence.
- The target subject's device evaluates: "do I have drawers from interactions with the target subject's perspective that match the query embedding?" — this is the cleanest semantic-content match scoped to interactional context.
- The asking user receives only the resolution outcome (YES/NO + confidence). They do not learn what was actually said. They learn only: "did the topic of X come up in some conversation involving you and me, in this time window."
- Privacy property: the target subject's drawer content never leaves their device. The asking user does not learn content; they learn only resolution outcome.

The harder privacy gap is what the *asking user* learns from a YES versus NO outcome. A YES tells them "this topic came up in some conversation." That's information they didn't have. The mitigation is consent: third-party-mention markets require the target subject to opt in — either at market creation (the target subject signs the market), or per-resolution (the target subject's device declines the libp2p request if not consented).

### 3.8 Composition with the existing pipeline

The existing oracle pipeline is unchanged for tag-based markets. For semantic markets:

- `EvidenceVerifyPostprocessor` gains a sibling: `SemanticEvidenceVerifyPostprocessor`. Same StrongBox attestation logic; different payload schema.
- `DeterministicResolvePostprocessor` consults the formula registry. New formula types route to the semantic formulas (§3.4); existing ones unchanged.
- `MarketEvidence` and `SemanticMarketEvidence` PDAs coexist on a market. A market can have both (tag-based confirmation *plus* semantic-content corroboration).
- StrongBox attestation is identical. The hardware-attestation chain doesn't care what's being signed; it only cares that the signing key is StrongBox-bound and the device is enrolled.
- libp2p protocol: a sibling protocol id `/safta/sev/1.0.0` for semantic-evidence-vector serving, alongside the existing `/safta/fv/1.0.0`.

### 3.9 What this means for the spec's federation pathway (Part 9)

The semantic-evidence protocol is a *narrowed instance* of the federation pathway from Part 9. Both involve:
- Encrypted libp2p transport between peers.
- Sandbox-isolated decryption and computation.
- StrongBox attestation.
- Aggregate findings emission, never content emission.

But they differ in trust topology and goal:

- **Federation pathway (Part 9)**: peer-to-peer matching for relationship discovery. Two users opt in mutually. The gigabox sandbox holds both palace projections temporarily, runs layered triangulation, emits findings to switchboard.
- **Semantic-evidence protocol (§3 here)**: oracle-to-device evidence query for market resolution. The oracle is one-way (it asks, the device answers). The device's sandbox runs the formula. The on-chain layer ratifies the outcome.

The shared infrastructure is real and worth naming:
- libp2p transport with attestation: shared.
- StrongBox-protected sandbox: shared.
- Aggregate-only findings: shared.
- Trained embedding model: shared (semantic markets and federation matching both use it).

Recommendation: factor these as a common substrate (`secure_compute_substrate`) consumed by both the federation pathway and the semantic-evidence protocol. This is one engineering effort, two consumers.

### 3.10 What still needs to be built

This appendix has identified the protocol shape and the on-chain extensions. What remains for actual implementation:

1. **Rust side**: `init_semantic_evidence`, `submit_semantic_evidence` instructions; `SemanticMarketEvidence` and `SemanticEvidenceSubmission` PDAs; new formula constants.
2. **Off-chain Python**: `SemanticEvidenceVerifyPostprocessor`, `SemanticFormula` evaluators in the registry, `/safta/sev/1.0.0` libp2p protocol handler.
3. **Device-side**: the formulator that runs inside StrongBox isolation, computes `SemanticEvidenceVector` from drawer embeddings, signs and serves.
4. **Resolvability classifier in `question_decomposer.py`** (refactored): determines whether an incoming market is tag-based, semantic-content, third-party-mention, or jury-only.
5. **Model registry hardening**: signed equivalence proofs across model versions; reject cleanly when a device can't fulfill at the requested version.
6. **Consent mechanism** for third-party-mention markets: target-subject opt-in surface (per-market or blanket).

---

## 4. Summary of decisions in this appendix

The triage:
- 5 files kept (light refactor).
- 14 files refactored (substantial rework against the new substrate).
- 12 files discarded (capability delivered differently).
- 5 kernels extracted (move to new homes).
- 6 files marked resolver-side, untouched.
- The 2 Rust files kept as reference; **extended** with semantic-evidence instructions and PDAs.

The canonicalizer principle generalized: post-extraction normalization with seed canonicals + open-world learning + threshold collapse + provisional/confirmed/rejected lifecycle. Applied to predicates (existing), memory types, schemas, entities, periods, themes, goal-state markers.

The semantic-evidence protocol: query embedding committed at market creation, served via libp2p; device-side cosine similarity inside StrongBox isolation; aggregate `SemanticEvidenceVector` signed and submitted; oracle verifies and runs formula; on-chain ratification unchanged. Composes with existing tag-based pipeline. Extends to third-party-mention markets via target_subject and opt-in consent.

The federation pathway (Part 9) and semantic-evidence protocol (§3) share an underlying secure-compute substrate that is worth factoring as common infrastructure.

---

## 5. What's still open after this appendix

These remained open from the spec and remain open after this appendix:

1. **Local-vs-shared discriminative basis** for the signature store. (Part 7 spec, §1 not addressed here.)
2. **Voice session boundary mechanism** for capture. (Out of scope here; spec deferral.)
3. **Predicate canonicalization at scale** — algorithmic specifics and review surface.
4. **Period auto-creation policy** — confirm the live-period-per-theme commitment.
5. **Facet count freeze at seven** — confirm or push back.
6. **Consent UX** for third-party-mention markets — spec the per-market opt-in surface concretely.
7. **Resolvability classifier training data** — bootstrap path before downstream feedback accumulates.

Items 1–5 belong to the spec; item 6 is new from this appendix; item 7 is new from this appendix.
