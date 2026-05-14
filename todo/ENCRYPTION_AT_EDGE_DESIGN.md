# Encryption at Edge — Design (v2)

**Status:** Design only. No code yet. v2 supersedes v1; the v1 document
overpromised on the secure-element invariant by treating phone-side
and cloud-box-side as equivalent. They are not equivalent. v2 says
explicitly what each side can and cannot promise.

## The threat model — actual

The architecture has two compute hosts with different security primitives:

- **Phone**: Apple Secure Enclave / Android StrongBox. Hardware-isolated
  key storage; encrypt/decrypt happens inside the element; raw key
  bytes never leave it. This part of the v1 framing is correct.
- **Cloud box**: a colo or cloud VM with at-best a TPM. No hardware-
  isolated execution. Raw key bytes ARE in process memory while the
  daemon runs. R3 §10 explicitly rejected requiring TEE/SGX for the
  cloud box; that decision is upstream of this design.

Treating the cloud box as if it had SE-grade isolation is wrong. v1
did that. v2 does not.

### Threat actors and what's actually defended

| Actor | What they can do | What protects against this |
|---|---|---|
| **Casual operator** (SSH, no kernel malware) | Read cold disk, observe daemon during operation | Full-disk encryption + idle-zeroing means cold reads return ciphertext only. Live memory access during operation is limited to whatever they can see via `ps` / `top` / file handles — not process memory. |
| **Active operator** (kernel access, root) | Everything casual can, plus read `/proc/<pid>/mem`, replace daemon binary, tamper with disk | Binary attestation breaks if they replace the daemon (catches it on next start). Signed audit log breaks if they tamper with data. **Live memory reads are NOT prevented** — they can read session keys + plaintext-in-flight. This is the floor. |
| **Infrastructure provider** (physical disk access only) | Image the disk | Full-disk encryption handles it. |
| **Infrastructure provider** (also has OS access) | Same as casual or active operator depending on level | Same defenses as the operator rows. |
| **Network observer** | Read traffic between phone and cloud box | TLS to the daemon; session-key challenge-response over TLS. |
| **Subject as adversary** (own phone, own cloud box, adversarial to a market they're being measured by) | Has all keys; can run modified daemons | Attestation chain catches modified binary; signed audit log catches in-place data tampering; behavior-vs-baseline markets (R3 §5.2.2) make sustained gaming costly. **Not protected by encryption** — the subject IS the keyholder. |

### What this design CAN promise

- "Cold disk reads return ciphertext." — Full-disk encryption + idle-zeroing.
- "The phone is the gatekeeper for whether the cloud box can decrypt anything." — TTL'd session keys released by the phone.
- "Burning the phone makes prior data unrecoverable." — Revocation destroys keys; no escrow.
- "Daemon binary is auditable." — Binary attestation registered on-chain at enrollment.
- "Tamper-with-data-on-disk is detectable." — Signed audit log; gaps refuse startup.
- "Compromise has a bounded window." — Active attacks last only as long as the current TTL'd session-key release; phone heartbeat going silent forces re-attestation; sustained absence triggers integrity lockout.

### What this design explicitly does NOT promise

- Live-memory protection from kernel-privileged attackers on the cloud
  box. The cloud box is not a TEE.
- That an actively-attacked cloud box keeps its data confidential during
  the attack window. The defense is "leaves traces, bounded window," not
  "impossible."
- Anything against the subject themselves where the subject is the
  attacker. Use attestation + audit + market-shape design (R3 §5).

If your security model needs anything stronger than the above, the
architecture has to change — TEE/SGX requirement, or homomorphic /
MPC-only operations on the cloud box. R3 took neither path.

## The two key-domain model

There are two key domains, owned by different hardware:

### Phone-side keys (SE-isolated)

Held in the phone's Secure Enclave / StrongBox. Never extracted.
Used for:

- **Master key derivation root.** All other keys descend from here.
- **Cross-palace federation key derivation.** When the phone authorizes
  a cross-palace match, it signs the session establishment.
- **Revocation authority.** Burning the phone destroys the master key
  handle; no other party can revoke.
- **Attestation chain leaf.** The phone's master key signs cloud-box
  session-key releases.

### Cloud-box-side keys (in process memory while daemon runs)

Loaded by phone challenge-response on daemon startup; idle-zeroed during
inactivity. Used for:

- **Drawer/Property decryption** during miner passes, ranker calls,
  retrieval. The daemon needs plaintext to operate.
- **Federation-egress derivation** when a federation match runs in the
  daemon's sandbox.
- **Disk-at-rest key** when the daemon mounts the encrypted volume.

These keys ARE in cloud-box memory. The defenses are TTL + idle-zero +
binary attestation + audit log, NOT hardware isolation.

The phone holds the master key; the cloud box holds session keys
released by the phone for a TTL.

## Where decryption can be done with phone-only keys (no cloud box)

The phone CAN decrypt some things directly without involving the cloud
box:

- The user's own drawers when displayed on the phone (the topology /
  drawer-view UX described in §"User-facing deletion + viewing" below).
