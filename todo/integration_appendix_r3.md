# MemPalace Integration Appendix (Revision 3)

**Companion to** `mempalace_spec.md`.
**Replaces** R1 and R2 in full.

This revision absorbs the architectural corrections from the recent rounds:
- Facets revised to 5 (verbatim, acoustic, semantic embedding, structural, social). Paralinguistic moved to interpretation layer. Interactional folded into structural as a typed property.
- Goals consistently treated as edges, not as goal-set properties.
- Resolvability classifier promoted to first-class component (not bootstrap-deferred).
- Review surface is the app's daily/weekly mode (with a derived `pending_review` view, an MCP programmatic surface, and the UI consumer).
- Signatures narrowed to two legitimate uses: self-baseline tracking and triage indicator. The "unusual axes alignment as primary match signal" framing is dropped.
- Triage feedback loop with cooldown + cap to prevent infinite oscillation.
- Security: no SGX/TEE. Layered defense via hardware-bound key store (Android StrongBox / iOS Secure Enclave), enrollment-gated participation, at-rest disk encryption with idle key zeroing, signed audit log, binary integrity attestation, code/data volume separation.
- Per-session keys for federation transfers; clarified what a session is.
- Ranker isolation: process boundary, pure-function contract, signed code, behavioral monitoring, capability-restricted API, trusted aggregator combining outputs.
- Two-systems consolidation pattern from Conway named explicitly; affects naming and orphan-assertion semantics.
- Iterative handle refinement (`mem_refine`) added to the handle protocol.
- Substrate-verification flag on the handle protocol; span-pointer provenance on `derived_from` edges.
- Conway/KisMATH extras integrated: hierarchical retrieval preference, exploration-vs-exploitation tunable on rankers, fork-significance feature on events, discourse-structure carrying load at deeper alignment levels.
- Markets beyond tag-only narrowed: external-evidence, behavior-vs-baseline, multi-witness (without random sampling), subject-blind formula. Counterparty-attestation dropped (collusion mitigation infeasible).
- Match-request abuse vector addressed: rate-limiting, idempotent matching against (requester, target, window) with cached findings, economics.
- Switchboard / mempalace relationship explicitly defined.
- Federation slice protocol specified: discovery, manifest fetch, match request, slice transfer, findings emission, heartbeat. libp2p protocol IDs named.
- Privacy framing reframed: exposure inside trusted boundaries fulfilling stated intention is acceptable; "minimize data exposure absolutely" was the wrong framing.
- Cross-platform attestation: Android StrongBox AND iOS Secure Enclave both supported; on-chain verifier accepts either chain.
- Phone-off graceful degradation: TTL'd decryption keys + read-only mode + integrity lockout for sustained outages.

This document is meant to be the last design document before the file inventory and code generation. Subsequent corrections will land as targeted additions, not full revisions.

---

## 0. How to read this document

The `mempalace_spec.md` defines the architecture in 11 parts. This appendix maps existing implementation onto it and adds detail where the spec was thin.

Every Python file in `predictions/` is treated as **incomplete**. Triage dispositions:

- **Keep** — already aligned, minor refactors only.
- **Refactor** — solves a real problem; rework against the new substrate.
- **Discard** — superseded by other architectural decisions.
- **Extract** — useful kernel inside that moves to a new home.

The two Rust files (`state.rs`, `acta.rs`, `entra.rs`) are kept as reference. They get extended with new instructions for **model-attestation events**, **heartbeat attestation**, **enrollment for cross-platform devices**, and **matched-against ledger entries**. They are *not* extended with semantic-content or third-party-mention market PDAs (those market types remain dropped per R2).

Default disposition is rewrite; not attached to current implementation.

---

## 1. The stacking framework

The unifying abstraction. Six different consumers in the system are doing structurally identical work:

1. **Ranker stacking**: factored multiplicative → neural cross-terms → composer-specific.
2. **Inference-model stacking**: web search → classifier → transcribe → embed → resolver.
3. **Miner pass stacking**: Class 1 streaming → Class 2 cross-drawer → Class 3 schema induction.
4. **Federation matching layers**: layer 1 structural → layer 2 derivation → layer 3 substrate.
5. **Composition layer**: drawer-selection → ordering → coherence-check → output assembly.
6. **Wake-up composer**: identity → active periods → I-ams → high-velocity → contradictions → proposals.

Each is a configurable pipeline of pluggable steps; the composition itself is task-specific. We implement the framework once; each domain specializes.

### 1.1 Base abstractions

```python
class Step(Protocol):
    name: str
    
    def declares(self) -> StepManifest: ...
        # Inputs required, outputs produced, attestation requirements,
        # privacy properties, capability requirements
    
    async def run(self, ctx: StackContext) -> StepResult: ...


class Stack:
    plan: list[Step]
    
    def validate(self) -> list[ValidationError]: ...
    async def execute(self, initial_ctx: StackContext, 
                      attestation_required: bool) -> StackResult: ...


class StackContext:
    inputs: dict       # set by stack init
    outputs: dict      # accumulated by step executions  
    stance: Stance     # consumer / cognitive task
    privacy_mode: PrivacyMode  # LOCAL_ONLY | SANDBOX | EXTERNAL
    attestation_handle: AttestationHandle  # collects per-step attestations
```

