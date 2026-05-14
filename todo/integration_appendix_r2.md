# MemPalace Integration Appendix (Revision 2)

**Companion to** `mempalace_spec.md` (the 11-part architecture).
**Replaces** the first revision of this appendix in full.

**Major changes from R1:**
- Semantic-content and third-party-mention market protocols **dropped entirely** — gameable by the subject, unacceptable.
- Orchestration-glue triage **corrected**. Eight files previously marked "discarded" are reclassified as "refactored" because they encode the stacking-substrate pattern that the new architecture needs.
- **Stacking framework introduced** as a unified substrate (an OO base abstraction) that ranker stacking, inference-model stacking, miner-pass stacking, federation-matching layers, composition layers, and wake-up composer all specialize. No duplicated logic across these consumers.
- **Model attestation** added as a first-class architectural concern. Not specific to a single market type; it threads through every privacy-preserving inference path.
- **Secure compute substrate** factored as a single layer serving both cross-palace matching (federation, Part 9) and local-only inference (privacy-preserving evidence resolution). One substrate, two consumers.
- Voice capture **excluded** from this appendix's scope. App-layer concern; the mempalace cloud box receives streamed blobs.
- Several open questions resolved (decisions recorded in §6).

---

## 0. How to read this document

The spec defines the architecture; this appendix maps the existing implementation onto it. Every Python file in `predictions/` is treated as **incomplete**. The dispositions are:

- **Keep** — already aligned, minor refactors only.
- **Refactor** — solves a real problem; rework against the new substrate, often with broader applicability than current implementation.
- **Discard** — superseded by other architectural decisions.
- **Extract** — useful kernel inside that moves to a new home.

Defaults to rewrite; not attached to current implementation.

The two Rust files (`state.rs`, `acta.rs`) are kept as reference. They get extended with new instructions for **model-attestation** events and for the **stacking framework's plan commitment**, but **not** for semantic/mention markets (those are dropped).

---

## 1. The stacking framework (the architectural core)

This is the unifying abstraction we missed in the first appendix. Six different consumers in the system are doing structurally identical work:

1. **Ranker stacking**: factored multiplicative → neural cross-terms → composer-specific.
2. **Inference-model stacking** (evidence pipeline): web search → classifier → transcribe → embed → resolver.
3. **Miner pass stacking**: Class 1 streaming → Class 2 cross-drawer → Class 3 schema induction.
4. **Federation matching layers**: layer 1 structural → layer 2 derivation → layer 3 substrate.
5. **Composition layer**: drawer-selection → ordering → coherence-check → output assembly.
6. **Wake-up composer**: identity → active periods → I-ams → high-velocity → contradictions → proposals.

Each is a *configurable pipeline of pluggable steps*, with the composition itself task-specific. We do not implement these six times. We implement the framework once and have each domain specialize.

### 1.1 Base abstractions

```python
class Step(Protocol):
    """A single stage in a stack. Pure function over inputs.

    Subclasses specialize for domain (ranker step, inference step, miner
    pass, federation layer, etc.) but the framework only sees the contract.
    """
    name: str
    
    def declares(self) -> StepManifest:
        """What this step requires (input keys, features) and produces
        (output keys, attestation requirements, privacy properties)."""
        ...
    
    async def run(self, ctx: StackContext) -> StepResult:
        """Execute. Reads from ctx.inputs; writes to ctx.outputs.
        Returns StepResult with success, attestations emitted, events
        emitted, errors."""
        ...


class Stack:
    """Ordered composition of steps. Configurable per task.
    
    Not an execution engine — the multiplexer schedules step execution.
    Stack is the declared sequence and dependencies.
    """
    plan: list[Step]
    
    def validate(self) -> list[ValidationError]:
        """Check that step inputs are provided by prior outputs or by
        the initial context, etc."""
        ...
    
    async def execute(self, initial_ctx: StackContext, 
                      attestation_required: bool) -> StackResult:
        """Run steps in sequence. Each step's outputs flow into the next
        step's inputs. Emits attestation events if required."""
        ...


class StackContext:
    """Carries inputs/outputs through a stack execution.
    
    Includes:
      - inputs (set by stack initialization)
      - outputs (accumulated by step executions)
      - stance / consumer info
      - privacy mode (LOCAL_ONLY / SANDBOX / EXTERNAL)
      - attestation handle (collects per-step attestations)
    """
```

