# Voice stack — Design

**Status:** Design only. Companion to HANDLES_DESIGN.md (v2)
§"Voice — the rewrite" and §"Implementation that's left / Voice
stack." Says concretely what `mempalace/stack/voice/` looks like when
built.

## The principle

Voice transcription via a remote service (Google Speech, OpenAI
Whisper API, etc.) means the user's audio leaves the trusted boundary.
For a system whose entire premise is that data doesn't leave trusted
boundaries, a remote ASR is a load-bearing leak.

The architecture already commits to local inference: `mempalace/stack/`
provides the `AttestedStep` framework, R3 §1.4 commits to LOCAL_ONLY
privacy mode for steps that touch palace content, R3 §10 records
the no-network-egress guarantee that LOCAL_ONLY makes load-bearing.
A remote ASR call would violate that. Local ASR is the only consistent
choice.

Beyond that: voice is rich. Treating it as just transcription throws
away most of the signal. The voice stack is N specialized models,
each contributing one kind of substrate enrichment, composing through
the existing Stack framework.

## The substrate enrichment

What the voice stack writes into the substrate:

| Substrate object | What it carries |
|---|---|
| `TokenFeatures` | Per-token: text, onset_ms, offset_ms, prosody vector, affect distribution, speaker_label, per-feature confidences, model-pass provenance |
| `DrawerSegment` | Per-segment: boundaries, dominant speaker, dominant affect, accent distribution |
| `voice_matches_reference` edge | segment → entity reference, with confidence |
| `paralinguistic_event_at` edge | segment → paralinguistic event node (laughter, sigh, breath, code-switch) |
| `drawer_has_segment` edge | drawer → segment |
| `model_attestation` event | per voice-stack pass: which model_pass_id ran, hash of weights, timing |

This is bottom-up enrichment: the more the substrate carries, the
better the DAG paths — without changing the inference layer above.

## Stack composition

The voice stack is an ordered chain of steps, each implemented as an
`AttestedStep` (R3 §1.4) running LOCAL_ONLY. Each step reads
substrate fields the previous step produced, writes its own outputs,
emits a `model_attestation` event.

```
Audio capture (drawer audio_blob)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 1: ASR                                             │
│   Input:  audio bytes                                   │
│   Output: list[TokenFeatures] with token, onset_ms,     │
│           offset_ms                                     │
│   Model:  Whisper-class, locally hosted                 │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 2: Diarization                                     │
│   Input:  audio bytes + token timing from Step 1        │
│   Output: TokenFeatures.speaker_label populated         │
│   Model:  pyannote-class diarization, locally hosted    │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 3: Speaker matching                                │
│   Input:  audio bytes + speaker_labels from Step 2      │
│           + reference voiceprints from the DAG          │
│           (entities the user has spoken to/about)       │
│   Output: voice_matches_reference edges from segment    │
│           to entity, with per-edge confidence           │
│   Model:  speaker-embedding model + cosine similarity   │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 4: Prosody / affect                                │
│   Input:  audio bytes + tokens from Step 1              │
│   Output: TokenFeatures.prosody and                     │
│           TokenFeatures.affect populated                │
│   Model:  emotion / affect classifier, locally hosted   │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 5: Accent / origin                                 │
│   Input:  audio bytes + segment boundaries              │
│   Output: DrawerSegment.accent_distribution populated   │
│   Model:  accent classifier producing soft distribution │
│           (NOT a hard label)                            │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Step 6: Paralinguistic events                           │
│   Input:  audio bytes                                   │
│   Output: paralinguistic_event_at edges                 │
│   Model:  event detector (laughter, sigh, breath,       │
│           code-switching)                               │
└─────────────────────────────────────────────────────────┘
    │
    ▼
TokenFeatures + DrawerSegment + edges all written to substrate
```

### Why this order

- ASR first because subsequent steps need token boundaries.
- Diarization before speaker matching because matching needs to
  know which speaker each segment belongs to.
- Prosody / affect / accent / paralinguistic can run in parallel
  (steps 4, 5, 6) — they read disjoint inputs and write disjoint
  outputs. The diagram shows them sequentially for simplicity but
  the Stack framework already supports parallel composition (see
  `mempalace/stack/aggregator.py`).

