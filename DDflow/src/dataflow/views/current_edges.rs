//! `current_edges` as a Differential Dataflow view (sub-slice C).
//!
//! Maintains `(edge_id → EdgeState)`. EdgeState carries bitemporal
//! validity (`valid_from`, `valid_to`, `recorded_at`, `invalidated_at`)
//! plus structural fields. The `is_active()` and `is_valid_at()`
//! helpers mirror the legacy view's API so consumers can switch
//! transparently.
//!
//! # Operator chain
//!
//! Same shape as `current_nodes`: filter → flat_map (key by edge_id)
//! → reduce → inspect_batch (snapshot mirror) → arrange_by_key.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/current_edges.rs` continues to exist until sub-slice F.
//! Both produce the same observable state from the same input.

use std::sync::{Arc, Mutex};

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

// TODO(rust-build): same operator imports as current_nodes
use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

// =============================================================================
// EdgeState — DD-compatible version
// =============================================================================

/// Edge state, identical in shape to the legacy view's `EdgeState`
/// (the legacy version is kept by reference; this version replaces
/// non-Eq fields with Eq-compatible representations).
///
/// - `weight` and `confidence` (f64) → `*_bits: u64` (IEEE bits)
/// - `properties: serde_json::Value` → `properties_json: String`
#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EdgeState {
    pub edge_id: String,
    pub edge_kind: String,
    pub source_node_id: String,
    pub target_node_id: String,
    pub valid_from: Option<u64>,
    pub valid_to: Option<u64>,
    pub recorded_at: u64,
    pub invalidated_at: Option<u64>,
    pub weight_bits: u64,
    pub confidence_bits: u64,
    pub derivation: String,
    pub properties_json: String,
    pub created_at_offset: LogOffset,
}

impl EdgeState {
    pub fn weight(&self) -> f64 {
        f64::from_bits(self.weight_bits)
    }
    pub fn confidence(&self) -> f64 {
        f64::from_bits(self.confidence_bits)
    }
    pub fn properties(&self) -> serde_json::Value {
        serde_json::from_str(&self.properties_json).unwrap_or(serde_json::Value::Null)
    }
    pub fn is_active(&self) -> bool {
        self.invalidated_at.is_none()
    }
    pub fn is_valid_at(&self, world_time_ms: u64) -> bool {
        if self.invalidated_at.is_some() {
            return false;
        }
        let from_ok = self.valid_from.map_or(true, |f| f <= world_time_ms);
        let to_ok = self.valid_to.map_or(true, |t| world_time_ms < t);
        from_ok && to_ok
    }
}

// =============================================================================
// Payload structs and parse logic
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default)]
struct EdgeCreatedPayload {
    edge_id: String,
    edge_kind: String,
    source_node_id: String,
    target_node_id: String,
    valid_from: Option<u64>,
    valid_to: Option<u64>,
    #[serde(default = "default_one")]
    weight: f64,
    #[serde(default = "default_one")]
    confidence: f64,
    #[serde(default = "default_derivation")]
    derivation: String,
    #[serde(default)]
    properties: serde_json::Value,
}