The framework is small and shared. The specialization is in the `Step` subclasses.

### 1.2 Specialization per domain

Each consumer domain has a base step class:

```python
class RankerStep(Step): ...           # ranker stack
class InferenceStep(Step): ...        # evidence pipeline
class MinerPass(Step): ...            # miner stack
class MatchingLayer(Step): ...        # federation layers
class CompositionStep(Step): ...      # composer
class WakeUpComponent(Step): ...      # wake-up
```

Each base inherits from `Step`, fills in domain-specific contract details (typed inputs/outputs, what kinds of features it consumes), and concrete implementations subclass these.

Concrete examples:

```python
class FactoredMultiplicativeRanker(RankerStep): ...
class NeuralCrossTermRanker(RankerStep): ...

class WebSearchInference(InferenceStep): ...
class WhisperTranscribeInference(InferenceStep): ...
class LocalLLMInference(InferenceStep): ...

class Class1DrawerEnrichment(MinerPass): ...
class Class2CrossDrawerAggregation(MinerPass): ...
class Class3SchemaInduction(MinerPass): ...

class StructuralAlignmentLayer(MatchingLayer): ...
class DerivationChainCompatibilityLayer(MatchingLayer): ...
class SubstrateLevelComparisonLayer(MatchingLayer): ...
```

All of these go through the same `Stack.execute()` machinery. The differences are in their `declares()` and `run()` methods.

### 1.3 Plan commitment for evidence pipelines

For evidence resolution specifically, the stack plan is **negotiated at market qualification time** and committed on-chain:

1. RPC returns the available pipeline options for a question (JSON, with explanation per option for the frontend).
2. The market creator picks one option as part of the on-chain market creation transaction.
3. The pipeline_routes commitment in `EvidenceRequirements` is the committed plan.
4. The oracle off-chain executes exactly the committed plan; deviation is a violation.

This is already structurally supported by `pipeline_routes` in the Rust state. The off-chain side just needs to use a `Stack` whose plan is loaded from the on-chain commitment, then execute it through the framework.

For non-evidence stacks (ranker stacks, miner stacks, federation layers, etc.), plan composition happens at runtime by the consumer; no on-chain commitment.

### 1.4 Attestation as a step decorator

Any step that runs ML inference can require model attestation:

```python
class AttestedStep(Step):
    """Wraps a Step with model-attestation discipline.
    
    Before execution: emits 'model_loaded' event with weights_hash
    and StrongBox-bound key signature.
    After execution: emits 'model_inference_completed' event with
    SHA256(input || output || weights_hash || timestamp), signed.
    
    For LOCAL_ONLY privacy mode, all steps must be AttestedStep.
    For SANDBOX mode, steps inside the sandbox must be AttestedStep.
    For EXTERNAL mode, attestation is informational (external API can't
    be hardware-attested).
    """
    
    def __init__(self, inner_step: Step, model_id: str): ...
```

The attestation events feed the on-chain layer for verification. See §3.

---

## 2. Triage of the `predictions/` files (revised)

### 2.1 Keep (light refactor)

- **`predicate_canonicalizer.py`** — Keep. Generalized to the canonicalizer pattern in §4. Storage moves into the master log; learned canonicals emit events. See §4 for algorithmic and review-surface specifics.
- **`kg_sketch.py`** — Keep, retarget. MinHash-over-typed-walks signature. Becomes a derived representation feeding **federation matching layer 1**. Reads from current_nodes/current_edges views, walk weighting respects edge kinds.
- **`manifold_index.py`** — Keep, retarget. Path-weight EMA, uncertainty estimation, tunnel discovery. Vocabulary updated (theme/period/event), feedback through events, tunnels through the canonicalizer + provisional/confirmed/rejected lifecycle.
- **`watermark.py`** — Keep as-is.
- **`tee_attest.py`** — Keep as-is. Becomes the foundation for the `AttestedStep` decorator (§1.4).