- Findings emitted to the user's phone for review.
- Anything the user tap-decrypts during chunked plaintext export.

For these flows, the cloud box ships ciphertext + DEK handle to the
phone over TLS; the phone's SE decrypts; plaintext is shown on the
phone and discarded after view. The cloud box never sees plaintext for
phone-only-decryption flows.

The cloud box DOES need to decrypt for everything that runs on the cloud
box — miners, rankers, retrieval, federation. That's where the
session-key model applies.

## What needs to be encrypted

Same field-level boundaries as v1, but the table now distinguishes
ciphertext-on-disk-and-network from ciphertext-on-the-wire-only:

| Field | At rest | In phone↔cloud transit | In cloud-box memory |
|---|---|---|---|
| `DrawerCaptured.verbatim` | Ciphertext | Ciphertext | Plaintext during operation |
| `DrawerCaptured.audio_blob` | Ciphertext | Ciphertext | Plaintext during operation |
| `DrawerCaptured.semantic_embedding` | Ciphertext at rest | Ciphertext | Plaintext during operation (needed by similarity search) |
| `DrawerCaptured.paralinguistic_features` | Ciphertext at rest | Ciphertext | Plaintext during operation |
| `DrawerCaptured.state_context` | Ciphertext at rest | Ciphertext | Plaintext (used by views) |
| `DrawerCaptured.content_hash` | Plaintext (hash itself reveals nothing) | Plaintext | Plaintext |
| `DrawerCaptured.drawer_id` | Plaintext | Plaintext | Plaintext |
| `DrawerCaptured.duration_ms` | Plaintext | Plaintext | Plaintext |
| `DrawerCaptured.interactional` | Plaintext | Plaintext | Plaintext |
| `NodeCreated.properties` (assertions) | Encrypted per-property | Ciphertext | Plaintext when accessed |
| `EdgeCreated` everything | Plaintext | Plaintext | Plaintext (edges are pure structure) |
| Embedding indexes (HNSW etc.) | Ciphertext at rest | n/a | Plaintext during operation |

The structural metadata (IDs, hashes, edge shape, durations, kinds)
stays plaintext throughout because views need it. **The semantic
content** (verbatim, properties, blobs, embeddings) is ciphertext at
rest, ciphertext on the wire, plaintext only during cloud-box
operation under a TTL'd session key.

This is "field-level + full-disk" — same as v1 — but with the cloud-
box-memory column made explicit instead of waved away.

## Key hierarchy (revised)

The hierarchy is the same; the layering of "where each key lives" is
now explicit:

```
Manufacturer / Dev Root Key
       |
       | (signs)
       v
Phone Device Attestation Key                                  [PHONE SE]
       |
       | (derives)
       v
Phone Master Key                                              [PHONE SE]
       |                                  ┌─────────────────────────────────┐
       |                                  │ Phone-only decryption flows:    │
       |                                  │ - On-phone drawer view          │
       |                                  │ - Chunked plaintext export      │
       |                                  │ - User-tap-decrypt of single    │
       |                                  │   drawer/property               │
       |                                  └─────────────────────────────────┘
       |
       | (signs releases of)
       v
Cloud-box Session Key Bundle                                  [CLOUD-BOX MEMORY]
   |                                       ┌─ Released by phone via challenge-response
   |                                       ├─ TTL = 24h default (configurable)
   |                                       ├─ Idle-zeroed after configurable inactivity
   |                                       └─ Refreshed on heartbeat
   |
   +--> Drawer Encryption Keys (DEKs)         derived per drawer/burst
   +--> Property Encryption Key (PEK)         derived for assertion properties
   +--> Federation Egress Keys (FEKs)         derived per sandbox session
   +--> Disk-At-Rest Key (DARK)               derived for cold storage of substrate

Federation peer's Phone Master Key (during a match)
   |
   +--> Peer FEK                              negotiated for sandbox boundary
```

Two changes from v1:

1. The "keys never leave the secure element" line is **removed**. It
   was wrong for the cloud-box layer. The phone keys never leave the
   phone SE; the cloud-box session keys live in cloud-box memory.
2. The "Palace Master Key" → "Phone Master Key" rename. The master
   key is a phone-side key, not an abstract palace-wide one.

## What the device-attestation chain looks like (revised)

Same `mempalace/federate/attest.py` `AttestationChain` as v1. Two
chain-link kinds:

- Phone-side attestation: manufacturer root → phone device attestation
  key → phone master key → operations signed under it (federation
  signing, session-key release).
- Cloud-box attestation: phone master key → cloud-box daemon binary
  hash → cloud-box session key. The cloud box does NOT have an
  attestation root of its own; its attestation is rooted in the phone.

Revocation:
- Phone revocation destroys the phone master key; downstream session
  releases stop being valid; idle-zeroing finishes the job on the
  cloud-box side.
- Cloud-box compromise (binary mismatch, audit log gap) is detected by
  the daemon refusing to start. The phone won't release session keys
  to a daemon whose attestation doesn't match enrollment.

## Where encryption sits in the data flow (revised)

**Capture path (drawer enters the system):**

```
Phone capture surface
  → Phone SE encrypts under DEK derived from Phone Master Key
  → Phone ships (ciphertext, DEK_handle, attestation_sig) over TLS
  → Cloud box appends to log
```

The cloud box never sees plaintext at capture time. The DEK_handle is
opaque; the cloud box stores it.

**Local read path (cloud-box miner / ranker reads a drawer):**

```
Cloud-box daemon
  → Looks up ciphertext + DEK_handle in log
  → Loads session-key bundle (released by phone at startup, idle-zeroed
    if expired — see "session-key lifecycle" below)
  → Derives the per-drawer key from session-key bundle + DEK_handle
  → Decrypts to plaintext in process memory
  → Pass uses plaintext; plaintext goes out of scope
  → No explicit zeroize in v2 (the v1 explicit-zeroize was a Python
    fiction — Python doesn't expose memory-erasure primitives at the
    application layer; we rely on idle-zeroing of the session key
    plus shorter object lifetimes)
```

The cloud box has plaintext in memory while the operation runs. The
defense is the bounded TTL + idle-zero, not "never in memory."

**Federation path (cloud-box sandbox emits findings to a peer):**

```
Cloud-box session-key manager
  → Negotiates per-session FEK with peer's cloud box (sandbox-scoped)
  → Decrypts source fields from at-rest ciphertext under our DEK/PEK
  → Re-encrypts under FEK
  → Ships to peer over TLS
  → Peer's cloud box decrypts under their negotiated FEK in their
    sandbox boundary
  → Findings emerge per R3 §7.7
  → FEK destroyed at sandbox teardown
```

**Phone-only-decrypt path (user views a drawer on their phone):**

```
Phone display surface
  → Requests drawer-id over TLS
  → Cloud box ships (ciphertext, DEK_handle) to phone — note: the
    cloud box does NOT decrypt for this flow
  → Phone SE decrypts using Phone Master Key
  → Plaintext shown on phone, discarded after view
```

