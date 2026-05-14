# User-facing view and delete — Design

**Status:** Design only. Captures the UX surface for "show me my
mempalace" and "delete parts of my mempalace." Companion to
ENCRYPTION_AT_EDGE_DESIGN.md (v2) — depends on the encryption
boundary defined there.

## What this addresses

Two user-visible concerns that the architecture has to support:

1. **Plaintext visibility.** The user must be able to inspect their
   own data. If the box can run inference over it, the user must be
   able to read it. Anything less makes the threat model wrong:
   "your data is private from the operator" only matters if you can
   confirm the operator has what you put in.
2. **Selective deletion.** The user must be able to remove parts of
   their mempalace, with two distinct operations: cheap-and-reversible
   ("hide this from retrieval") and expensive-and-one-way ("delete
   this from existence"). Both are needed; they are not the same
   thing.

These are UX commitments that the architecture has to make easy.
The substrate design has to support them; this doc says how.

## Three layers of view

The encryption boundary (ENCRYPTION_AT_EDGE_DESIGN.md v2) creates
three layers of what's viewable on the phone, with different cost
and decryption profiles:

### Layer 1 — Topology view (always available)

The DAG structure — nodes, edges, periods, themes, schemas, drawer
metadata — is plaintext per the encryption boundary table. The phone
can browse the entire structure without decrypting any drawers. Cost
is megabytes; runs on any phone.

What's in the topology view:
- All node IDs, kinds, and creation timestamps.
- All edges and their kinds (asserted_subject, derived_from,
  drawer_has_segment, voice_matches_reference, etc.).
- Per-drawer metadata: drawer_id, content_hash, duration_ms,
  interactional kind, capture timestamp.
- Per-assertion structural metadata: which nodes it connects, which
  schema, when it was asserted, by which miner pass.
- Heat / velocity / canonical-set markers on nodes (the DD view
  outputs).
- Segment boundaries and their structural metadata.

What's NOT in the topology view:
- Verbatim drawer content.
- Audio blobs.
- Decrypted assertion property values.
- Per-token paralinguistic features.
- Anything from the encryption boundary's "ciphertext" column.

The topology view is the default surface. Opening the app shows it.

### Layer 2 — Drawer view (on-demand, phone-side decryption)

Tapping a node in the topology view triggers a phone-only-decrypt
flow:
1. Phone requests `drawer_id` over TLS.
2. Cloud box ships `(ciphertext, DEK_handle, attestation_sig)` to
   the phone — note: the cloud box does NOT decrypt for this flow.
3. Phone SE decrypts using the Phone Master Key.
4. Plaintext displayed on the phone.
5. Phone caches recently-viewed drawers up to budget (default
   ~100MB, LRU eviction).
6. Plaintext is discarded when the user moves on.

The cloud box never sees plaintext for this flow. This is what makes
it different from running a ranker — the ranker NEEDS plaintext
because it does inference; the user view doesn't need cloud-box
plaintext because the user is reading on the phone.

### Layer 3 — Full plaintext export (chunked, deliberate)

"Show me everything" streams the log to the phone in chunks:

```
Phone: "export full plaintext, chunks of 1000 drawers"
  → Cloud box: ship batch 1 of N (ciphertext + handles)
  → Phone SE: decrypt batch 1, hold briefly, render
  → User reviews / scrolls / saves
  → Cloud box: ship batch 2 of N
  ...
  → Phone: signal "done"
```

Defaults (configurable):
- Chunk size: 1000 drawers per chunk (or ~50 MB plaintext, whichever
  smaller).
- Pause/resume supported — the export carries a cursor; resuming
  picks up from the cursor.
- Abort supported — partial export is just whatever the phone
  already cached.
- Time estimate: 100k drawers takes minutes-to-tens-of-minutes
  depending on chunk size and network.

Export UX is deliberately friction-gated:
- Distinct entry point (not a tap-on-node).
- Confirmation dialog explaining what's being decrypted and
  approximately how long it'll take.
- Progress feedback during the export.
- Pause/resume/abort controls.
- After completion, the phone holds the export until the user
  explicitly clears it OR until the LRU budget kicks in.

Why friction: an export materializes substantial amounts of plaintext
on the phone. If the phone is then compromised (lost, stolen, malware),
the export blast radius is large. Friction matches the consequence.

## Two-tier deletion

The user-deletion mechanic has two operations with very different
cost profiles. Conflating them in UX would be a mistake — the
reversible one is cheap and common, the irreversible one is expensive
and rare.

### Tier 1 — Invalidate (cheap, reversible)

The user emits `drawer_invalidated` or `node_invalidated` from the
phone. This is a substrate event:

```python
@dataclass
class DrawerInvalidated(Event):
    EVENT_KIND = "drawer_invalidated"
    drawer_id: str = ""
    invalidated_by_user: bool = True
    reason: str | None = None             # optional user-supplied note
    invalidated_at_ms: int = 0
```

After this event:
- All DD views respect the invalidation. Retrieval doesn't surface
  the drawer.
- The drawer's edges into other nodes don't disappear from the
  topology view (the structure remains visible) but the drawer is
  flagged "invalidated" so the user can see what they hid.
- Markets that referenced the drawer get NULL-evidence rather than
  the drawer's content. (This is intentional: "I deleted the
  evidence" is a real signal that some markets care about.)
- Miner passes that already produced assertions from this drawer
  don't have those assertions retracted — but the assertions are
  flagged as "derived from invalidated source" and downstream
  rankers can choose to weight them lower.

Reversibility: the user can later emit a `drawer_revalidated` event
that flips the flag back. Idempotent — revalidating an already-valid
drawer is a no-op.

The original ciphertext is NOT removed from the log. Tier-1
invalidation is a substrate-level "do not surface," not erasure. The
ciphertext remains addressable; if the user revalidates, retrieval
sees it again.

Cost: one event append. Effectively free.

### Tier 2 — Erase (expensive, one-way)

The user emits `request_erase` from the phone. This kicks an
asynchronous compaction job:

```python
@dataclass
class RequestErase(Event):
    EVENT_KIND = "request_erase"
    target_kind: str = ""                 # "drawer" / "node" / "edge"
    target_id: str = ""
    requested_by_user: bool = True
    requested_at_ms: int = 0
    erasure_job_id: str = ""              # tracks the compaction job
```

The job:
1. Walks the cold log, identifying every event referencing the
   target.
2. Rewrites those events to a tombstone form: keeps the event
   structurally (offset, kind, batch_id) but removes the ciphertext
   payload.
3. Rewrites every snapshot/backup the substrate keeps that contains
   the target's ciphertext.
4. Updates the DD views to reflect the tombstones (current_nodes,
   current_edges retract; pending_review surfaces the erasure as a
   completed action).
5. Emits `erase_completed` on success.

What this does NOT remove:
- Structural references — if a drawer was the source of an
  assertion, the assertion still exists; its `derived_from` field
  carries a tombstone reference rather than the drawer_id. The user
  can then choose to erase the assertion separately.
- Hashes used by collision detection — `content_hash` is preserved
  even after erasure so that if the same content is captured again,
  the substrate can flag it as previously-erased and prompt the
  user.

What this DOES remove:
- The ciphertext itself, irrecoverably.
- All copies in snapshots, backups, and federation cache.

Cost: minutes-to-hours depending on log size. Status surface ("queued
for erasure; will complete by X") is visible during the job.

Reversibility: zero. Once `erase_completed` fires, the data is gone.

UX:
- Distinct entry point from invalidate.
- Triple-confirmation dialog: "this cannot be undone" → "really
  cannot be undone" → "type DELETE to confirm".
- Estimate of how long the job will take.
- Job status surface where the user can see queued / running /
  completed erasures.
- After `erase_completed`, the user's topology view shows tombstone
  markers where the erased data used to be.

### Tier 3 — Burn the palace (catastrophic, one-way)

Different from Tier 2: not "delete this data," but "destroy the
entire palace." Triple-confirmation flow on the phone destroys the
Phone Master Key. Cloud-box session bundles can no longer be
refreshed; on TTL expiry they idle-zero. All ciphertext on disk
becomes unrecoverable. On-chain `IntegrityLockout` PDA fires per R3
§7.6 and stake is returned.

Distinguish from Tier 2 in UX:
- Tier 2 = "delete this drawer / this node"
- Tier 3 = "shut down my entire mempalace"

Different entry points. Tier 3 lives behind a settings menu, behind
several screens, with extreme warnings.

## What the user can browse and delete

| Substrate object | Browsable in topology view? | Browsable as plaintext? | Tier 1 invalidate? | Tier 2 erase? |
|---|---|---|---|---|
| Drawer | Yes (metadata) | Yes (phone-only-decrypt) | Yes | Yes |
| Drawer segment | Yes (metadata) | Yes (phone-only-decrypt) | Yes | Yes (whole drawer or this segment) |
| Node (entity, theme, period) | Yes | n/a (structural only) | Yes | Yes (cascades to edges) |
| Edge | Yes | n/a (structural only) | Yes | Yes |
| Assertion (NodeCreated for a property) | Yes (structure) | Yes (decrypted properties) | Yes | Yes |
| Schema | Yes | Yes | No (schemas are derived) | No |
| Heat / velocity / signature | Yes (read-only DD views) | n/a | No | No |
| Match findings | Yes | Yes | Yes | Yes |
| Audit log entries | Yes (read-only) | Yes (decrypted) | No | No |

The "no" entries: schemas are derived from drawers; if you erase the
drawers, the schemas re-derive next miner pass without them.
Heat/velocity/signature are running aggregates over the substrate;
they reflect whatever's left after invalidation/erasure.

Audit log entries are intentionally non-deletable — they're the
record that the user did or didn't do something, which the integrity
model depends on.

## Phone-only-decrypt path — concrete protocol

The flow that powers Layer 2 (drawer view) and Layer 3 (full export):

```
Phone                                  Cloud box
   │                                       │
   │  GET /palace/drawer/{drawer_id}       │
   ├──────────────────────────────────────▶│
   │                                       │ (no decryption performed)
   │  {ciphertext, dek_handle,             │
   │    attestation_sig, content_hash,     │
   │    metadata}                          │
   │◀──────────────────────────────────────┤
   │                                       │
Phone SE.decrypt_drawer(ciphertext, dek_handle)
   │  (verifies attestation_sig matches    │
   │   content_hash, DEK_handle is bound   │
   │   to this palace, etc.)               │
   │                                       │
Plaintext rendered on phone display        │
   │                                       │
```

Phone SE verifications during decrypt:
1. `dek_handle` is bound to this Phone Master Key (not some other
   palace's).
2. `attestation_sig` is valid over `(ciphertext, dek_handle,
   content_hash)`.
3. After decryption, recompute `content_hash` of the plaintext;
   matches the stored `content_hash`. (Catches any tampering with
   ciphertext-on-disk by the cloud box or operator.)

If any verification fails, the phone surfaces "this drawer's
ciphertext appears tampered with" and offers to flag the cloud box
for integrity check.

## Where this composes with the rest of the architecture

- **Encryption** (ENCRYPTION_AT_EDGE_DESIGN.md v2): provides the
  phone-only-decrypt path that Layer 2 and Layer 3 depend on, and
  the full-disk encryption that ensures the cloud-box is the only
  entity capable of producing ciphertext + handles for the phone
  to decrypt.
- **DD wiring** (DD_WIRING_SUMMARY.md): drawer/node invalidation
  events flow through the existing `current_nodes`,
  `current_edges`, etc. views — which already retract on
  invalidation patterns (sub-slice E `pending_review` uses the
  same shape). Erasure tombstones are a new event kind but they
  fit the same DD reduce pattern.
- **Phase 1 batch framing**: an erasure job is a batch.
  `BatchStarted("erasure.job", input_summary={"erasure_job_id": ...})`
  opens; per-event tombstone writes happen under the batch_id;
  `BatchClosed` on completion. Crashed erasure mid-batch leaves a
  torn batch that the recovery scan can resume rather than restart
  from scratch.
- **Phase 4 dependency tracking**: when a drawer is invalidated or
  erased, all `RankerOutput` artifacts that depended on it
  invalidate via the existing dependency mechanism. Cached scores
  recompute next time they're queried.
- **R3 §5 markets**: market resolutions completed before the
  invalidation/erasure are NOT retroactively changed. The audit
  trail reads "market M resolved using drawer D; drawer D was
  later erased." Markets in flight at the time of erasure see
  NULL-evidence for the erased drawer and resolve accordingly.

## Implementation that's left

This design adds three implementation tracks:

### 1. Topology browsing API + phone surface

The cloud-box exposes a topology endpoint that returns DAG metadata
without decryption. The phone consumes it and renders. Implementation
work:
- Cloud-box endpoint that emits topology JSON (paginated; uses
  existing DD views, no new compute).
- Phone client + UI for browsing nodes/edges/periods/themes.
- "Tap to view" handler that triggers Layer 2 phone-only-decrypt.

### 2. Phone-only-decrypt flow

The protocol above. Implementation work:
- Cloud-box endpoint that returns `(ciphertext, dek_handle, sig,
  content_hash, metadata)` for a drawer_id without decrypting.
- Phone SE method `decrypt_drawer` per ENCRYPTION_AT_EDGE_DESIGN.md
  v2 §"SecureElement interface (revised)".
- Phone-side verification (handle binding, signature check, hash
  match).
- LRU cache on the phone with the configured budget.

### 3. Invalidate + erase flows

The two-tier deletion mechanic. Implementation work:
- Schema: `DrawerInvalidated`, `NodeInvalidated`, `EdgeInvalidated`,
  `DrawerRevalidated` (and equivalents for nodes/edges),
  `RequestErase`, `EraseCompleted` event kinds.
- DD views consume invalidation; verify they retract correctly.
- Erasure compaction job: walks the log, rewrites tombstoned events,
  updates snapshots, emits `EraseCompleted`. This is a non-trivial
  worker — needs idempotency (if it crashes mid-job, resume cleanly)
  and progress reporting.
- Phone UI for both flows with the friction levels described
  (tap-to-invalidate is one tap; erase is triple-confirmed).

### Sequencing

These three tracks are largely independent; pick whichever to ship
first based on what blocks UX work most. The natural order would be
(1) → (2) → (3): topology browsing without plaintext is useful by
itself; phone-only-decrypt unlocks Layer 2 viewing; invalidation +
erasure adds the destructive operations on top of the visibility the
first two provide.

Each is its own session, similar to the DD wiring sub-slices.

## What I'd want you to confirm

1. **Three-layer view shape?** Topology / on-demand-drawer /
   chunked-export. Or do you want a different cut?
2. **Two-tier deletion?** Tier 1 invalidate (cheap, reversible) +
   Tier 2 erase (expensive, one-way) + Tier 3 burn-palace (separate
   from Tier 2). Confirm.
3. **Default chunk size for export?** 1000 drawers / 50 MB. Adjust?
4. **Audit-log non-deletability?** Audit log entries are not
   user-deletable. The argument: integrity depends on them. The
   counter-argument: user might want to clear their audit trail.
   I lean strongly toward non-deletable but flagging for your call.
5. **Invalidation visibility in topology view?** I propose: the
   structure stays visible with an "invalidated" flag, so the user
   can see what they hid. Alternative: invalidated nodes disappear
   from topology too. I lean visible-with-flag.

This design is a sketch. Edits before any code.