### What runs when

Three execution profiles for the stack:

1. **At capture (online)**: only Step 1 (ASR). Latency-sensitive;
   the user wants to see "transcript captured" immediately. Other
   steps deferred.
2. **Class 1 miner pass (near-real-time)**: Steps 2, 3 (diarization
   + speaker matching). Adds the structural enrichment the substrate
   uses for Class 2.
3. **Class 2/3 miner pass (offline)**: Steps 4, 5, 6 (affect,
   accent, paralinguistic events). These are interpretive; running
   them in batches is more efficient and lets them use richer
   context.

This matches the existing Class 1/2/3 cadence (R3 §9.1) — voice
enrichment fits the Conway two-systems pattern naturally.

## Independence and upgradability

Each step is an independent `AttestedStep`:

```python
class ASRStep(AttestedStep):
    step_id: str = "voice.asr"
    model_pass_version: str = "whisper-large-v3@2025-08-01"
    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_ONLY

    def run(self, ctx: StackContext, inputs: dict) -> StepResult:
        ...
```

Upgrade flow when a step's model improves:
1. New model is registered with `model_registry.py` (R3 §2.2).
2. Signed code verification confirms the new model weights match
   the registered hash.
3. The step's `model_pass_version` bumps.
4. Existing `TokenFeatures` carry their `produced_by_model_pass`
   stamp so the dependency tracker knows which substrate values
   were produced by which model pass.
5. Re-running the step on a drawer produces new `TokenFeatures`;
   the old ones are superseded but preserved (event-sourced log).
6. Downstream artifacts (rankers, ranker outputs, signature) that
   depended on the old features invalidate via the existing
   dependency mechanism (Phase 4).

Importantly: upgrading Step 4 (affect) does NOT invalidate Step 1
(ASR) outputs. The granularity is per-step, not per-stack.

## Schema additions

### Events

```python
@dataclass
class VoiceStepCompleted(Event):
    """A voice-stack step finished processing a drawer or segment."""
    EVENT_KIND = "voice_step_completed"
    drawer_id: str = ""
    segment_id: str | None = None
    step_id: str = ""
    model_pass_version: str = ""
    output_summary: dict[str, Any] = field(default_factory=dict)
    completed_at_ms: int = 0
```

```python
@dataclass
class TokenFeaturesWritten(Event):
    """Per-drawer write of the token-features list."""
    EVENT_KIND = "token_features_written"
    drawer_id: str = ""
    token_count: int = 0
    features_blob_ref: str = ""           # large; stored out of band
    produced_by_model_passes: dict[str, str] = field(default_factory=dict)
    written_at_ms: int = 0
```

```python
@dataclass
class SegmentCreated(Event):
    """A drawer segment was created."""
    EVENT_KIND = "segment_created"
    segment_id: str = ""
    drawer_id: str = ""
    start_ms: int = 0
    end_ms: int = 0
    created_at_ms: int = 0
```

### Edge kinds

- `drawer_has_segment` — drawer to segment.
- `voice_matches_reference` — segment to entity, with confidence
  on the edge.
- `paralinguistic_event_at` — segment to paralinguistic event node
  (laughter, sigh, breath, code-switching). Each event kind is its
  own node so retrieval can ask "show me drawers with code-switching"
  via the existing edge-traversal machinery.

### Override edges (from HANDLES_DESIGN.md v2)

- `interpretation_memo_for` — drawer to drawer (or drawer to
  segment) declaring interpretation override.
- `interpretation_memo_for_segment` — same, segment-targeted.

These are user-emitted, not voice-stack-produced. The voice stack
respects them when they exist: if an interpretation memo says "the
angry tone in seconds 12-18 was theatrical," the prosody/affect step
defers to the memo for those tokens rather than emitting a
contradicting affect classification.

## Module structure