This last flow is what makes the user-facing deletion / view UX
described in §"User-facing deletion + viewing" possible without
involving the cloud box in plaintext exposure.

## Session-key lifecycle (new section)

The session-key bundle is the cloud-box-side counterpart to v1's
"keys never leave SE" claim. v2 makes its lifecycle explicit:

**Acquisition**: daemon startup performs phone challenge-response over
TLS. Phone signs a release containing the daemon's binary attestation
(must match the on-chain enrolled hash) plus a TTL (default 24h).
Phone returns a session-key bundle: derivations of DEK-key, PEK-key,
DARK-key valid for the TTL window.

**In-memory storage**: the bundle lives in a single allocated buffer
in the daemon's address space, with a generation counter. Operations
that need decryption read from this buffer.

**Idle detection**: a watchdog tracks "time since last operation."
Configurable threshold (default 15min). On threshold:
- Bundle buffer is overwritten with zeros AND deallocated.
- Daemon transitions to read-only-locked state (refuses any
  decryption operation).
- Outstanding operations that are mid-flight are allowed to complete;
  no new ones start.

**Resumption**: an incoming operation in read-only-locked state
triggers a fresh phone challenge-response. If phone is reachable,
new bundle issued, daemon resumes. If phone unreachable, request
returns "phone unavailable."

**Heartbeat refresh**: the phone's hourly heartbeat (R3 §7.6)
implicitly carries refresh authority. If the heartbeat arrives
before the TTL expires, the bundle is renewed in place without
the daemon needing to read it.

**Sustained absence**: 3 missed heartbeats → integrity lockout per
R3 §7.6 → on-chain trigger → bundle destroyed → return-staked-funds
flow runs.

## Schema changes (preserved from v1, with one addition)

`DrawerCaptured` event gains optional ciphertext fields:

```python
@dataclass
class DrawerCaptured(Event):
    # Existing fields unchanged...
    drawer_id: str = ""
    content_hash: str = ""
    capture_recorded_at: int = 0
    duration_ms: int = 0
    interactional: str = "memo_to_self"

    # Encryption-at-edge fields
    verbatim_ciphertext: bytes = b""
    verbatim_dek_handle: str = ""
    verbatim_attestation_sig: bytes = b""

    audio_blob_uri: str = ""
    audio_blob_dek_handle: str = ""

    semantic_embedding_ciphertext: bytes = b""
    semantic_embedding_fek_handle: str = ""

    encryption_schema_version: str = "v2"

    # NEW IN v2: which session-key bundle generation produced these
    # ciphertexts. Lets the cloud box detect when a stale bundle is
    # being used to decrypt fields that should be re-keyed.
    session_bundle_generation: int = 0
```

`Drawer.verbatim` is a property accessor that lazy-decrypts via the
current session-key bundle. If the daemon is in read-only-locked
state, the accessor raises `KeysNotLoaded` rather than returning
empty/error data.

## SecureElement interface (revised)

The interface gets two implementations, distinguished by where they
run:

```python
class PhoneSecureElement(Protocol):
    """Runs on the phone. Hardware-isolated. Production-grade."""

    def encrypt_drawer(self, plaintext: bytes, *, drawer_id: str) -> EncryptResult:
        """Phone-side capture encryption. Plaintext stays on the phone;
        ciphertext is what ships to the cloud box."""

    def decrypt_drawer(self, ciphertext: bytes, *, dek_handle: str) -> bytes:
        """Phone-only decryption (e.g., for in-app drawer viewing).
        The cloud box does NOT use this path."""

    def release_session_bundle(
        self,
        daemon_attestation: bytes,
        ttl_seconds: int,
    ) -> SessionKeyBundle:
        """Issued via challenge-response at daemon startup. Bundle is
        TTL'd and signed."""

    def revoke_palace(self) -> None:
        """Destroy the Phone Master Key. Downstream session releases
        become invalid; cloud-box bundles can't be refreshed; existing
        bundles expire on TTL; nothing recovers thereafter."""

class CloudBoxKeyManager(Protocol):
    """Runs on the cloud box. Software-only. Bundle lives in process
    memory while daemon runs."""

    def load_bundle(self, bundle: SessionKeyBundle) -> None:
        """Called once at daemon startup with a phone-issued bundle."""

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        dek_handle: str,
        attestation_sig: bytes,
    ) -> bytes:
        """Decrypt under the current bundle. Raises KeysNotLoaded if
        idle-zeroed."""

    def encrypt_for_egress(
        self,
        plaintext: bytes,
        *,
        sandbox_id: str,
        peer_pubkey: bytes,
    ) -> bytes:
        """Federation-egress encryption under a per-sandbox FEK."""

    def idle_zero(self) -> None:
        """Watchdog calls this on the inactivity threshold. After this,
        decrypt() raises KeysNotLoaded until a fresh bundle is loaded."""

    def is_loaded(self) -> bool:
        """For diagnostics; downstream code shouldn't branch on this."""
```