### 2.2 Refactor (substantial rework, includes the previously-misclassified orchestration files)

The eight orchestration files I previously discarded are reclassified here.

- **`pipeline.py`** — Refactor to **the stacking framework's runtime**. The current orchestrator (preprocessors → processors → postprocessors) generalizes to `Stack.execute()`. Preprocessors and postprocessors become specialized `Step` subclasses with declared input/output contracts.
- **`execution_plan.py`** — Refactor. The `ExecutionPlan` data structure is the right shape for a committed stack plan. Extends to ranker plans, miner plans, federation layer plans, composition plans. Adds: model attestation requirements per step, privacy mode, plan commitment hash.
- **`step_result.py`** — Refactor as the unified `StepResult` shape across all stacks. Adds attestation pointers, error categorization, partial-result handling.
- **`dispatch.py`** — Refactor. Becomes the dispatch surface for `Step.run()` calls. Routes to the multiplexer, handles privacy-mode constraints (LOCAL_ONLY → reject external models), produces attestation events.
- **`gate.py`** — Refactor. Generic precondition gate. Used by all stacks (not just evidence) to check policy constraints before step execution. Time-window, submission-cap, privacy-mode, and consent are all gates.
- **`evidence_gate.py`** — Refactor as a specialization of the general gate.
- **`validate.py`** — Refactor as inter-step validation (output of step N matches expected input of step N+1).
- **`compose.py`** — Refactor as a `CompositionStep` base class, used by the composer in spec Part 5.4 and elsewhere.
- **`evaluate.py`** — Refactor as per-step evaluation hooks (test outputs against expected, emit metrics events).
- **`encode_result.py`** — Refactor lightly. The off-chain side produces structured outputs; encoding-to-on-chain-value is in Rust. Slim Python wrapper retained.
- **`resolve.py`** — Refactor as a specialized `Stack` (the resolution stack) with its own composition rules.
- **`engagement_vector.py`** — Refactor. The static `cosine_similarity` is replaced by ranker dispatch; engagement vectors become a feature input to rankers, not a self-scoring object. Storage moves into derived views. Covariance structure is a new computation over engagement-vector history.
- **`question_decomposer.py`** — Refactor. Becomes the **resolvability-and-pipeline-options surface**. Returns to the frontend (via RPC) the JSON of available pipeline paths for a market question, with explanations. Market creator's choice is committed on-chain. Runtime LLM call (Claude or local equivalent); no separate trained classifier needed at bootstrap.
- **`relational_extract.py`** — Refactor. Assertion-emit path. Output is event emissions for assertion nodes plus their edges (asserted_subject, asserted_object, derived_from). 8-part frame enforced. Predicates run through the canonicalizer.
- **`engagement_update.py`** — Refactor as a Class 1 miner pass.
- **`palace_update.py`** — Refactor. Most of the file collapses into thin event-emit helpers; mutation-style code disappears.
- **`market_state.py`** — Refactor. Off-chain mirror via event log + DDflow views.
- **`evidence_verify.py`** — Refactor. StrongBox attestation verification logic is correct; data shapes change to event payloads. Becomes one `Step` in the resolution stack.
- **`deterministic_resolve.py`** — Refactor. The 19 formula types extract into the formula registry (§2.4). The resolver becomes a `Step` that loads a formula and evaluates against an evidence summary.
- **`resolution_feedback.py`** — Refactor. Emits `feedback_recorded` events keyed to interpretation versions active at resolution. Credit-assignment chain root.
- **`embedder.py`**, **`embed.py`** — Refactor. Local trained model replaces ChromaDB default. Embedding writes are events.
- **`model_registry.py`** — Refactor. Model metadata, version tracking, weights-hash registration. Adds: model-class taxonomy, training-state per model, attestation-key bindings.
- **`device_context.py`** — Refactor. Capability advertisement.

### 2.3 Discard (capability genuinely delivered differently)

Smaller list than R1:

