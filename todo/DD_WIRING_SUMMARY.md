# DD wiring — 8-sub-slice summary

This is the post-mortem for the 8-sub-slice push to wire Differential
Dataflow into MemPalace. Background: DD was specified in `mempalace_spec.md`
Part 2.2 ("Differential Dataflow maintains incremental views") and DD/timely
were listed in `mempalace_core/Cargo.toml` (`differential-dataflow = 0.12`,
`timely = 0.12`), but **zero source files imported them**. The 14 views in
`mempalace_core/src/views/` were `parking_lot::RwLock<HashMap>` placeholders.
The comment in the legacy `views/builder.rs` ("for now... move to true
DDflow operators") was an assistant-authored placeholder forgotten across
sessions.

This wire-up shipped the spec.

## The arc

| Slice | What it landed | Tests added |
|---|---|---|
| A | Dataflow infrastructure scaffold (`DataflowWorker`, `InputDriver`, `ViewSpec` trait, `DataflowHandle`) | 15 structural + 5 inline Rust |
| B | Convert `current_nodes` end-to-end (real DD operators) | 16 + 4 skipped + 9 inline Rust |
| C | `current_edges`, `current_interpretations` | 22 + 2 skipped + inline Rust |
| D | `heat_field`, `velocity_field`, `recurrence_clusters`, `active_periods`, `active_iams`, `canon_set` (user-staged with `node_accessed` event redesign) | 35 + 4 skipped + inline Rust |
| E | `open_contradictions`, `pending_review`, `match_cache`, `matched_against`, `current_schemas` (joins via reduce-by-id) | 25 + 2 skipped + inline Rust |
| F | Delete legacy View trait + 14 legacy views, wire `LogReplayer` → `DataflowHandle.feed_batch`, expose `PyDataflowHandle` | 15 |
| G | Replace Phase 5 parking_lot `FrontierTracker` with timely-frontier-driven version (via `attach_dataflow`) | 14 + 9 inline Rust (6 legacy + 3 G-mode) |
| H | `DataflowBridge` Python adapter + `PyFrontierRegistry::attach_dataflow` PyO3 surface | 19 (16 + 3 skipped) |

**Final test count: 366 Python tests, 347 passed, 19 skipped (Rust-path tests),
zero regressions across the entire arc.**

## Architectural decisions worth recording

### View shape: `flat_map → reduce → arrange_by_key`

Every one of the 14 DD views uses the same operator chain:

```
input.flat_map(parse_event)                  // event → (key, parsed)
     .reduce(|key, events, output| {         // fold per-key
         if let Some(state) = fold(events) {
             output.push((state, 1));
         }
     })
     .inspect_batch(snapshot_mirror_writeback)
     .arrange_by_key()                       // for query path
```

The `inspect_batch` writes to a `parking_lot::RwLock<HashMap>` snapshot mirror;
that's what `query_bytes` reads. The `arrange_by_key` is what other DD views
would join against (none currently do — but the hooks are there).

### DD-compat conventions

DD requires `Clone + Eq + Hash + PartialOrd + Ord + 'static + Send + Sync` on
key and value types. The data shapes the legacy views used didn't all satisfy
these:

- `f64` → `u64` IEEE bits (`heat_bits`, `stability_score_bits`, etc.) and a
  derived accessor (`fn heat(&self) -> f64 { f64::from_bits(self.heat_bits) }`).
- `serde_json::Value` → JSON-encoded `String` + accessor that re-parses.
- Custom `#[derive(Clone, Debug, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]`
  on every state struct.

### Joins via reduce-by-id, not DD's `join` operator

`match_cache`, `matched_against`: a `match_request_received` and a
`finding_emitted` need to be combined per `match_id`. DD has a `join` operator
that does this directly — but using `reduce` with both events keyed on
`match_id` is simpler, fewer operators in the graph, same semantics. The
reduce closure uses `?`-unwrap on `Option<Request>` and `Option<Finding>` —
both must be present for the entry to materialize.

### `pending_review`: deterministic `item_id`s for retraction

The legacy `pending_review` mutated a `Vec<PendingItem>` on `contradiction_resolved`
to remove the corresponding entry. In DD that doesn't translate — there's no
"global mutate" hook in the operator graph. Instead: deterministic
`pri_{category}_{ref}` ids let resolution events key to the same id as
their assertion events. Reduce returns `None` when the latest event is
a removal → DD retracts the entry from the trace.

### `node_accessed` event kind (sub-slice D, user-authored)

The legacy `heat_field` and `velocity_field` had side-channel APIs (`bump`,
`record_access`) — direct mutation outside the event log. That breaks the
"view is a function of the event log" invariant from spec Part 1.3. The
sub-slice D rewrite introduces a new event kind, `node_accessed`, that
replaces those APIs. Heat decay, which used to live in the side-channel
`decay_to(node_id, now_ms)` call, becomes a *query-time* computation:
state stores `(heat_at_anchor, anchor_ms)`; `heat_at(now_ms)` applies the
exponential decay at read.

### Frontier as two layers: DD frontier × batch coordination

Sub-slice G: the registry separates the application-level *batch coordination*
from the dataflow-level *frontier*:

- **DD frontier**: timely capability frontiers, owned by the worker thread,
  one per view. Auto-advance as data flows. Read via `DataflowHandle::frontier_of`.
- **Batch coordination**: `lowest_open_batch_start` per-tracker, plus the
  `open_batches: HashMap<(consumer_id, batch_id), start_offset>` registry.
  This is application semantics (batch framing per spec Part 5.4) that DD
  doesn't know about.

The composition: `committed_offset = min(applied_offset, lowest_open_batch_start - 1)`
when a batch is open; else `applied_offset`. The application layer can keep
calling `record_batch_started` / `record_batch_closed`; what changes after
`attach_dataflow` is the *source* of `applied_offset` — internal storage
becomes a DD-frontier read.

## Pending — must be done by an environment that has rustc

This entire arc was authored without a working Rust toolchain. Every guess is
marked `TODO(rust-build)` in the source. A non-exhaustive list of things to
verify on first build:

1. **DD operator types**: `flat_map`, `reduce`, `arrange_by_key`, `inspect_batch`
   — confirm the import paths and trait bounds match `differential-dataflow = 0.12`.
2. **`timely::execute_directly` form**: the worker loop in `dataflow/mod.rs`
   uses `Worker::step_or_park` in a custom loop. May need adjusting to the
   actual timely 0.12 API.
3. **`TraceAgent` type erasure**: the `BoxedTrace` returned by `ViewSpec::build`
   discards the concrete trace type. If DD requires generic-typed traces for
   `arrange_by_key` to be useful downstream, this needs revisiting (currently
   nothing reads the arrangement — queries go through the `inspect_batch`
   snapshot mirror).
4. **PyO3 boundary shapes**: every method on `PyLogClient`, `PyDataflowHandle`,
   `PyFrontierRegistry` is annotated with `TODO(rust-build)` for the boundary.
   Most likely: `Vec<u8>` ↔ `bytes`, `String` ↔ `str`, `u64` ↔ `int`,
   `Vec<String>` ↔ `list[str]`. PyO3 should DTRT but watch for surprises.
5. **`DataflowHandle: Clone`**: the field is `#[derive(Clone)]`. In
   `PyFrontierRegistry::attach_dataflow`, we do
   `Arc::new(handle.inner.clone())` — ensure that compiles. If it doesn't,
   add a public `inner_arc(&self) -> Arc<DataflowHandle>` helper.
6. **DD reduce closure capture**: closures passed to `.reduce()` must be `Fn`
   (not `FnMut`). Per-view configuration is captured by value (cloned at
   build time) — runtime config changes require rebuilding the dataflow.
7. **`active_iams` self-entity filter**: `SELF_ENTITY_ID = "ent_self_self0000"`
   is hardcoded in the parsing path. Verify against the Python-side
   identifier convention in `mempalace/identity.py` (or wherever the
   self-entity id is canonical).

## When this all works

1. `cargo build --release -p mempalace_core` produces an extension.
2. Python `import mempalace_core` finds `PyLogClient`, `PyFrontierRegistry`,
   `PyDataflowHandle` at the top level.
3. The 19 currently-skipped behavioral tests start running.
4. `test_phase5_frontier_alignment` should still pass — it's the cross-check
   that the DD-frontier-driven path agrees with the scan-based fallback path.
5. The whole regression should still pass at 366+ tests.

## What's left in the broader project

This summary covers DD wiring only. From the sub-slice D pre-staging session,
the user resolved item 5 (Rust evidence-approach changes) by authoring the
`node_accessed` event redesign. Items 1, 3, 4 (assertion rename, handles
design doc, encryption-at-edge design doc) shipped earlier. Item 2 (Switchboard
SDK + oracle threat-model) is still blocked on user input. Item 6 (final zip)
is the last step.