The v1 `SecureElement` Protocol that conflated phone and cloud-box
concerns is dropped.

## Where this composes with the 5-phase substrate (preserved)

- **Phase 1 (batch framing)**: encryption is per-event. A torn batch
  leaves some events ciphertext-formed. Recovery scan still works
  because batch_started/aborted are plaintext metadata.
- **Phase 2 (versioning)**: `VersionStamp.content_hash` is computed
  over plaintext. The hash is meaningful only inside the trust boundary
  (cloud box during operation). Operators can't compute the hash
  without the keys; no security loss to hashing plaintext.
- **Phase 3 (frontier)**: unchanged.
- **Phase 4 (dependency tracking)**: unchanged.
- **Phase 5 (DD on Rust)**: views index on hashes/IDs/edge structure.
  The few that need plaintext (full-text search) cross the
  CloudBoxKeyManager boundary explicitly.

## What this design does NOT yet do (revised)

1. **Searchable encryption.** Full-text search over encrypted drawers
   requires either local-plaintext indexing (which is what we have on
   the cloud box during operation) or homomorphic schemes (heavy, out
   of scope).
2. **Cross-palace federation without cloud-box plaintext.** Each side
   decrypts in its own cloud box during the sandbox session. A
   stronger model — peer-to-peer matching with no cloud-box plaintext
   ever — would require MPC. Out of scope for v2.
3. **Key escrow / recovery.** Revocation = destruction. No escrow.
   Opt-in user-managed backup is possible (R3 §10) but is a separate
   threat model.
4. **Live-memory protection on the cloud box.** Out of scope per the
   threat-model section above. The defense is TTL + idle-zero + audit,
   not memory isolation.

## User-facing deletion + viewing

### Three layers of view

The encryption boundary creates three layers of what's viewable on the
phone, each with different cost and decryption cost:

**Topology view (always available, no decryption).** The DAG structure
— nodes, edges, periods, themes, schemas — is plaintext per the
encryption boundary table. The phone browses the entire structure
without decrypting any drawers. Cost is megabytes; runs on any phone.
This is the default "what's in my mempalace" surface.

**Drawer view (on-demand, phone-side decryption).** Tapping a drawer
in the topology view triggers a phone-only-decrypt flow: cloud box
ships (ciphertext, DEK_handle) to the phone over TLS, phone SE
decrypts, plaintext displayed, discarded after view. Phone caches
recently-viewed drawers up to a budget (default ~100MB, LRU eviction).
The cloud box never sees plaintext for this flow.

**Full plaintext export (chunked, deliberate).** "Show me everything"
streams the log to the phone in chunks (default 1000 drawers per
chunk). 100k drawers takes minutes. Each chunk decrypted on the phone,
held briefly, discarded after the user moves on. The export flow is
deliberately gated behind extra confirmation because it materializes
substantial amounts of plaintext.

### Two-tier deletion

**Cheap-and-reversible deletion.** Phone emits a `drawer_invalidated`
or `node_invalidated` event. The event is appended to the log; views
respect it; retrieval no longer surfaces the drawer; markets relying
on the drawer get NULL evidence rather than the drawer. Reversible
later by emitting `drawer_revalidated`. The original ciphertext is
still in the log — invalidation is a substrate-level "do not surface,"
not erasure.