Per-domain specialization is via subclassing Step.

### 1.2 Trusted aggregator

When stacks combine outputs from multiple steps (especially rankers), the combination is performed by a **trusted aggregator** in the daemon's core, not by any individual step. This prevents a malicious step from manipulating the combination — for example, a rogue ranker returning all-zeros doesn't zero out the stack because the aggregator gates the combination logic. See §6 for the broader ranker-isolation story.

### 1.3 Plan commitment for evidence pipelines

For evidence resolution, the stack plan is negotiated at market qualification time and committed on-chain via the existing `pipeline_routes` field. The off-chain side executes the committed plan; deviation is a violation. For non-evidence stacks (rankers, miners, etc.), composition happens at runtime by the consumer.

### 1.4 Attestation as a step decorator

```python
class AttestedStep(Step):
    """Wraps a Step with model-attestation discipline.
    
    Before execution: emits 'model_loaded' event with weights_hash
    and hardware-bound signing-key signature.
    After execution: emits 'model_inference_completed' event with
    SHA256(input || output || weights_hash || timestamp || step_id), signed.
    """
```

For LOCAL_ONLY privacy markets, all steps must be `AttestedStep`. For SANDBOX, steps inside the sandbox boundary must be. For EXTERNAL, attestation is informational.

---

## 2. Triage of `predictions/` files

### 2.1 Keep (light refactor)

- **`predicate_canonicalizer.py`** — Generalized to the canonicalizer pattern (§4). Storage moves to log; learned canonicals emit events.
- **`kg_sketch.py`** — MinHash-over-typed-walks signature. Becomes a derived representation feeding federation matching layer 1.
- **`manifold_index.py`** — Path-weight EMA, uncertainty estimation, tunnel discovery. Vocabulary updated to theme/period/event.
- **`watermark.py`** — Keep as-is.
- **`tee_attest.py`** — Keep; foundation for the `AttestedStep` decorator (§1.4) and the cross-platform attestation chain (§6.2).

### 2.2 Refactor

- **`pipeline.py`** — Stacking framework runtime. Generalizes to `Stack.execute()`.
- **`execution_plan.py`** — Committed stack plans across all consumers. Adds: per-step attestation requirements, privacy mode, plan commitment hash.
- **`step_result.py`** — Unified `StepResult` with attestation pointers, error categorization.
- **`dispatch.py`** — Step.run() dispatch surface routing to multiplexer; privacy-mode constraints; attestation events.
- **`gate.py`**, **`evidence_gate.py`** — Generic precondition gates used by all stacks (time-window, submission-cap, privacy-mode, consent, rate-limiting).
- **`validate.py`** — Inter-step validation.
- **`compose.py`** — `CompositionStep` base class.
- **`evaluate.py`** — Per-step evaluation hooks (test outputs, emit metrics events).
- **`encode_result.py`** — Light wrapper for off-chain → on-chain encoding.
- **`resolve.py`** — Specialized `Stack` (resolution stack).
- **`engagement_vector.py`** — Static `cosine_similarity` replaced by ranker dispatch. Engagement vectors become a feature input. Storage in derived views. Covariance structure computed from history.
- **`question_decomposer.py`** — Now the **resolvability classifier** (first-class component, §3). Returns to the frontend the JSON of available pipeline paths plus a resolvability classification: `PUBLIC_LLM_RESOLVABLE` | `PRIVACY_PRESERVING_REQUIRED` | `JURY_ONLY` | `NOT_RESOLVABLE`. Bootstrap from frontier LLM; transition to local trained classifier as outcomes accumulate.
- **`relational_extract.py`** — Assertion-emit path with 8-part frame. Predicates run through canonicalizer.
- **`engagement_update.py`** — Class 1 miner pass.
- **`palace_update.py`** — Thin event-emit helpers; mutation-style code disappears.
- **`market_state.py`** — Off-chain mirror via event log + DDflow views.
- **`evidence_verify.py`** — StrongBox/Secure Enclave attestation verification. Becomes a `Step` in the resolution stack.
- **`deterministic_resolve.py`** — Formula extraction (§2.4); resolver becomes a step.
- **`resolution_feedback.py`** — Emits `feedback_recorded` events keyed to interpretation versions active at resolution.
- **`embedder.py`**, **`embed.py`** — Local trained model replaces ChromaDB default.
- **`model_registry.py`** — Model metadata, version tracking, weights-hash registration. Adds: model-class taxonomy, training-state, attestation-key bindings, **signed code verification for downloaded rankers** (§6).
- **`device_context.py`** — Capability advertisement; cross-platform attestation chain.

### 2.3 Discard