fn default_one() -> f64 {
    1.0
}
fn default_derivation() -> String {
    "OBSERVATION".to_string()
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct EdgeInvalidatedPayload {
    edge_id: String,
    #[serde(default)]
    reason: Option<String>,
}

#[derive(Clone, Debug)]
enum ParsedEdgeEvent {
    Created {
        offset: LogOffset,
        timestamp_ms: u64,
        payload: EdgeCreatedPayload,
    },
    Invalidated {
        timestamp_ms: u64,
    },
}

/// Parse an event tuple. The `EventTuple` doesn't carry timestamp_ms,
/// so we extract it from the payload's wrapper if present, otherwise
/// fall back to `offset` as a stand-in. The legacy view reads
/// `entry.timestamp_ms`, but DD's input is just the tuple. The
/// timestamp_ms is preserved by the Python-side feeder when it
/// constructs the EventTuple.
///
/// TODO(rust-build): when the Python side wires up `feed()`, it
/// needs to include `timestamp_ms` in the event. For sub-slice C
/// we encode it as a synthetic field in the payload — i.e. the
/// payload bytes include `_timestamp_ms` from the LogEntry. Confirm
/// this convention when sub-slice H wires up the feed surface.
fn parse_event(evt: &EventTuple) -> Option<(String, ParsedEdgeEvent)> {
    match evt.kind.as_str() {
        "edge_created" => {
            let p: EdgeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            // Extract timestamp_ms from a known wrapper field; default
            // to offset if absent. (See TODO above.)
            let v: serde_json::Value =
                serde_json::from_slice(&evt.payload).ok().unwrap_or_default();
            let ts = v.get("_timestamp_ms").and_then(|x| x.as_u64()).unwrap_or(evt.offset);
            let id = p.edge_id.clone();
            Some((
                id,
                ParsedEdgeEvent::Created {
                    offset: evt.offset,
                    timestamp_ms: ts,
                    payload: p,
                },
            ))
        }
        "edge_invalidated" => {
            let p: EdgeInvalidatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            let v: serde_json::Value =
                serde_json::from_slice(&evt.payload).ok().unwrap_or_default();
            let ts = v.get("_timestamp_ms").and_then(|x| x.as_u64()).unwrap_or(evt.offset);
            Some((p.edge_id, ParsedEdgeEvent::Invalidated { timestamp_ms: ts }))
        }
        _ => None,
    }
}

fn fold_events(events: Vec<(LogOffset, ParsedEdgeEvent)>) -> Option<EdgeState> {
    let mut state: Option<EdgeState> = None;
    for (_offset, evt) in events {
        match evt {
            ParsedEdgeEvent::Created {
                offset,
                timestamp_ms,
                payload,
            } => {
                state = Some(EdgeState {
                    edge_id: payload.edge_id,
                    edge_kind: payload.edge_kind,
                    source_node_id: payload.source_node_id,
                    target_node_id: payload.target_node_id,
                    valid_from: payload.valid_from,
                    valid_to: payload.valid_to,
                    recorded_at: timestamp_ms,
                    invalidated_at: None,
                    weight_bits: payload.weight.to_bits(),
                    confidence_bits: payload.confidence.to_bits(),
                    derivation: payload.derivation,
                    properties_json: serde_json::to_string(&payload.properties)
                        .unwrap_or_else(|_| "{}".to_string()),
                    created_at_offset: offset,
                });
            }
            ParsedEdgeEvent::Invalidated { timestamp_ms } => {
                if let Some(ref mut s) = state {
                    s.invalidated_at = Some(timestamp_ms);
                }
                // else: invalidate-without-create, no-op
            }
        }
    }
    state
}

// =============================================================================
// CurrentEdgesView — ViewSpec impl
// =============================================================================

pub struct CurrentEdgesView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, EdgeState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl CurrentEdgesView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for CurrentEdgesView {
    fn name(&self) -> &'static str {
        "current_edges"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["edge_created", "edge_invalidated"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(edge_id, parsed)| (edge_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(|_edge_id, input_events, output| {
            let mut events: Vec<(LogOffset, ParsedEdgeEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();
            events.sort_by(|a, b| a.0.cmp(&b.0));

            if let Some(state) = fold_events(events) {
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((edge_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(edge_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(edge_id) == Some(state) {
                        snap.remove(edge_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();
        // TODO(rust-build): retain `_arranged.trace` for true trace queries

        Box::new(EdgesSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery impl
// =============================================================================

struct EdgesSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, EdgeState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for EdgesSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        // Re-expand into legacy-shape JSON so consumers see the same fields
        let mut json = serde_json::Map::new();
        json.insert("edge_id".to_string(), serde_json::Value::String(s.edge_id.clone()));
        json.insert("edge_kind".to_string(), serde_json::Value::String(s.edge_kind.clone()));
        json.insert(
            "source_node_id".to_string(),
            serde_json::Value::String(s.source_node_id.clone()),
        );
        json.insert(
            "target_node_id".to_string(),
            serde_json::Value::String(s.target_node_id.clone()),
        );
        json.insert(
            "valid_from".to_string(),
            s.valid_from
                .map(serde_json::Value::from)
                .unwrap_or(serde_json::Value::Null),
        );
        json.insert(
            "valid_to".to_string(),
            s.valid_to
                .map(serde_json::Value::from)
                .unwrap_or(serde_json::Value::Null),
        );
        json.insert(
            "recorded_at".to_string(),
            serde_json::Value::from(s.recorded_at),
        );
        json.insert(
            "invalidated_at".to_string(),
            s.invalidated_at
                .map(serde_json::Value::from)
                .unwrap_or(serde_json::Value::Null),
        );
        json.insert("weight".to_string(), serde_json::Value::from(s.weight()));
        json.insert(
            "confidence".to_string(),
            serde_json::Value::from(s.confidence()),
        );
        json.insert(
            "derivation".to_string(),
            serde_json::Value::String(s.derivation.clone()),
        );
        json.insert("properties".to_string(), s.properties());
        json.insert(
            "created_at_offset".to_string(),
            serde_json::Value::from(s.created_at_offset),
        );
        json.insert(
            "is_active".to_string(),
            serde_json::Value::Bool(s.is_active()),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(edge_id, _)| {
                let v = self.query_bytes(edge_id.as_bytes())?;
                Some((edge_id.as_bytes().to_vec(), v))
            })
            .collect()
    }

    fn frontier_offset(&self) -> LogOffset {
        *self.frontier.lock()
    }
}

// =============================================================================
// Inline tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fold_create_then_invalidate() {
        let events = vec![
            (
                1,
                ParsedEdgeEvent::Created {
                    offset: 1,
                    timestamp_ms: 1000,
                    payload: EdgeCreatedPayload {
                        edge_id: "e1".to_string(),
                        edge_kind: "contains".to_string(),
                        source_node_id: "p1".to_string(),
                        target_node_id: "ev1".to_string(),
                        weight: 1.0,
                        confidence: 1.0,
                        derivation: "OBSERVATION".to_string(),
                        ..Default::default()
                    },
                },
            ),
            (
                2,
                ParsedEdgeEvent::Invalidated { timestamp_ms: 2000 },
            ),
        ];
        let state = fold_events(events).unwrap();
        assert!(!state.is_active());
        assert_eq!(state.invalidated_at, Some(2000));
        assert_eq!(state.recorded_at, 1000);
    }

    #[test]
    fn parse_event_creates_with_timestamp_default() {
        let evt = EventTuple {
            kind: "edge_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "e1",
                "edge_kind": "contains",
                "source_node_id": "p1",
                "target_node_id": "ev1",
            }))
            .unwrap(),
            offset: 5,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "e1");
        match parsed {
            ParsedEdgeEvent::Created { offset, timestamp_ms, .. } => {
                assert_eq!(offset, 5);
                // No _timestamp_ms in payload → falls back to offset
                assert_eq!(timestamp_ms, 5);
            }
            _ => panic!("expected Created"),
        }
    }

    #[test]
    fn parse_event_uses_explicit_timestamp_ms() {
        let evt = EventTuple {
            kind: "edge_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "e1",
                "edge_kind": "contains",
                "source_node_id": "p1",
                "target_node_id": "ev1",
                "_timestamp_ms": 9999,
            }))
            .unwrap(),
            offset: 5,
        };
        let (_, parsed) = parse_event(&evt).unwrap();
        match parsed {
            ParsedEdgeEvent::Created { timestamp_ms, .. } => {
                assert_eq!(timestamp_ms, 9999);
            }
            _ => panic!("expected Created"),
        }
    }

    #[test]
    fn is_valid_at_respects_window() {
        let s = EdgeState {
            edge_id: "e1".to_string(),
            edge_kind: "x".to_string(),
            source_node_id: "a".to_string(),
            target_node_id: "b".to_string(),
            valid_from: Some(1000),
            valid_to: Some(2000),
            recorded_at: 500,
            invalidated_at: None,
            weight_bits: 1.0f64.to_bits(),
            confidence_bits: 1.0f64.to_bits(),
            derivation: "OBSERVATION".to_string(),
            properties_json: "{}".to_string(),
            created_at_offset: 1,
        };
        assert!(!s.is_valid_at(500));   // before window
        assert!(s.is_valid_at(1000));   // window start (inclusive)
        assert!(s.is_valid_at(1500));   // inside
        assert!(!s.is_valid_at(2000));  // window end (exclusive)
    }

    #[test]
    fn is_valid_at_returns_false_when_invalidated() {
        let s = EdgeState {
            edge_id: "e1".to_string(),
            edge_kind: "x".to_string(),
            source_node_id: "a".to_string(),
            target_node_id: "b".to_string(),
            valid_from: None,
            valid_to: None,
            recorded_at: 500,
            invalidated_at: Some(1000),
            weight_bits: 1.0f64.to_bits(),
            confidence_bits: 1.0f64.to_bits(),
            derivation: "OBSERVATION".to_string(),
            properties_json: "{}".to_string(),
            created_at_offset: 1,
        };
        assert!(!s.is_valid_at(2000));
    }

    #[test]
    fn snapshot_query_returns_legacy_compatible_json() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "e1".to_string(),
            EdgeState {
                edge_id: "e1".to_string(),
                edge_kind: "contains".to_string(),
                source_node_id: "p1".to_string(),
                target_node_id: "ev1".to_string(),
                valid_from: Some(1000),
                valid_to: None,
                recorded_at: 500,
                invalidated_at: None,
                weight_bits: 0.8f64.to_bits(),
                confidence_bits: 0.9f64.to_bits(),
                derivation: "INFERENCE".to_string(),
                properties_json: "{}".to_string(),
                created_at_offset: 3,
            },
        );

        let q = EdgesSnapshotQuery { snapshot: snap, frontier };
        let bytes = q.query_bytes(b"e1").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["edge_id"], "e1");
        assert_eq!(v["edge_kind"], "contains");
        assert!((v["weight"].as_f64().unwrap() - 0.8).abs() < 1e-9);
        assert!((v["confidence"].as_f64().unwrap() - 0.9).abs() < 1e-9);
        assert_eq!(v["derivation"], "INFERENCE");
        assert_eq!(v["is_active"], true);
        assert_eq!(v["valid_from"], 1000);
        assert_eq!(v["valid_to"], serde_json::Value::Null);
    }
}
