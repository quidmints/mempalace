# TODO — React Native voice agent for MemPalace

This document collects the React Native–side work that pairs with the
backend (Python + Rust) substrate. **Do NOT implement these in the
backend repo** — they belong to the existing React Native repo as a
separate module.

## Concept reminder

The IO reference for MemPalace is *a conversation with your
app-mediated palace voice agent*. The voice agent makes it possible
for the substrate to query *you*, not just for you to query it.
Bidirectional dialogue is what keeps the substrate healthy
(fine-tuning, pruning, preventing rhymes from becoming tumors).

By default the app is in **listening mode**. Conversation mode is
either scheduled (daily / weekly review) or triggered on demand.

## Categories

### A. Voice capture loop (default listening mode)

- [ ] Background-safe ambient capture: continuous audio rolling
      buffer with VAD-based segmentation. Power budget: phones
      should not need more than the OS gives "always on assistant"
      apps already.
- [ ] Per-segment encryption at the edge: each captured segment
      encrypts via the device's StrongBox / Secure Enclave–bound
      key BEFORE leaving the audio capture pipeline. The cloud-box
      never sees plaintext. (Pairs with backend Track 5C.)
- [ ] Auto-pause on detected non-self speech that's not addressed
      to the device (per-user calibration).
- [ ] User-visible "I'm listening" affordance + hard mute toggle
      that severs the capture pipeline at the OS level (not just
      a software flag).

### B. Conversation mode (triggered/scheduled)

- [ ] **Daily review trigger**: user-defined window (default:
      bedtime). On trigger, the voice agent opens with: "Quick
      check-in. Earlier you mentioned [drawer summary]. Want to
      come back to that?" Pulls from the backend's daily-review
      query.
- [ ] **Weekly review trigger**: separate cadence. Pulls a
      different shape — themes that emerged this week, contradictions
      that opened, periods that closed.
- [ ] **On-demand trigger**: user says/taps a wake word. Voice
      agent enters conversation mode, queries the substrate for
      what to surface based on recent context.
- [ ] **Mempalace-initiated**: when the substrate has a high-priority
      clarifying question (open contradiction, drift report, missing
      provenance on a high-importance assertion), the agent surfaces
      it the next time the user is in a receptive state. Backend
      already provides the queue (`PendingQueryQueue` in
      `mempalace/query/bidirectional.py`); RN consumes it.

### C. Clarifying-question shapes

The agent's questions don't sound like questions. They're
fill-in-the-blank prompts shaped like a never-ending story
with dialogue. Examples:

- "When you said you were tired earlier, that was after ___..."
  (user fills: "the call with my sister")
- "I keep noticing 'cooking' near 'evening' — your mom mentioned
  that one too. Is that a ___?" (user fills: "tradition")
- "Dum spero ___" (a recall prompt; user fills: "spiro" — substrate
  uses Google-search-suggestion-shaped richer composition)

Implementation:

- [ ] Map `QueryType` from `mempalace/query/bidirectional.py` to
      RN UI affordances:
   - `FILL_IN_THE_BLANK` → text input with prefix/suffix shown,
     blank position highlighted
   - `WHAT_IF` → narrative card with toggle
   - `PERIPHERAL_VISION` → ambient note, dismissible
   - `META_COGNITION` → reflective prompt with "I'd rather not
     answer right now" affordance
- [ ] Voice rendering: questions go through TTS with the agent's
      configured voice. User speaks the answer; ASR routes the
      response back to the substrate's query handler.
- [ ] Stash-for-later: any prompt the user defers gets re-queued
      in the substrate; backend already handles the requeue logic.

### D. Bidirectional query loop

- [ ] When the user starts a query (voice or text), the RN client
      passes through the substrate's `Canonicalizer` to expand
      handles (re-routing semiotic intelligence). User sees the
      canonicalized form for confirmation if it diverged
      meaningfully from the input.
- [ ] When the substrate generates a clarifying question, the RN
      client surfaces it with the appropriate UI shape (see C).