- **`dag.py`** — DDflow over event log replaces.
- **`main.py`** — Multiplexer main loop replaces.
- **External-pipeline integrations** (`doc_verify.py`, `web_search.py`, `multimodal.py`, `visual_classify.py`, `classify.py`, `transcribe.py`) — Resolver-side, not palace-side. Become `InferenceStep` implementations.
- **`entity_embedding_export.py`** — Folded into federation pathway.

### 2.4 Extract

- **19 formula types from `deterministic_resolve.py`** → `formula_registry.py`.
- **MinHash sketch builder** → signature-store derivation.
- **Welford's online stats** → `streaming_stats.py`.
- **Seed-canonical-plus-learned pattern** → general canonicalizer (§4).
- **libp2p protocol negotiation** → `secure_compute_substrate/transport.py` (§7).

---

## 3. The resolvability classifier and the switchboard / mempalace relationship

### 3.1 Switchboard and mempalace

- **Mempalace** is the user's cloud box. Holds the master log, runs the multiplexer, hosts rankers, runs the secure compute substrate.
- **Switchboard** is the oracle network resolving prediction markets. Switchboard nodes are compute providers volunteering to run resolution jobs.
- A mempalace **can volunteer to also run as a switchboard node** — contributing compute to resolution jobs that aren't its own user's data. This is the thin compute layer: qualification logic, external-pipeline integrations, evidence formulators for markets where the subject's mempalace is the witness.
- A mempalace is *always* its own user's palace; it can *additionally* run as a switchboard node.
- Some markets pin resolution to specific mempalaces (the subject's own device for subject-blind formula markets, §5).

### 3.2 Resolvability classification

The market creator submits a question via `question_decomposer`. The classifier returns one of:

- **`PUBLIC_LLM_RESOLVABLE`** — Claude or equivalent at qualification can both classify and resolve. Resolution path uses public LLM; no palace content involved.
- **`PRIVACY_PRESERVING_REQUIRED`** — resolution requires palace content; must run in LOCAL_ONLY or SANDBOX privacy mode. Switchboard nodes that are also enrolled mempalaces can host the resolution; public-LLM nodes cannot.
- **`JURY_ONLY`** — automated resolution insufficient; jury escalation required (existing MODE_JURY_ONLY).
- **`NOT_RESOLVABLE`** — the question can't be resolved by the available evidence model; market creation rejected.

The classifier is a first-class component, not bootstrap-deferred. Two consumer paths:
- **Markets**: classifies at qualification time.
- **Montage layer**: classifies whether a draft pipeline can complete using only privacy-preserving inference, or whether public LLM steps are required. Same trained classifier, different consumer.

Bootstrap: cold-start uses frontier LLM with synthetic training data (generated questions + known correct routings). As real outcomes accumulate, the classifier fine-tunes against actual resolution outcomes. The trained classifier runs locally for privacy-preserving classifications; the frontier LLM is used only for the `PUBLIC_LLM_RESOLVABLE` path.

### 3.3 Match-request abuse vector and idempotent matching

Match requests can be a DoS vector: an attacker floods the daemon with match requests, exhausting compute. Mitigations:

- **Rate-limiting per requester**: token-bucket per enrolled key. Excess requests are rejected.
- **Idempotent matching against (requester, target, time-window)**: once a match has been run, the result is cached for some window (configurable; default 7 days). Re-requests within the window return cached findings.
- **Matched-against ledger** (on-chain or in derived view): records prior matches per requester. New requests check the ledger; recent matches return cached findings; stale entries prune by TTL.
- **Stake or fee per request**: requester deposits a small stake. Refunded if the match was useful (consumer reports back); forfeited if abusive. This is an on-chain mechanism added via a new instruction: `submit_match_request` requiring stake; `claim_match_refund` after consumer acknowledgment.
- **Cache + ledger are derived representations** subscribed to match-result events; pruning is automatic.

The daemon's behavior on a match request:
1. Verify enrolled-key signature on requester's request.
2. Check rate-limit; reject if exceeded.
3. Check idempotency cache; if recent match exists for (requester, target, window), return cached findings.
4. Otherwise, schedule sandbox session per §7.
5. Update matched-against ledger on completion.

---

## 4. Canonicalizer pattern (with algorithmic specifics)

### 4.1 Pattern

Seed canonicals → fast-path exact match → slow-path embedding similarity → threshold collapse → open-world novel acceptance → cache. From `predicate_canonicalizer.py`, generalized.

### 4.2 Domain instances and thresholds

| Domain | Threshold | Notes |
|---|---|---|
| Predicates | 0.85 | Existing setting; relational verbs. |
| Memory types | 0.90 | Small fixed set; want strict collapse. |
| Schema names | 0.78 | Longer phrases with more variation. |
| Entity aliases | 0.92 | False-merge of distinct people is costly. |
| Period names | 0.80 | Periods drift in naming. |
| Theme names | 0.85 | Mid. |
| Goal markers | 0.75 | Loose; short and noisy. |

Per-palace and per-domain configurable.

### 4.3 Novel-canonical promotion

- Below-threshold inputs go into a candidate pool (not promoted immediately).
- Pool clusters incoming candidates at threshold + 0.05.
- Cluster promotes to canonical when ≥3 members and stable across ≥2 Class 2 miner passes.
- Promotion is recorded as an event with cluster members for audit.

### 4.4 Review surface — the daily/weekly review mode

The user-facing review surface is the **app's daily/weekly review mode**, not primarily an MCP tool. Three layers:

- **`pending_review` view**: a master-derived view aggregating items awaiting user review across the system: schema induction proposals, open contradictions, period-state transition proposals, recurrence cluster validations, canonicalizer pending entries, inhibition adjustments.
- **App review mode** (UI consumer): daily and weekly digests. Chunked, paced for human attention; covers each pending category with examples. User confirms/rejects/renames/merges.
- **MCP programmatic surface** (`mempalace_pending_review(category, ...)`): for agents that want to query and act on pending items programmatically. Same view, different interface.

All three exist concurrently. The daily/weekly cadence is a UX decision; the underlying data is always live.

### 4.5 Reversibility

All canonicalization decisions are events. Wrong collapse → emit `canonicalization_reverted` event with new mapping. Canonical history is queryable; current canonicalization is the latest non-reverted mapping.

### 4.6 General extractor

(Not in `predictions/` zip but referenced.) Becomes a Class 1 miner pass with the canonicalizer applied to memory_type assignment. Regex-marker logic is reusable as fast-path heuristics; LLM/local-classifier is slow path; canonicalizer gates output vocabulary. The room-as-type mapping is dropped — type is a versioned drawer property; topic is emergent from event/schema participation.

---

## 5. Markets beyond tag-only

### 5.1 Constraint

Any non-tag market type must satisfy: **the subject cannot determine the resolution favorably by their own action alone.** If knowing the market exists lets the subject resolve it in their preferred direction by choosing how to behave, it's gameable.

### 5.2 Endorsed market shapes

Four shapes survive adversarial review:

#### 5.2.1 External-evidence markets

Resolution against signed third-party content (web search results, court records, public statements). Doesn't touch palace content. Extends the existing MODE_EXTERNAL with formula-registry entries adapted to external content. *Constraint*: the market's question must be specific enough that resolution is determinable from external evidence; vague questions get classified `NOT_RESOLVABLE` by the resolvability classifier.

#### 5.2.2 Behavior-vs-baseline markets

Resolution against the subject's own historical baseline — "is the subject's velocity along axis Y this week different from their historical mean by more than 2 standard deviations." The baseline is established before the subject knows about the market; gaming requires sustained behavior change against an established baseline, which is costly and detectable.

*Attack surface*: short baseline windows allow the subject to establish a fake baseline. Mitigation: minimum baseline window (e.g., 90 days) before any market on that axis can be created.

This relies on the **self-baseline-tracking** part of the signature concept (§8.2). It does not depend on cross-user signature comparison, which avoids the empirical concerns about local geometries.

#### 5.2.3 Multi-witness markets (without random sampling)

The market specifies witness eligibility criteria (e.g., "devices that exchanged messages with the subject in the time window"). The oracle queries *all eligible witnesses* — no random sampling. Resolution requires N consistent responses; one strong dissent flags INDETERMINATE.

*Attack surface*: subject can game their own device. **But the app is controlled** — subject's device runs the official binary with attested integrity, and the app's behavior is deterministic from the subject's actual interaction history. Subject can avoid having interactions in the time window, which affects whether their witness is eligible at all; subject cannot inject false evidence into the witness's data.

*Eligibility set is computed off-chain* by the oracle. Only the witness submissions (and the eligibility-set hash for reproducibility) go on-chain. This avoids adding random-sampling logic to Rust.

#### 5.2.4 Subject-blind formula markets

The market specifies the subject; the resolution formula is *encrypted to the resolver's enrolled key*. The subject knows they're being measured but doesn't know along which axes or with what thresholds. Formula reveals at resolution time.

The resolver, in this case, is **the subject's own mempalace** — because the formula is encrypted to that mempalace's enrolled key, only that device can decrypt it. The subject's mempalace acts as the switchboard node that resolves this market.

This is structurally interesting: the subject's mempalace runs the formula on the subject's own data, but the subject has no read access to the formula until after resolution. Hardware-bound key management plus the daemon's read-only-formula-ABI guarantees this.

*Attack surface*: subject can over- or under-comply uniformly across all axes (since they don't know which dimensions matter). But the formula is N-dimensional; uniformly heavy compliance is itself a behavior pattern that can be detected by markets the subject doesn't know about. As long as multiple subject-blind markets are running concurrently with different formulas, uniform-compliance gaming is itself signal.

### 5.3 Dropped (no acceptable adversarial story)

- **Counterparty-attestation markets** — collusion between subject and counterparty cannot be mitigated.
- **Commit-reveal of formula at the bet level** (R2 §5.2.1) — useless in practice.
- **Time-shifted commitment markets** — depends on subject not learning of the market until after the window; secrecy hard to maintain.
- **Semantic-content / third-party-mention markets** (R1's design) — gameable by subject choosing whether to discuss the topic.

### 5.4 On-chain extension for endorsed shapes

`state.rs` and `acta.rs` get the following additions, each as separate PDAs (no breaking changes to existing markets):

```rust
/// External-evidence markets — extends MODE_EXTERNAL with external-formula support.
/// Already mostly covered by existing MODE_EXTERNAL; just adds formula-registry IDs.

/// Behavior-vs-baseline markets.
/// Seeds: [b"baseline_market", market.key().as_ref()]
#[account]
pub struct BaselineMarketEvidence {
    pub market: Pubkey,
    pub subject: Pubkey,
    pub axis_id: [u8; 32],          // hash of the axis name (theme, schema, etc.)
    pub baseline_window_start: i64,
    pub baseline_window_end: i64,
    pub measurement_window_start: i64,
    pub measurement_window_end: i64,
    pub deviation_threshold_bps: u16,
    pub formula_id: u8,             // baseline-formula registry entry
    pub bump: u8,
}

/// Multi-witness markets.
/// Seeds: [b"witness_market", market.key().as_ref()]
#[account]
pub struct WitnessMarketEvidence {
    pub market: Pubkey,
    pub subject: Pubkey,
    pub eligibility_criteria_hash: [u8; 32],  // hash of off-chain eligibility spec
    pub min_consistent_witnesses: u8,
    pub eligibility_set_hash: [u8; 32],       // hash of eligible-set published by oracle
    pub formula_id: u8,
    pub bump: u8,
}

/// Subject-blind formula markets.
/// Seeds: [b"blind_market", market.key().as_ref()]
#[account]
pub struct BlindFormulaMarketEvidence {
    pub market: Pubkey,
    pub subject: Pubkey,
    pub resolver_pubkey: Pubkey,    // typically the subject's enrolled mempalace
    pub encrypted_formula_hash: [u8; 32],  // off-chain encrypted formula identified by hash
    pub bump: u8,
}
```

Plus instructions for each: `init_baseline_market`, `init_witness_market`, `init_blind_formula_market`, and submission instructions mirroring the existing `submit_evidence` pattern.

---

## 6. Ranker isolation and integrity

### 6.1 Threat model

Users (or third parties via a ranker registry) can write rankers. A malicious ranker could:
- Multiply outputs by zero to nullify a stack
- Inject code into the daemon binary
- Read state it shouldn't access
- Modify what other rankers see

### 6.2 Mitigations (all applied together)

- **Process isolation per ranker.** Each ranker runs in its own process. Communication is over IPC with typed schemas. Native code injection across the OS process boundary is blocked.
- **Pure-function contract.** Rankers take `(query, candidates, stance, features) → scored_results`. No side effects, no network access, no filesystem access. Enforced via restricted-syscall sandbox (seccomp on Linux, App Sandbox profiles on macOS, equivalent on Windows).
- **Trusted aggregator combines outputs.** When stack composition combines multiple rankers' results, combination logic is in the trusted core. A ranker returning all-zeros doesn't zero the stack — the aggregator sees the all-zeros and proceeds (with a warning event).
- **Code signing for downloaded rankers.** Rankers downloaded from a ranker registry have a signature. Daemon verifies signatures before loading. Unsigned rankers don't load.
- **Behavioral monitoring.** Daemon watches ranker output distributions. Always-zero rankers, always-agreeing rankers (suggesting shared state), or rankers producing values outside the expected range trigger alerts.
- **Capability-restricted ranker API.** Rankers read features from a typed read-only view. Cannot write to logs, append events, or trigger other rankers. Only output is scored results.
- **Resource limits per ranker.** CPU, memory, wall-clock budget per invocation. Exceeded → ranker is killed and the call returns an error to the aggregator.

A malicious ranker can return wrong answers (which would surface in evaluation as poor performance and lead to deprecation), but cannot compromise the daemon or other rankers.

---

## 7. Secure compute substrate

The same substrate serves both cross-mempalace matching (federation, spec Part 9) and local-only inference for evidence resolution.

### 7.1 Components

```
secure_compute_substrate/
  attestation.py    — model attestation primitives; cross-platform
                       (StrongBox + Secure Enclave) chain verification
  transport.py      — libp2p protocols (§7.5)
  sandbox.py        — sandbox lifecycle: provision, decrypt-inside-boundary,
                       run, emit findings/results, tear down, destroy keys
  enclave_run.py    — execution interface for Step.run() inside sandbox
                       boundary; LOCAL_ONLY enforcement
  attest_chain.py   — attestation chain verification (device → enrolled
                       → enrolled-model)
  session_keys.py   — per-session keypair generation in hardware-bound
                       key store; destruction at session end
  rate_limit.py     — match-request rate-limiting and idempotency cache
```

### 7.2 Hardware-bound attestation, cross-platform

The architectural commitment is **hardware-bound key store with attestation**. Specific implementation is platform-dependent.

- **Android**: StrongBox (or TEE-backed Keystore as fallback on devices without StrongBox). Key Attestation via `KeyInfo` chain rooted in Google's attestation root.
- **iOS**: Secure Enclave with App Attest. Attestation chain rooted in Apple's attestation root.

Both produce attestation chains. The on-chain layer accepts both. Enrollment instructions take a `platform` field (already trivially fits the existing `enroll_device` instruction) and the attestation verification logic on-chain has two verifiers — one for each platform — selected by the `platform` field. One decision rule: valid chain to the platform's attestation root + key-description matches enrollment expectations.

### 7.3 Per-session keys

A **matching session** is the lifetime of one match operation between two palaces: from match-request acceptance through slice transfer through layered triangulation through findings emission to teardown. Maybe minutes to a few hours.

A **per-session key**: receiving palace generates a fresh keypair at session start in its hardware-bound key store. Public key shared with sending palace; sending palace encrypts slice to that public key; only receiving palace decrypts. Private key destroyed at session end.

If the same key were reused across all transfers, an attacker who eventually compromised that key could decrypt historical transfers. Per-session limits the blast radius.

### 7.4 At-rest security

Threat: an attacker gains code-execution access to the cloud box (SSH, exploit, etc.) but doesn't have the user's mobile device. What protects the data?

- **Disk-level encryption.** Palace data files (event log, ChromaDB, derived view storage) are encrypted at rest with a key not stored on the box. Decryption key supplied at daemon startup after challenge-response with the user's mobile device. At rest, the disk is opaque blobs.
- **Idle-zeroing.** When the daemon detects a period of inactivity (no operations, no requests), it zeros in-memory decryption keys and unmounts encrypted volumes. To resume, re-authenticates with the user's device. SSH'd attacker arriving during idle sees no decrypted data and no in-memory keys.
- **Binary integrity attestation.** Daemon binary's hash is registered on-chain at enrollment. Daemon refuses to start if on-disk binary doesn't match registered hash. Catches binary modification.
- **Signed audit log.** Every action (event appended, view updated, ranker invoked, etc.) is recorded in an audit log signed with the daemon's hardware-bound key. Audit log encrypted at rest. Modifications to data on disk that bypass the log create gaps; next daemon startup verifies audit chain and refuses to start with gaps. Tamper-detected.
- **Code-data volume separation.** Rankers and other downloaded code in a different volume from master log. Code volume can be re-downloaded (signed by the registry); data volume requires user-device-mediated decryption.

Combined effect: SSH'd attacker on idle box sees encrypted disk, encrypted audit log, and a binary that can't be modified without detection on next start. Cannot read data. Cannot run modified binary. Leaves traces if attempting interference.

This achievable without TEE/SGX. Requirements: disk encryption (LUKS or equivalent), daemon binary attestation (binary hash registered on-chain), idle-detection in daemon, signed audit log with on-startup verification, separation of code volume from data volume.

### 7.5 libp2p protocol IDs

Named protocols, each with its own attestation requirements:

- `/mempalace/discovery/1.0.0` — query the public-manifest registry; return ranked candidates.
- `/mempalace/manifest/1.0.0` — fetch a specific palace's public manifest.
- `/mempalace/match-request/1.0.0` — initiate a match. Negotiates layer, scope, consent.
- `/mempalace/slice/1.0.0` — transfer encrypted slice in response to match request.
- `/mempalace/findings/1.0.0` — emit structured findings to switchboard after match.
- `/mempalace/heartbeat/1.0.0` — app-side integrity heartbeats.
- `/mempalace/evidence/1.0.0` — evidence response for resolution queries.

Each protocol's message schema, attestation requirements, TTLs are specified in the per-module documentation.

### 7.6 Phone-off graceful degradation

The mobile app produces hourly heartbeat attestations. When phone is offline:

- **Generous TTL on decryption keys**: daemon receives a 24-hour key release at session-start, can keep operating until TTL expiry. Refreshed when phone comes online.
- **Read-only mode** when fresh attestation isn't available within heartbeat-grace: daemon serves queries but can't append to log or run heavy operations. Resumed when phone reconnects with fresh attestation.
- **Integrity-lockout** for sustained outages: 3 consecutive missed heartbeats (3 hours) → lockout flow triggered. Daemon stops; on-chain instruction `trigger_app_integrity_lockout` is callable by the contract, returning staked funds to the user (in case the app was spoofed) and locking until re-enrollment.

So:
- Up to 3 hours phone-off: fine; daemon operates on TTL'd keys.
- 3+ hours: lockout flow; user must re-enroll to resume.
- Read-only mode bridges briefly when keys are TTL-expired but heartbeat is fresh.

Hourly heartbeat with N=3 grace is the committed default. User-configurable per device.

### 7.7 Privacy framing — exposure inside trusted boundaries

The privacy commitment is **not** "data never crosses boundaries." It's: **data crosses only inside trusted boundaries; only what the requested intention requires; only for the duration required; only emerging as structured findings the boundary allows.**

In matching: foreign palace slice crosses into sandbox; sandbox runs only the operations the matching protocol specifies; only structured findings emerge. Inside the sandbox boundary, exposure is bounded by the operation, not by an arbitrary minimization.

This reframing has implications:
- Layer 2 derivation chains can include the assertion's discourse structure, not just a stripped skeleton — because the boundary is the sandbox, not the slice itself.
- Layer 3 substrate comparison can include verbatim content — because that's what substrate-level matching needs and the sandbox enforces tear-down.
- The signature triage at layer 1 stays minimal because layer 1 is the *initial* filter, not the boundary: minimizing initial exposure narrows candidate sets cheaply before sandbox sessions are spun up.

The frame is: minimize *outside* the boundary; allow *inside*, bounded by intention.

---

## 8. The signature concept (narrowed)

The "unusual axes alignment as primary match signal" framing from earlier rounds is dropped. Empirical evidence for cross-user signature alignment is weak; the math runs but the trajectories are different.

### 8.1 Two legitimate uses

**Self-baseline tracking.** Signature snapshots per period, against the user's own past. Trajectory analysis within consistent embedding history. Powers behavior-vs-baseline markets (§5.2.2) and the "is the user drifting" reflection mode for the user themselves.

**Triage indicator** for layered matching. Cheap pre-filter: "are these two profiles even worth running a full match against." False positives at this stage are tolerable (next layers catch them); false negatives are not. Layer 1 in the federation pathway.

### 8.2 What's in the signature (for both uses)

- **Mean position** in embedding space across recent drawers (per theme, per period).
- **Velocity field** — per-theme velocity over recent windows.
- **Schema fingerprint** — canonical schemas at canonical-projection level.
- **Contradiction-resolution profile** — statistical features over how the user resolves contradictions.
- **Fork-significance pattern** — events the miner identifies as decision-points carry a `fork_significance` score (§9.3); the signature includes the user's distribution over fork-significance scores per theme. Decision-rich themes have different signatures than routine ones.

The signature does *not* include anything that requires cross-user calibration. No PCA against an external population, no eigendecomposition against shared bases, no claim of "unusual axes that align across palaces."

### 8.3 Triage feedback loop

Triage emits candidate-pair matches → layer 2 evaluates → some confirmed, some false positives.

For false positives:
- Triage records *which signature dimensions aligned* in the false-positive case.
- The triage similarity metric down-weights those dimensions for future comparisons.
- **Cooldown**: a single false-positive pattern can't trigger another adjustment for 30 days.
- **Cap**: a single dimension can't be down-weighted to zero, only attenuated. Floor at 0.1 of original weight.
- All adjustments recorded as events; reversible.

This is small (per-user adjustment matrix), simple math, no infinite-loop risk. Online learning bounded by structural constraints.

---

## 9. Conway/KisMATH integrations

### 9.1 Two-systems consolidation pattern (Conway)

The neuroanatomical observation: human memory has at least two systems with different consolidation rates. Episodic-buffer (fast, perceptual, raw, hippocampal) and autobiographical-knowledge (slow, schematic, organized, prefrontal-mediated). The Class 1/2/3 miner pass cadence isn't just a performance choice — it's a faithful replication of this two-systems pattern.

Implications for the data model:

- **Drawer events and assertion events are different rates of consolidation already.** Drawers arrive at capture rate; assertions arrive at slower rates; schemas at slowest. The naming as Class 1/2/3 maps to Class 1 = episodic-buffer-flavored; Class 3 = autobiographical-knowledge-flavored.
- **Drawer deletion does not cascade to derived assertions.** An assertion with deleted drawers becomes a *floating* assertion. New event kind: `assertion_orphaned` — emitted when an assertion's `derived_from` drawers are all invalidated/deleted. The orphaned assertion isn't deleted but is flagged with reduced confidence.
- **Hierarchical retrieval preference.** Conway's protocol studies show retrieval almost always passes through the autobiographical-knowledge layer first (period before event before drawer). Handles support a `prefer_hierarchical_scope` flag — surfaces results going through period/event scoping before drawer-level matching. Default is true; flat retrieval is a fallback.
- **Re-derivability.** Assertions carry a `derivation_seed` property — sufficient to re-run the derivation deterministically. For miner-asserted items: miner pass version + drawer IDs + deterministic prompt/template hash. Powers the substrate-verification flag (§9.4).

### 9.2 Iterative handle refinement (Conway)

Retrieval is iterative in Conway's protocol data — initial cue, preliminary results, working-self evaluates, refines cue, re-retrieves. The handle protocol gains a `mem_refine` operation:

```
handle = mem_allocate(query, scope, stance, ...)
results = mem_resolve(handle, fidelity)

# consumer evaluates
feedback = mem_refine(handle, 
                     more_like=[result_id_a, result_id_b],
                     less_like=[result_id_c],
                     adjust_scope={...})

results_v2 = mem_resolve(handle, fidelity)  # handle now has updated state
```

`mem_refine` shifts embedding-space scope toward more_like centroid and away from less_like; optionally adjusts stance dimensions; optionally re-dispatches ranker. Handle becomes stateful.

New event kinds for handle lifecycle: `handle_allocated`, `handle_refined`, `handle_resolved`, `handle_closed`. Handle state in the daemon's view layer; consumers don't maintain state across calls.

### 9.3 Substrate verification and span-pointer provenance

Schema-driven gap-filling is a confabulation risk we don't fully protect against. Mitigation: at retrieval time, the consumer can request **substrate verification**.

- Handle protocol adds `substrate_verification: bool` flag.
- When set, retrieved assertions come with their supporting drawer_refs and a substrate-faithfulness score (how closely the assertion text matches the substrate text).
- Low scores flag possible coherence-overwrite; high scores indicate well-grounded.

Plus finer-grained provenance:

- `derived_from` edges carry a `span` property — token offsets or line ranges within the source drawer indicating *what specifically* was used.
- Substrate verification can show: "this assertion was derived from drawer Y, lines 3-7." The user-review mode shows actual substrate for each assertion claim.

### 9.4 Exploration-vs-exploitation tunable on rankers (KisMATH)

KisMATH found two behavioral modes in LLMs: bell-shaped (high entropy at fork tokens, drives exploration) and exponential (low entropy, low exploration). Rankers gain a tunable **exploration entropy** parameter. Stance-conditional dispatch decides:

- Matching pathway: high exploration; surface diverse candidates.
- FOYER: low exploration; commit to top scores.
- Montage: mid exploration depending on the artifact (creative drafts want exploration; final assembly wants commitment).

The parameter shapes how rankers handle near-tie cases — high exploration randomizes among top-K; low exploration commits to absolute top.

### 9.5 Discourse structure at deeper alignment levels (KisMATH)

For complex reasoning, entity chains alone aren't enough; discourse structure carries non-trivial deductive load. In our matching pathway:

- Layer 2 derivation-chain comparison: at simplest level, just the assertion graph. At deeper levels, the discourse structure connecting assertions — what kinds of refinement, contradiction, support patterns the user uses to navigate from one assertion to another.
- Layer 2 has internal sub-levels:
  - **2a structural**: assertion graph alone.
  - **2b discourse**: discourse-structure patterns (refinement chains, contradiction-resolution patterns, supports/opposes structures).
  - **2c full-derivation**: full derivation graph with drawer embeddings.

Higher sub-levels expose more; gating between them follows the same prior-confidence pattern.

### 9.6 Fork-significance feature on events (KisMATH)

The miner identifies events the user navigated as decision-points (where multiple coherent continuations were possible and one was chosen). These events carry a `fork_significance` score. Fork events are signature-relevant; matching at fork events weights more heavily than matching at routine continuations (because forks are far more discriminating).

In the data model:
- `event` node properties gain `fork_significance` (float 0-1).
- Class 2 miner identifies forks by detecting goal-set transitions, schema instantiation changes, or contradiction-resolution events at the boundary.
- Signature includes per-theme distribution of fork-significance scores.
- Matching kernel weights fork-significant events higher in cross-palace alignment.

---

## 10. Open questions still open after R3

A short list, distinguishing what's design-deferrable from what blocks implementation.

### 10.1 Resolved this round

- Heartbeat cadence: hourly, N=3 grace, then lockout.
- Ranker isolation: all proposed mitigations applied together.
- At-rest security design: matches expectation.
- Multi-witness markets without random sampling.
- Subject-blind formula markets via subject-mempalace-as-resolver.
- Counterparty-attestation dropped.
- Cross-platform attestation: Android + iOS both supported.
- Phone-off graceful degradation: TTL keys + read-only + lockout.
- Federation slice protocol: discovery → manifest → request → slice → findings.
- Match-request abuse mitigations: rate-limit + idempotent cache + ledger + stake.
- Switchboard/mempalace relationship explicit.
- Signature narrowed to self-baseline + triage.
- Triage feedback loop with cooldown and cap.

### 10.2 Worth verifying before implementation

These don't block code but want confirmation:

- **Facet count: 5 (verbatim, acoustic, semantic embedding, structural, social).** Confirm this final list before file inventory.
- **Stake amount per match request.** Configurable per network, but want a starting default.
- **Match-result cache TTL.** 7 days is the proposed default.
- **Baseline-window minimum** for behavior-vs-baseline markets. 90 days proposed.

### 10.3 Definitely deferred (out of scope for spec/appendix; downstream design)

- Voice capture pipeline (app-layer concern).
- Mobile-app review-mode UX details (UX, not architecture).
- Ranker registry economics and curation policy (governance, not data model).
- Specifics of contradiction-resolution profile statistical features (signature design at leaf level).

---

## 11. Path forward

R3 is the last design document before file inventory and code generation. Sequence:

1. **Confirm the facets list** (§10.2).
2. **Produce file inventory** — every module in the new system, what it does, what it imports, what events it emits and consumes. One document, your review before code generation.
3. **Cook the files**, organized by spec part, in batches with checkpoints.

The 11 spec parts plus this appendix's stacking framework, secure compute substrate, ranker isolation, federation slice protocol, and resolvability classifier give the full structural commitment. The file inventory makes it concrete.