```
mempalace/stack/voice/
├── __init__.py            # exports the steps + the composed Stack
├── stack.py               # the composed VoiceStack(Stack)
├── steps/
│   ├── __init__.py
│   ├── asr.py             # ASRStep
│   ├── diarization.py     # DiarizationStep
│   ├── speaker_match.py   # SpeakerMatchStep
│   ├── prosody.py         # ProsodyStep
│   ├── affect.py          # AffectStep
│   ├── accent.py          # AccentStep
│   └── paralinguistic.py  # ParalinguisticStep
├── models/
│   ├── __init__.py
│   ├── registry.py        # voice-specific model registry entries
│   └── stub.py            # stub models for tests (return canned
│                          # outputs from fixtures)
├── memo.py                # interpretation-memo override resolution
└── README.md              # how to swap models
```

## Privacy and attestation

Every step runs under `PrivacyMode.LOCAL_ONLY`. The
`AttestedStep.run()` flow already enforces:
- No network egress allowed during the step (verified by the stack
  context).
- Model weights are signed per `model_registry.py`; weights with
  invalid signatures fail step execution.
- `model_attestation` event emitted per step invocation, recording
  the model_pass_version, weights hash, and step timing.

For markets that require resolution by privacy-preserving inference
(R3 §3.2), the voice stack is the substrate enrichment under the
same privacy mode. A market resolver running over the substrate
sees the voice features without ever needing to re-decode the audio
or call out to a remote service.

## Composition with the rest

- **Encryption** (ENCRYPTION_AT_EDGE_DESIGN.md v2): audio blobs are
  ciphertext at rest. The voice stack runs in the cloud-box during
  active operation — audio is decrypted under the session-key
  bundle, processed, plaintext discarded. `TokenFeatures` and
  `DrawerSegment` outputs are themselves substrate fields that
  follow the same encryption boundary (semantic content is
  ciphertext at rest, plaintext during operation).
- **Handles** (HANDLES_DESIGN.md v2): the voice stack produces the
  substrate signals (per-token prosody/affect, per-segment
  speaker/accent/paralinguistic, voice_matches_reference edges)
  that the substrate-signal analysis pass uses to pin
  `InterpretiveFrame.fields`. Without the voice stack, voice-flavored
  frames have nothing to derive from.
- **DD wiring** (DD_WIRING_SUMMARY.md): the new edge kinds
  (`drawer_has_segment`, `voice_matches_reference`,
  `paralinguistic_event_at`) flow through `current_edges` like any
  other edge kind. No new DD views needed for the voice stack
  itself; views consume the new edges automatically.