- [ ] Conversation memory: the dialog itself becomes drawers in
      the substrate. The voice agent's prompts are tagged
      `interactional=PALACE_QUERY`; user responses are tagged
      `interactional=PALACE_RESPONSE`. Backend already supports
      this via `mempalace/schema/kinds.py` `InteractionalKind`.

### E. Voice substrate (Track 1C — needs hardware)

Track 1C from the backend roadmap is "real voice models." It's
deferred because it needs hardware tuning. RN-side TODOs:

- [ ] **Speaker diarization**: locally on-device, distinguishes
      operator vs others. Required for the asserter field — when
      another person speaks about the operator, the captured drawer
      should be marked as a candidate for an external assertion (not
      a self-observation).
- [ ] **Affect / paralinguistic features**: pitch, intensity,
      stress, hesitation. These feed the backend's voice-substrate
      pipeline.
- [ ] **Whisper-style on-device ASR**: required so the cloud-box
      never sees plaintext audio. Choose a model that runs on
      iPhone 12 / Pixel 6 class hardware at <1× realtime.
- [ ] **Voice-cloning suppression**: The agent uses a
      distinct voice the operator chooses; the operator's voice
      is only for input. (Privacy + dissociation prevention.)

### F. UI for cross-palace mentions (asserter — backend done)

The backend supports `AssertionAsserter` for cross-palace mentions.
RN TODO:

- [ ] **Show external mentions as a distinct card type** in the
      substrate viewer. "[Palace Alice] said you tend to take on
      too much" should be visually differentiable from a
      self-observation.
- [ ] **Affordance to decorate**: tap an external mention →
      "what do you make of this?" prompt. User's response gets
      stored as a self-asserted assertion *about* the external
      assertion (anchor boundary — see backend
      `ANCHOR_BOUNDARY_DESIGN.md` once the design firms up).
- [ ] **Filter by asserter**: in retrieval UI, allow "show me only
      what others have said about me" / "show me my own
      observations" / "show me Palace X's mentions."

### G. Privacy + safety

- [ ] **Burn-palace UI**: triple-confirmation as designed in
      `USER_VIEW_AND_DELETE_DESIGN.md` Tier 3. Wires to the
      backend's `signal_burn` flow.
- [ ] **Tier 2 erase UI**: per-drawer or per-period erasure with
      strong confirmation. Wires to `request_erase`.
- [ ] **Export UI**: chunked download of the full plaintext
      substrate (decrypted on-device as it streams). Wires to
      `ChunkedExporter`.
- [ ] **Mute / pause** affordances at three levels: the listening
      pipeline, the conversation prompts, the substrate mining.
      User should be able to disable any of the three independently.

### H. Sync + offline

- [ ] **Phone offline → cloud-box continues as oracle**: the
      backend supports oracle work continuing while the phone is
      offline (the box's session bundle stays valid until TTL).
      RN TODO is to make this *visible*: indicator in settings
      showing "your cloud-box has been operating as an oracle
      node X% of the time you've been offline." Trust signal.
- [ ] **Sync-on-reconnect**: when the phone comes back online,
      pull deltas from the cloud-box. Existing federation
      machinery handles the wire format.
- [ ] **Bundle refresh**: phone re-issues the session bundle on
      a schedule + on-demand. RN UI shouldn't need to expose this
      directly, but should surface "your palace was locked out
      because the bundle expired" if it ever happens.

---

## Wiring map (RN ↔ backend)

| RN component | Backend module |
|---|---|
| Listening pipeline | `mempalace/drawer/capture.py` (encryption-at-edge) |
| Daily-review query | `mempalace/query/qualifier.py` |
| Bidirectional dialog | `mempalace/query/bidirectional.py` |
| Cross-palace mention card | `views/graph.py:assertions_about_self()` |
| Burn UI | `mempalace/secure/burn.py:signal_burn` |
| Erase UI | `mempalace/views/erase.py:request_erase` |
| Export UI | `mempalace/views/export.py:ChunkedExporter` |


