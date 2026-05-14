//! `current_nodes` as a Differential Dataflow view (sub-slice B).
//!
//! The legacy `current_nodes` view (in `views/current_nodes.rs`) keeps
//! a `parking_lot::RwLock<HashMap>` and mutates it on each `apply()`.
//! This module implements the same semantics as a DD `ViewSpec` —
//! incremental, frontier-aware, queryable through a trace.
//!
//! # Operator chain
//!
//! ```text
//! input: Collection<EventTuple, time=LogOffset, diff=isize>
//!     |
//!     | filter to {node_created, node_property_set}
//!     v
//! keyed: Collection<(node_id, EventTuple)>
//!     |
//!     | reduce by node_id
//!     v
//! state: Collection<(node_id, NodeState)>
//!     |
//!     | arrange_by_key
//!     v
//! trace: Trace<node_id -> NodeState>
//! ```
//!
//! The `reduce` closure folds all events ever seen for a node into the
//! latest state, in offset order. DD's incremental machinery means
//! only re-folding the keys whose input changed — not the full graph.
//!
//! # Coexistence with the legacy view
//!
//! Both `views/current_nodes.rs` (legacy) and this DD view exist
//! simultaneously during sub-slices B–F. They produce the same
//! observable state from the same input. The Python side starts
//! reading from the DD view in sub-slice H; the legacy view is
//! deleted in sub-slice F once nothing reads from it.
//!
//! # TODO(rust-build) marker count
//!
//! Several. The DD operator API (specifically `reduce` and
//! `arrange_by_key`) has trait-bound complexity that's hard to get
//! right without a build. Marked per call site below.

use std::cmp::Ordering;
use std::sync::{Arc, Mutex};

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

// TODO(rust-build): confirm DD operator imports for 0.12. The forms
// I expect:
//   - differential_dataflow::operators::Reduce::reduce
//   - differential_dataflow::operators::arrange::ArrangeByKey::arrange_by_key
//   - differential_dataflow::trace::TraceReader (for reading the arrangement)
use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::trace::TraceReader;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

// =============================================================================
// NodeState — the per-key value the trace stores
// =============================================================================

/// State for a single node, identical in shape to the legacy view's
/// `NodeState` so consumers can switch between them transparently.
///
/// TODO(rust-build): DD's `reduce` operator requires the value type
/// to be `Data` (= `Clone + ExchangeData + Hashable`). `Hashable`
/// is not auto-implemented; we may need a manual impl. Confirm.
#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct NodeState {
    pub node_id: String,
    pub node_kind: String,
    /// Stored as a JSON-encoded string (not `serde_json::Value`) so
    /// the type implements `Hash` and `Ord`, which DD requires for
    /// values flowing through `reduce`/`arrange`. The legacy view
    /// stores a `serde_json::Value` directly; the conversion happens
    /// at the read boundary in `TraceQuery::query_bytes`.
    pub properties_json: String,
    pub canonical: bool,
    pub canon_path: Option<String>,
    /// Float importance — but `f64` is not `Eq`. We store as the
    /// IEEE-754 bit pattern so `NodeState` can be a DD value.
    /// Conversion: `f64::from_bits(state.importance_bits)`.
    pub importance_bits: u64,
    pub created_at_offset: LogOffset,
    pub last_modified_at_offset: LogOffset,
}

impl NodeState {
    pub fn importance(&self) -> f64 {
        f64::from_bits(self.importance_bits)
    }

    pub fn set_importance(&mut self, v: f64) {
        self.importance_bits = v.to_bits();
    }

    /// Decode the JSON properties bag. Returns `Value::Null` on
    /// malformed JSON.
    pub fn properties(&self) -> serde_json::Value {
        serde_json::from_str(&self.properties_json).unwrap_or(serde_json::Value::Null)
    }
}