- **Phase 1 batch framing**: a voice stack invocation is a batch.
  `BatchStarted("voice_stack.process", input_summary={"drawer_id": ...})`
  opens; each step's outputs land under the batch_id;
  `BatchClosed` on completion. A torn voice-stack batch (one step
  ran, others didn't) is detectable by the recovery scan and the
  partial enrichment is quarantined or rerun.
- **Phase 4 dependency tracking**: ranker outputs, signature
  computations, and other downstream artifacts that read voice
  features carry dependencies on the `produced_by_model_pass` of
  those features. Upgrading a voice step invalidates dependent
  artifacts.
- **R3 §3.2 privacy modes**: every step is `LOCAL_ONLY`. Markets
  classified `PRIVACY_PRESERVING_REQUIRED` can use voice features
  in their formulas without the audio leaving the trusted boundary.

## Performance considerations

The voice stack is compute-heavy. Approximate per-drawer cost on a
modest cloud-box (mid-tier GPU or fast CPU):

| Step | Time per minute of audio |
|---|---|
| ASR (Whisper-class) | 5-30 sec depending on model size |
| Diarization | 5-15 sec |
| Speaker match | <1 sec (embedding + cosine) |
| Prosody/affect | 5-20 sec |
| Accent | 5-15 sec |
| Paralinguistic | 5-15 sec |
| Total | 25-100 sec per minute of audio |

Implications:
- ASR runs at capture (latency-sensitive); other steps run in batches.
- Class 1 miner pass cadence (~minutes) is fine for diarization +
  speaker match.
- Class 2/3 cadence (hours/days) is fine for prosody, affect, accent,
  paralinguistic. Batched processing amortizes model load time.
- Real-time interaction (e.g., conversational queries while a
  recording is in-flight) needs the substrate to expose partial
  enrichment — Step 1 outputs available, others pending.

These numbers are illustrative; actual performance depends on model
sizes and hardware. Implementation includes benchmarks per step.

## What this design does NOT yet do

1. **Multi-language support beyond what the underlying ASR provides.**
   If the user speaks Spanish, the ASR needs to handle Spanish; the
   stack doesn't add language-specific reasoning beyond what each
   model offers.
2. **Cross-drawer speaker continuity.** The diarization step
   produces speaker labels scoped to a single drawer (`s0`, `s1`,
   ...). Cross-drawer "this is the same speaker as in last week's
   conversation" is the speaker-matching step's job (Step 3), and
   it depends on having reference voiceprints in the DAG. Initial
   bootstrapping (no references yet) means new users have generic
   labels until they accumulate enough labeled voice data.
3. **Real-time streaming ASR.** The stack assumes a complete
   audio blob as input. Streaming would be a substantial extension
   and isn't in scope for this design.
4. **Music / non-speech detection.** A user recording with
   music in the background isn't well-served by speech-focused
   steps. A separate "audio classification" step (speech vs music
   vs ambient) could prepend the stack to skip speech-only steps
   on non-speech audio. Not in this design but compatible.

## Implementation that's left

Three buckets:

### Bucket A — module skeleton + schema

- Create `mempalace/stack/voice/` with the structure above.
- Add `TokenFeatures`, `DrawerSegment`, `VoiceStepCompleted`,
  `TokenFeaturesWritten`, `SegmentCreated` to schema.
- Add the new edge kinds to schema.
- Wire edge kinds through DD `current_edges` view (no view code
  change; just confirm the kinds work through the existing
  reduce path).
- Tests: schema roundtrip, edge kind coverage, event log replay.

### Bucket B — stub steps + stack composition

- Implement each step as a stub that returns canned outputs from
  test fixtures. The stub validates the contract (right inputs,
  right outputs, right substrate writes) without requiring a real
  model.
- Compose the steps into `VoiceStack(Stack)`.
- Tests: stack contract, per-step contract, end-to-end fixture-
  driven run, override-memo precedence.

### Bucket C — real model integration

- ASR: integrate Whisper or equivalent. Local hosting only.
- Diarization: pyannote or equivalent.
- Speaker matching: speaker embedding + cosine similarity against
  reference voiceprints stored in the DAG.
- Prosody / affect: pick a model, integrate.
- Accent: pick a model, integrate.
- Paralinguistic events: pick a model, integrate.
- Each gets benchmarks per the performance table above.
- Tests: real audio fixtures, output sanity, comparison against
  ground-truth annotations on a small held-out set.

### Sequencing

A → B → C. Bucket A and B together give a "structurally complete"
voice stack with stub models — same shape as the DD wiring sub-slices,
where the structure was complete before the Rust toolchain materialized.
Bucket C is then a sequence of model integrations, each its own
session, swappable independently.

This sequencing also lets Bucket A + B ship before the substrate-
signal analysis pass for HANDLES, because the analysis can use the
stub-output contracts to plan its work. The real-model integration
in Bucket C feeds the analysis with actual signal characteristics.

## What I'd want you to confirm

1. **Six-step decomposition?** ASR / diarization / speaker-match /
   prosody-affect / accent / paralinguistic. Or fewer steps with
   more responsibility, or more steps with finer granularity?
2. **Three execution profiles** (capture / Class 1 / Class 2-3)?
   Or different cadence?
3. **Stub-models-first sequencing?** Bucket A → B → C means we
   ship structural completeness before any real model runs. Or do
   you want one real model integrated end-to-end first to verify
   the contract under real conditions?
4. **Soft-distribution for accent (vs hard label)?** I lean soft —
   accent is rarely categorical, and downstream rankers benefit
   from the distribution.
5. **References-from-DAG-only for speaker matching?** I.e., no
   global accent registry, no celebrity voice database — only voices
   the user has interacted with or referenced. I lean strongly
   toward this; flag if you want global references too.

This design is a sketch. Edits before code.