**Expensive-and-one-way erasure.** A separate `request_erase` event
queues a compaction job that rewrites the cold log to remove the
original ciphertext. This is what GDPR-style erasure requires; it is
costly because every snapshot/backup gets rewritten. UX is distinct
(extra confirmation, "are you sure" with a dwell time) because
accidentally pressing it is irreversible.

The two-tier model handles the common case (oh wait, I didn't mean to
record that) cheaply, and the rare case (delete this from existence)
expensively. Distinct UX surfaces them as distinct operations.

### Burning the palace

Triple-confirmation flow on the phone. Phone SE destroys the Phone
Master Key. All cloud-box session bundles become un-refreshable; on
their next TTL expiry they idle-zero and don't come back. Existing
ciphertext on disk becomes unrecoverable. On-chain `IntegrityLockout`
PDA fires and stake is returned per R3 §7.6.

This is the burn-device end-state. Different from `request_erase` in
that it kills *the entire palace* not just specific drawers.

## What needs your call before any of this gets built (revised)

1. **Field-level + full-disk + cloud-box-bundle is the right model?**
   v1 asked this assuming SE-grade isolation; v2 asks it knowing the
   cloud-box layer is software-only. The composite gives "cold reads
   ciphertext, live reads bounded by TTL." If you wanted unconditional
   live protection too, we'd need TEE/SGX (not the path R3 took) or
   homomorphic-only operations (not feasible for our workloads).
2. **Software CloudBoxKeyManager for development?** Real production
   uses the same software stack but with bundles released by a real
   phone SE; tests need a SoftwarePhoneSE that can issue bundles.
   Both ship; both log a stark warning when used in non-test contexts.
3. **Schema migration story.** Existing tests use plaintext `verbatim`.
   Declare them legacy and start fresh? My instinct: yes; production
   has no logs yet.
4. **`request_erase` rewrite cost.** A user erasure request kicks a
   compaction job that may take minutes-to-hours over a large log.
   UX flow: "queued for erasure; will complete by X" with a status
   surface. Confirm OK.
5. **Burn-the-palace UX confirmation.** Triple-confirm + dwell time +
   plaintext warning. Anything else? Hardware button hold? Two-device
   co-sign? My instinct: triple-confirm + dwell is enough; co-sign
   adds operational friction without buying much.

## Reality check (preserved + extended)

Same caveats as v1 plus:

- The TTL + idle-zero + heartbeat composition is straightforward to
  implement but operationally subtle. The state machine has 4 states
  (loaded-active, loaded-idle, locked-zeroed, locked-failed) and the
  transitions need careful testing against intermittent-network
  scenarios. R3 §7.6 already specifies the heartbeat envelope; this
  document refines what each heartbeat means for the bundle.
- The phone-only-decrypt flow assumes the phone has enough compute
  and battery for ad-hoc decryption. AES-GCM on a modern phone is
  microseconds per drawer; should be fine.
- The export-in-chunks flow needs UX care: a user who exports 100k
  drawers wants progress feedback, ability to pause, ability to abort
  partway. Not just a single "decrypting…" spinner.

These are implementation surprises, not design changes.

## Summary of the v1 → v2 delta

- **Removed**: "keys never leave the secure element" as a global
  invariant. True for the phone, false for the cloud box.
- **Added**: explicit "what each threat actor can and cannot do"
  table.
- **Added**: cloud-box session-key bundle lifecycle (load / TTL /
  idle-zero / refresh / sustained absence).
- **Added**: phone-only-decrypt flow (drawer view, chunked export).
- **Added**: two-tier deletion + burn-the-palace.
- **Renamed**: Palace Master Key → Phone Master Key.
- **Split**: SecureElement Protocol → PhoneSecureElement +
  CloudBoxKeyManager (different security primitives).
- **Preserved**: field-level encryption boundary table, key hierarchy
  (with explicit per-key home), schema additions, 5-phase
  composition.

If anything in v2 should be even sharper, edit before code.