// =============================================================================
// Payload structs — what we deserialize from EventTuple.payload bytes
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeCreatedPayload {
    node_id: String,
    node_kind: String,
    #[serde(default)]
    properties: serde_json::Value,
    #[serde(default)]
    canonical: bool,
    #[serde(default)]
    canon_path: Option<String>,
    #[serde(default)]
    importance: f64,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodePropertySetPayload {
    node_id: String,
    field_name: String,
    new_value: serde_json::Value,
}

/// Small enum that classifies an event for the reduce step. Avoids
/// re-parsing the JSON inside the reduce closure.
#[derive(Clone, Debug)]
enum ParsedEvent {
    Created(NodeCreatedPayload),
    PropertySet(NodePropertySetPayload),
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedEvent)> {
    match evt.kind.as_str() {
        "node_created" => {
            let p: NodeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            let id = p.node_id.clone();
            Some((id, ParsedEvent::Created(p)))
        }
        "node_property_set" => {
            let p: NodePropertySetPayload = serde_json::from_slice(&evt.payload).ok()?;
            let id = p.node_id.clone();
            Some((id, ParsedEvent::PropertySet(p)))
        }
        _ => None,
    }
}

/// Fold a list of `(offset, ParsedEvent)` (already sorted by offset)
/// into the resulting `NodeState`. Returns `None` if the fold begins
/// with a property_set with no preceding created (orphan property
/// set — invariant violation, but we tolerate it as no-op).
fn fold_events(events: Vec<(LogOffset, ParsedEvent)>) -> Option<NodeState> {
    let mut state: Option<NodeState> = None;
    for (offset, evt) in events {
        match evt {
            ParsedEvent::Created(p) => {
                state = Some(NodeState {
                    node_id: p.node_id,
                    node_kind: p.node_kind,
                    properties_json: serde_json::to_string(&p.properties)
                        .unwrap_or_else(|_| "{}".to_string()),
                    canonical: p.canonical,
                    canon_path: p.canon_path,
                    importance_bits: p.importance.to_bits(),
                    created_at_offset: offset,
                    last_modified_at_offset: offset,
                });
            }
            ParsedEvent::PropertySet(p) => {
                if let Some(ref mut s) = state {
                    // Update the JSON properties bag
                    let mut props: serde_json::Value =
                        serde_json::from_str(&s.properties_json)
                            .unwrap_or(serde_json::Value::Object(serde_json::Map::new()));
                    if let serde_json::Value::Object(ref mut map) = props {
                        map.insert(p.field_name.clone(), p.new_value.clone());
                    }
                    s.properties_json = serde_json::to_string(&props)
                        .unwrap_or_else(|_| "{}".to_string());

                    // Top-level field special-cases
                    match p.field_name.as_str() {
                        "canonical" => {
                            if let Some(v) = p.new_value.as_bool() {
                                s.canonical = v;
                            }
                        }
                        "canon_path" => {
                            s.canon_path = p.new_value.as_str().map(|x| x.to_string());
                        }
                        "importance" => {
                            if let Some(v) = p.new_value.as_f64() {
                                s.set_importance(v);
                            }
                        }
                        _ => {}
                    }
                    s.last_modified_at_offset = offset;
                }
                // else: orphan property_set, no-op
            }
        }
    }
    state
}

// =============================================================================
// CurrentNodesView — the ViewSpec implementation
// =============================================================================

pub struct CurrentNodesView {
    /// Shared trace handle the dataflow worker writes into during
    /// `build()`. Queries read from this. The handle is wrapped in
    /// `Mutex<Option<...>>` because it's populated *during* the
    /// `worker.dataflow(|scope| ...)` call, but the `TraceQuery`
    /// reader needs to outlive that scope.
    ///
    /// TODO(rust-build): the actual trace-handle type for an
    /// `arrange_by_key` output is `TraceAgent<...>` parameterized
    /// over the inner trace storage (Spine, etc.). The exact type
    /// is hard to write out — `arrange_by_key` returns
    /// `Arranged<S, TraceAgent<...>>` and you take `.trace` from
    /// that. Replace `TraceHandle` here with the real type once
    /// the build error tells us what it is.
    trace_holder: Arc<Mutex<Option<TraceHandle>>>,

    /// Snapshot mirror of the trace contents. Updated whenever the
    /// dataflow's frontier advances. Used by `query_bytes` so we
    /// don't have to navigate a `TraceCursor` per call (cursors are
    /// involved enough that even when they work, the simpler
    /// snapshot path is fine for current scale).
    snapshot: Arc<RwLock<std::collections::HashMap<String, NodeState>>>,

    /// Highest log offset the dataflow has fully processed for this
    /// view. Updated by the worker's frontier-advance hook.
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

/// TODO(rust-build): real type is something like
/// `differential_dataflow::operators::arrange::TraceAgent<
///     differential_dataflow::trace::implementations::ord::OrdValSpine<
///         String, NodeState, LogOffset, isize
///     >
/// >`. We use a placeholder here so the rest of the file compiles
/// structurally. Replace with the real type on first build —
/// the build error will name it.
#[allow(dead_code)]
struct TraceHandle {
    // Placeholder. The real handle is a TraceAgent that supports
    // .read_upper(), .cursor_through(), etc.
    _placeholder: (),
}

impl CurrentNodesView {
    pub fn new() -> Self {
        Self {
            trace_holder: Arc::new(Mutex::new(None)),
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }

    /// Helper used by the build closure to obtain a clonable
    /// snapshot-writer. The dataflow's `inspect` operator (added by
    /// `build()`) writes batches into this on every frontier advance.
    fn snapshot_writer(&self) -> Arc<RwLock<std::collections::HashMap<String, NodeState>>> {
        Arc::clone(&self.snapshot)
    }

    fn frontier_writer(&self) -> Arc<parking_lot::Mutex<LogOffset>> {
        Arc::clone(&self.frontier)
    }
}

impl ViewSpec for CurrentNodesView {
    fn name(&self) -> &'static str {
        "current_nodes"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["node_created", "node_property_set"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot = self.snapshot_writer();
        let frontier = self.frontier_writer();

        // Step 1: parse + key by node_id. Drop events that don't
        // belong to this view (defensive — the framework's
        // subscribed_kinds filter should already have done this, but
        // belt-and-braces).
        //
        // TODO(rust-build): `flat_map` returns a `Collection`; if
        // DD 0.12 named it differently, fix here.
        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(node_id, parsed)| (node_id, (evt.offset, parsed)))
        });

        // Step 2: reduce by node_id. Fold all events for a key into
        // the latest NodeState.
        //
        // TODO(rust-build): `reduce` signature in DD 0.12:
        //   fn reduce<L, V2, R2>(self, logic: L) -> Collection<...>
        //   where L: Fn(&K, &[(&V, R)], &mut Vec<(V2, R2)>) + 'static
        //
        // The closure receives the key and a slice of (value, count)
        // and pushes outputs. The `count` is the diff; for our
        // purposes (each event seen once), we ignore counts and just
        // collect the events.
        let state_collection = keyed.reduce(|_node_id, input_events, output| {
            // input_events is &[(&(LogOffset, ParsedEvent), isize)].
            // We collect the events with positive count and sort by
            // offset before folding.
            let mut events: Vec<(LogOffset, ParsedEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();
            events.sort_by(|a, b| a.0.cmp(&b.0));

            if let Some(state) = fold_events(events) {
                output.push((state, 1));
            }
        });

        // Step 3: inspect — every time the dataflow advances, mirror
        // the latest values into the snapshot map. This is what
        // `query_bytes` reads from.
        //
        // TODO(rust-build): `inspect_batch` signature in DD/timely:
        //   fn inspect_batch<F>(self, f: F) -> Collection<...>
        //   where F: FnMut(&G::Timestamp, &[(D, R)]) + 'static
        //
        // We get called once per batch with the current time and the
        // (data, diff) updates. We apply the diffs to the snapshot
        // map: positive diff = insert, negative diff = remove.
        let snapshot_inspect = Arc::clone(&snapshot);
        let frontier_inspect = Arc::clone(&frontier);
        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((node_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(node_id.clone(), state.clone());
                } else if *diff < 0 {
                    // The previous version of this state is being
                    // retracted. Only remove if the snapshot still
                    // has *exactly* this version — guards against
                    // out-of-order retractions racing with inserts.
                    if snap.get(node_id) == Some(state) {
                        snap.remove(node_id);
                    }
                }
            }
            // Update frontier
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        // Step 4: arrange by key for the trace handle.
        //
        // TODO(rust-build): `arrange_by_key` returns
        // `Arranged<S, TraceAgent<...>>`. We take `.trace` to get
        // the trace handle. The exact method name might be
        // `arrange_by_key()` or `arrange_by_key_named("name")`.
        let _arranged = state_collection.arrange_by_key();
        // TODO(rust-build): store `_arranged.trace` into
        // self.trace_holder so external readers can navigate it.
        // Skipped for sub-slice B: the snapshot path covers our
        // current query needs.

        // Return the TraceQuery view. We use a snapshot-backed reader
        // (not a trace cursor) for simplicity — the snapshot is
        // updated by `inspect_batch` above.
        Box::new(SnapshotTraceQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// SnapshotTraceQuery — TraceQuery impl backed by the snapshot map
// =============================================================================

struct SnapshotTraceQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, NodeState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for SnapshotTraceQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        // Key is the node_id as UTF-8 bytes.
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let state = snap.get(&key)?;
        // Encode the value as JSON, expanding properties_json back to
        // a Value so consumers see the same shape as the legacy view.
        let mut json = serde_json::Map::new();
        json.insert("node_id".to_string(), serde_json::Value::String(state.node_id.clone()));
        json.insert("node_kind".to_string(), serde_json::Value::String(state.node_kind.clone()));
        json.insert("properties".to_string(), state.properties());
        json.insert("canonical".to_string(), serde_json::Value::Bool(state.canonical));
        json.insert(
            "canon_path".to_string(),
            match &state.canon_path {
                Some(p) => serde_json::Value::String(p.clone()),
                None => serde_json::Value::Null,
            },
        );
        json.insert(
            "importance".to_string(),
            serde_json::Value::from(state.importance()),
        );
        json.insert(
            "created_at_offset".to_string(),
            serde_json::Value::from(state.created_at_offset),
        );
        json.insert(
            "last_modified_at_offset".to_string(),
            serde_json::Value::from(state.last_modified_at_offset),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(node_id, _state)| {
                let v = self.query_bytes(node_id.as_bytes())?;
                Some((node_id.as_bytes().to_vec(), v))
            })
            .collect()
    }

    fn frontier_offset(&self) -> LogOffset {
        *self.frontier.lock()
    }
}

// =============================================================================
// Inline tests — DD-side
//
// These tests don't exercise the real DD operators (that requires
// timely::execute_directly which is in the worker main loop). They
// verify the helper functions: parse_event, fold_events, the
// SnapshotTraceQuery serialization roundtrip. The end-to-end DD test
// lives on the Python side (test_dataflow_subslice_b.py) and runs
// when the extension is built.
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn evt(kind: &str, offset: LogOffset, payload: serde_json::Value) -> EventTuple {
        EventTuple {
            kind: kind.to_string(),
            payload: serde_json::to_vec(&payload).unwrap(),
            offset,
        }
    }

    #[test]
    fn parse_event_picks_node_id() {
        let e = evt(
            "node_created",
            5,
            json!({"node_id": "n1", "node_kind": "entity"}),
        );
        let (id, parsed) = parse_event(&e).unwrap();
        assert_eq!(id, "n1");
        match parsed {
            ParsedEvent::Created(p) => assert_eq!(p.node_kind, "entity"),
            _ => panic!("expected Created"),
        }
    }

    #[test]
    fn parse_event_skips_unrelated_kinds() {
        let e = evt("drawer_captured", 1, json!({"drawer_id": "d1"}));
        assert!(parse_event(&e).is_none());
    }

    #[test]
    fn fold_events_built_from_create_only() {
        let events = vec![(
            1,
            ParsedEvent::Created(NodeCreatedPayload {
                node_id: "n1".to_string(),
                node_kind: "entity".to_string(),
                importance: 0.5,
                ..Default::default()
            }),
        )];
        let state = fold_events(events).unwrap();
        assert_eq!(state.node_id, "n1");
        assert_eq!(state.node_kind, "entity");
        assert!((state.importance() - 0.5).abs() < 1e-9);
        assert_eq!(state.created_at_offset, 1);
        assert_eq!(state.last_modified_at_offset, 1);
    }

    #[test]
    fn fold_events_applies_property_set_after_create() {
        let events = vec![
            (
                1,
                ParsedEvent::Created(NodeCreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    importance: 0.3,
                    ..Default::default()
                }),
            ),
            (
                5,
                ParsedEvent::PropertySet(NodePropertySetPayload {
                    node_id: "n1".to_string(),
                    field_name: "importance".to_string(),
                    new_value: json!(0.9),
                }),
            ),
        ];
        let state = fold_events(events).unwrap();
        assert!((state.importance() - 0.9).abs() < 1e-9);
        assert_eq!(state.last_modified_at_offset, 5);
        // created_at stays at the original offset
        assert_eq!(state.created_at_offset, 1);
    }

    #[test]
    fn fold_events_orphan_property_set_is_noop() {
        // PropertySet without preceding Created → no state emitted
        let events = vec![(
            1,
            ParsedEvent::PropertySet(NodePropertySetPayload {
                node_id: "n1".to_string(),
                field_name: "x".to_string(),
                new_value: json!(1),
            }),
        )];
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn fold_events_applies_canonical_field() {
        let events = vec![
            (
                1,
                ParsedEvent::Created(NodeCreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    canonical: false,
                    ..Default::default()
                }),
            ),
            (
                2,
                ParsedEvent::PropertySet(NodePropertySetPayload {
                    node_id: "n1".to_string(),
                    field_name: "canonical".to_string(),
                    new_value: json!(true),
                }),
            ),
        ];
        let state = fold_events(events).unwrap();
        assert!(state.canonical);
    }

    #[test]
    fn snapshot_query_returns_json_with_expected_fields() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "n1".to_string(),
            NodeState {
                node_id: "n1".to_string(),
                node_kind: "entity".to_string(),
                properties_json: r#"{"name":"Alice"}"#.to_string(),
                canonical: false,
                canon_path: None,
                importance_bits: 0.7f64.to_bits(),
                created_at_offset: 1,
                last_modified_at_offset: 5,
            },
        );

        let q = SnapshotTraceQuery {
            snapshot: Arc::clone(&snap),
            frontier: Arc::clone(&frontier),
        };

        let bytes = q.query_bytes(b"n1").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["node_id"], "n1");
        assert_eq!(v["node_kind"], "entity");
        assert_eq!(v["properties"]["name"], "Alice");
        assert!((v["importance"].as_f64().unwrap() - 0.7).abs() < 1e-9);
        assert_eq!(v["created_at_offset"], 1);
        assert_eq!(v["last_modified_at_offset"], 5);
    }

    #[test]
    fn snapshot_query_returns_none_for_unknown_key() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));
        let q = SnapshotTraceQuery {
            snapshot: snap,
            frontier,
        };
        assert!(q.query_bytes(b"nobody").is_none());
    }

    #[test]
    fn snapshot_bytes_returns_all_entries() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(10u64));

        for id in &["n1", "n2", "n3"] {
            snap.write().insert(
                id.to_string(),
                NodeState {
                    node_id: id.to_string(),
                    node_kind: "entity".to_string(),
                    properties_json: "{}".to_string(),
                    ..Default::default()
                },
            );
        }

        let q = SnapshotTraceQuery {
            snapshot: Arc::clone(&snap),
            frontier: Arc::clone(&frontier),
        };
        let all = q.snapshot_bytes();
        assert_eq!(all.len(), 3);
        // Frontier reflects the writer
        assert_eq!(q.frontier_offset(), 10);
    }
}