- **`dag.py`** — Discard. DDflow over the event log provides DAG-shaped state; no need for a separate Python DAG module.
- **`main.py`** — Discard. Replaced by the multiplexer's main loop.
- **External-pipeline integrations**: `doc_verify.py`, `web_search.py`, `multimodal.py`, `visual_classify.py`, `classify.py`, `transcribe.py` — keep as **resolver-side modules**, not part of the palace. They become `InferenceStep` implementations (§1.2). Untouched as files; their integration point is the stacking framework.
- **`entity_embedding_export.py`** — Folded into the federation pathway (signature projection). The standalone export disappears; the logic lives in the matching layer.

### 2.4 Extract

- **The 19 formula types in `deterministic_resolve.py`** → `formula_registry.py`. The deterministic resolver becomes a runner over the registry.
- **MinHash sketch builder from `kg_sketch.py`** → factor as part of signature-store derivation; standalone module retained.
- **Welford's online stats from `engagement_vector.py`** → `streaming_stats.py` utility.
- **Seed-canonical-plus-learned pattern from `predicate_canonicalizer.py`** → general canonicalizer in §4.
- **libp2p protocol negotiation** → `secure_compute_substrate/transport.py` (§3).

---

## 3. The secure compute substrate

This is the unification noted in your message: the same isolated environment that runs cross-mempalace matching also runs privacy-preserving local-only inference for evidence resolution. One substrate, two consumers.

### 3.1 What the substrate provides

```
secure_compute_substrate/
  attestation.py    — model attestation primitives (StrongBox-bound key,
                       weights_hash registration, inference attestation
                       event emission)
  transport.py      — libp2p protocols for: foreign-palace transfer
                       (federation), evidence response (resolution),
                       attestation event delivery (on-chain)
  sandbox.py        — sandbox lifecycle: provision, decrypt-inside-boundary,
                       run, emit findings/results, tear down, destroy keys
  enclave_run.py    — execution interface for Step.run() inside sandbox
                       boundary; LOCAL_ONLY enforcement
  attest_chain.py   — verification of attestation chains (StrongBox to
                       device enrollment to enrolled-model registry)
```

### 3.2 Consumers

**Consumer 1: Federation pathway (spec Part 9).** Two palaces matching for collaboration discovery. Sandbox provisioned with both palace projections; layered triangulation runs as a stack of `MatchingLayer` steps; findings emitted to switchboard. Sandbox destroyed.

**Consumer 2: Privacy-preserving evidence resolution.** A market with privacy mode set to LOCAL_ONLY runs the entire evidence stack inside the sandbox. Each `InferenceStep` is wrapped as `AttestedStep`. The committed `ExecutionPlan` runs locally; attestation events accumulate; only the resolution result (with attestation chain) crosses out of the sandbox.

Both consumers go through the same `Stack.execute()` framework. The substrate enforces the privacy-mode invariants regardless of consumer.

### 3.3 Model attestation specifics

Hardware-bound, mirroring the StrongBox device-attestation pattern from `acta.rs`:

- Each enrolled model has a StrongBox-bound signing key. Generated at first model load on the device.
- The `model_registry.py` tracks (model_id, weights_hash, signing_pubkey, enrollment_signature). The enrollment signature commits the (model_id, weights_hash) pair under the device's enrolled key.
- At inference time, an `AttestedStep` produces an attestation event:
  ```
  attestation_payload = SHA256(
      model_id || weights_hash || input_hash || output_hash || nonce || step_id
  )
  signature = StrongBox_sign(attestation_payload, model_signing_key)
  ```
- The attestation event is emitted to the log and (for evidence resolution) submitted on-chain alongside the evidence submission.
- The on-chain side verifies: device enrollment is valid, model is registered to that device, signature chain checks out.

This is **the same pattern** as device-evidence attestation; the substrate is generic and serves both.

### 3.4 New on-chain instructions

Mirroring the existing `enroll_device` and `revoke_enrollment` pattern:

```rust
/// Register a model that this device is allowed to run.
/// The (model_id, weights_hash, enrollment_signature) tuple is committed.
/// At resolution time, attestation events for this model are accepted only
/// if signed by the corresponding key.
enroll_model(device_pubkey, model_id, weights_hash, signature)

/// Revoke a model enrollment. Subsequent inference attestations from
/// this model on this device are rejected.
revoke_model_enrollment(device_pubkey, model_id, signature)

/// Submit an inference attestation alongside the evidence submission.
/// The on-chain layer verifies the signature chain. Evidence resolution
/// for LOCAL_ONLY privacy markets requires every inference step to have
/// a verifiable attestation.
submit_inference_attestation(market, submitter, attestation_payload, signature)
```

These add to `state.rs` and `acta.rs` without breaking existing markets. Markets that don't use LOCAL_ONLY mode don't require attestations. Markets that do, require them at every inference step in the committed pipeline.

### 3.5 What this is *not*

It's not zk-proof of inference. The cost would be prohibitive at the model sizes we're dealing with. The attestation is hardware-bound to the device's StrongBox key, which proves the inference happened *on this device with these weights* but not that the device is honest. This is the same trust model as device-evidence attestation: you trust the hardware-bound signing chain, not the device's behavior beyond that.

For markets where this trust model isn't sufficient, jury escalation is the fallback (existing MODE_JURY_ONLY and MODE_AI_PLUS_JURY paths in the current Rust). No change there.

### 3.6 App-side integrity check

Per your note: the app must produce periodic StrongBox attestations independent of evidence submissions and receive smart-contract confirmations. If the chain breaks, the contract returns staked funds (in case the app was spoofed) and locks the user out.

This becomes a separate periodic instruction:

```rust
/// Heartbeat attestation — proves the app is still running on the device
/// with the enrolled key. If absent for >N hours, the contract triggers
/// the lockout-and-refund flow.
submit_heartbeat_attestation(device_pubkey, slot, signature)

/// Triggered by the contract when heartbeat chain is broken. Returns
/// staked funds to the user and locks the device until re-enrollment.
trigger_app_integrity_lockout(device_pubkey)
```

The app schedules these on a fixed cadence; missing N consecutive heartbeats → lockout. This is end-to-end integrity for the human-side of the system, separate from per-market evidence flows.

---

## 4. The canonicalizer pattern (generalized, with algorithmic specifics)

### 4.1 The pattern (recap)

From `predicate_canonicalizer.py`: seed canonicals → fast-path exact match → slow-path embedding similarity → threshold collapse → open-world novel acceptance → cache.

### 4.2 Generalization to multiple domains

A `Canonicalizer` class configurable per domain. Applied to:

- Predicates (existing)
- Memory types
- Schema names
- Entity resolution (alias merging)
- Period names
- Theme names
- Goal-state markers

Each domain has its own seed set, threshold, and review surface. All share the same machinery.

### 4.3 Algorithmic specifics

**Threshold per domain.** A few concrete starting values:

| Domain | Threshold | Reasoning |
|---|---|---|
| Predicates | 0.85 | Existing setting; reasonable for relational verbs. |
| Memory types | 0.90 | Small fixed set (7 seeds). Want strict collapse to avoid sprawl. |
| Schema names | 0.78 | Schemas are longer phrases with more variation; lower threshold catches the same schema across different surface forms. |
| Entity aliases | 0.92 | Stricter — false-merge of distinct people is costly. Low-threshold cases should require user confirmation explicitly. |
| Period names | 0.80 | Periods drift in naming; want collapse but not too aggressive. |
| Theme names | 0.85 | Mid. |
| Goal markers | 0.75 | Loose; goal markers are short and noisy. |

These are configurable per palace (user can override) and per domain.

**Novel-canonical promotion.** Currently any below-threshold input becomes a canonical. At scale this produces sprawl. New rule:

- Below-threshold inputs go into a *candidate pool* (not promoted to canonical immediately).
- The pool clusters incoming candidates at a *higher threshold* (the domain's threshold + 0.05).
- A cluster is promoted to canonical when it has at least N members (default N=3) and the cluster is stable across at least M Class 2 miner passes (default M=2).
- Promotion is recorded as an event with the cluster members for audit.

This prevents one-off typos from creating canonical sprawl.

**User review surface.** An MCP tool `mempalace_canonicalizer_pending(domain)`:

- Returns: the candidate pool for the domain, with cluster groupings and example uses for each member.
- The user can: confirm a cluster (promote to canonical with a chosen name), reject a cluster (mark members as not a canonical, suppress proposal in future), rename the canonical, or merge two existing canonicals (collapse one into the other with reversibility).

**Reversibility.** All canonicalization decisions are events. Wrong collapse → emit `canonicalization_reverted` event with new mapping. The canonical history is queryable; the current canonicalization is the latest non-reverted mapping.

**Storage.** Canonical sets and learned mappings are derived views over the event log, not separate tables. Cost is small (canonicals are short strings + small embeddings).

### 4.4 What about general_extractor.py

(File not in the zip but referenced.) Becomes a Class 1 miner pass with the canonicalizer applied to memory_type assignment. Regex-marker logic is reusable as fast-path heuristics; LLM/local-classifier is slow path; canonicalizer gates the output vocabulary. The current room-as-type mapping is dropped entirely — type is a versioned drawer property; topic is emergent from event/schema participation.

---

## 5. Markets beyond tag-only

The first appendix proposed semantic-content and third-party-mention markets. **Both dropped** — gameable by the subject. If I know the market exists I can game my own behavior.

What survives from the original ambition:

### 5.1 Constraints any non-tag market type must meet

- **Subject cannot determine resolution favorably by their own action alone.** If knowing the market exists lets the subject resolve it in their preferred direction by choosing how to behave, the market is gameable.
- **Resolution path uses evidence the subject cannot manipulate** (or manipulating it has high cost relative to the bet).

### 5.2 Candidate market shapes (worth considering, not yet committed)

These are starting points for design conversation, not committed protocols:

**5.2.1 Commit-reveal of the formula.** The market commits a SHA256 of the resolution formula at creation; the formula reveals at resolution. The subject knows the topic but not the threshold/conditions, so they can't precisely game the resolution direction. *Attack surface*: the subject can over-comply (do more of the relevant behavior) or under-comply (do less), depending on which direction they want, since the formula's existence range is bounded. Whether this is acceptable depends on how tight the formula's resolution surface is.

**5.2.2 Multi-witness markets.** Resolution requires evidence from N independent enrolled devices that interacted with the subject. The subject controls their own device; gaming N other devices is harder. *Attack surface*: collusion. Mitigated if witnesses are randomly selected from the subject's interaction graph and don't know they're providing evidence (they just respond to evidence requests as enrolled devices do today).

**5.2.3 Behavioral signature markets.** Resolution against the subject's signature shift along a specified axis. Signatures change slowly; gaming a specific signature shift requires sustained behavior change over the market window. *Attack surface*: large bets justify the cost of sustained gaming. Probably needs bet-size limits.

**5.2.4 External-evidence markets.** Resolution against signed third-party content (web search, court records, public statements). Doesn't touch palace content. Already supported as MODE_EXTERNAL — these would extend it with the formula registry's tag-formula equivalents adapted to external content.

I want your read on which of these (if any) are worth pursuing before designing protocols. Each has its own attack surface that needs to be mapped before commitment.

### 5.3 What's not changing in the on-chain Rust

`state.rs` and `acta.rs` keep their current shape. The semantic-evidence PDAs proposed in R1 are dropped. The model-attestation instructions from §3.4 are added (orthogonal to market types — they apply to any LOCAL_ONLY market). The heartbeat-attestation instructions from §3.6 are added. No semantic-content or mention-market PDAs.

---

## 6. Open questions — resolutions and remaining

### 6.1 Resolved this round

- **Local-vs-shared discriminative basis** → **local-per-palace baseline**. Trained embedding model is the shared reference frame; discriminative basis is local geometry over those embeddings. Federated baseline is an optional later layer.
- **Where signature extraction runs** → **on the user's mempalace cloud box**. The mobile app is a thin client. Signature extraction has no special location; it's a derived representation in the user's own palace, computed by the multiplexer.
- **Period auto-creation policy** → **(b) live-period-per-theme**. Drawers without explicit period attach to a perpetually-open period for the theme; Class 2 miner retroactively splits.
- **Resolvability classifier bootstrap** → **runtime LLM at qualification**. No separate trained classifier; the qualification path already does the LLM call as part of negotiating pipeline options with the market creator.
- **Stack composition language** → **negotiated at qualification, committed on-chain via pipeline_routes**. The off-chain side executes the committed plan. Already structurally supported.
- **Secure compute substrate** → **factored as a single layer (§3) serving both federation matching and local-only inference**.

### 6.2 Resolved with caveat

- **Facet count** → **6 facets** (collapsing interactional into structural as a typed field, keeping paralinguistic and acoustic separate because their consumers differ). Six facets: verbatim, acoustic, semantic embedding, paralinguistic, structural (with interactional as a typed field), state context. Confirm before file inventory.

### 6.3 Still open

- **Voice session boundary** → **out of scope for the appendix**. Voice capture is an app-layer concern. App streams blobs to mempalace cloud box; the boundary mechanism (VAD, silence threshold, etc.) is the app's responsibility. Mempalace receives blobs and treats them as drawer inputs.
- **Predicate canonicalization specifics** → **resolved in §4.3 with concrete thresholds, novel-canonical promotion, review surface, reversibility**. Should be implementable as specified.
- **Markets beyond tag-only** → **open**. Four candidate shapes in §5.2; need your read on which to pursue.
- **App-side integrity heartbeat cadence** → **how often does the app produce heartbeat attestations?** Hourly probably reasonable; I don't know your battery constraints.

### 6.4 Newly open (from this round)

- **Multi-witness market collusion mitigation** — if §5.2.2 is pursued, how are witnesses selected to make collusion costly?
- **Behavioral-signature market bet limits** — if §5.2.3 is pursued, what bet-size cap makes gaming uneconomical?
- **Heartbeat lockout grace period** — if heartbeat chain breaks, how long before lockout triggers?

---

## 7. Original mempalace files requiring no changes

From the original `mempalace-develop` codebase (separate from `predictions/`):

**Definitely preserve as-is:**
- `LICENSE`, `CHANGELOG.md`, `MISSION.md`, `SECURITY.md`, `CONTRIBUTING.md`, `ROADMAP.md`, `AGENTS.md` — project-level documentation. Some content updates eventually but file shape stays.
- `README.md`, `CLAUDE.md` — entry-point docs; update content as architecture evolves.
- `assets/`, `landing/`, `website/` — frontend / marketing.
- `examples/` — preserve.
- `benchmarks/` — preserve structure; benchmark contents may need API updates.
- `tests/` — preserve structure; test contents will be largely rewritten against new substrate.
- `hooks/`, `.claude-plugin/` — preserve config; the scripts call new MCP tool surface.
- `docs/` — preserve; update content per architecture.
- `pyproject.toml`, `uv.lock` — preserve shape; deps update.
- `integrations/` — preserve.
- `openarena-claim.txt` — preserve.

**Update lightly (config / deps only):**
- The `mempalace/__init__.py` and `mempalace/__main__.py` — light update for new entry points.
- Configuration loading, logging, telemetry modules within `mempalace/` — preserve patterns; update what they emit (events instead of direct writes).

**Replaced wholesale:**
- All other `mempalace/*.py` files — substantial rewrite. The substrate change (SQLite → log+DDflow) and the API change mean most file *internals* are new even when external contracts are similar.

A precise file-by-file list of the original codebase will be in the file inventory document, before code generation.

---

## 8. Path forward

This appendix supersedes the first revision. The next steps:

1. **Confirm the facet count** (6 versus 7).
2. **Review the candidate market shapes in §5.2** — which (if any) to design protocols for.
3. **Resolve the heartbeat cadence and lockout grace period** (§6.3, §6.4).
4. **Produce the file inventory** — every module in the new system, what it does, what it imports, what events it emits and consumes. This is one document, your review before code generation.
5. **Then cook the files**, organized by spec part, in batches with checkpoints.

The 11 spec parts plus this appendix plus the stacking framework give the structure. The file inventory makes it concrete. Code generation follows.

