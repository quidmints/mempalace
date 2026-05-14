//! `current_interpretations` as a Differential Dataflow view (sub-slice C).
//!
//! Maintains `((node_id, field_name) → InterpretationState)`. Each
//! `interpretation_assigned` event fully specifies the new value plus
//! its pass-attribution and confidence. Within DD, this is the
//! simplest reduce shape — pick the latest event per key by offset.
//!
//! # Operator chain
//!
//! Same shape as `current_nodes` and `current_edges`:
//!   filter → flat_map (key by composite "node_id::field") → reduce
//!   (latest by offset wins) → inspect_batch → arrange_by_key.
//!
//! # Compound key
//!
//! The key is the literal string `"{node_id}::{field_name}"`. This
//! matches the legacy view's keying so query bytes can be the same.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/current_interpretations.rs` continues to exist
//! until sub-slice F.

use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

// TODO(rust-build): same operator imports as the other DD views
use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

// =============================================================================
// InterpretationState — DD-compatible
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct InterpretationState {
    pub node_id: String,
    pub field_name: String,
    /// JSON-encoded for DD compat (Hash/Ord)
    pub value_json: String,
    pub miner_pass_version: String,
    pub confidence_bits: u64,
    pub assigned_at_offset: LogOffset,
    pub supersedes_event_id: Option<String>,
}

impl InterpretationState {
    pub fn confidence(&self) -> f64 {
        f64::from_bits(self.confidence_bits)
    }
    pub fn value(&self) -> serde_json::Value {
        serde_json::from_str(&self.value_json).unwrap_or(serde_json::Value::Null)
    }
}

// =============================================================================
// Payload
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default)]
struct InterpretationPayload {
    node_id: String,
    field_name: String,
    new_value: serde_json::Value,
    #[serde(default)]
    supersedes_event_id: Option<String>,
    #[serde(default)]
    miner_pass_version: String,
    #[serde(default = "default_confidence")]
    confidence: f64,
}

fn default_confidence() -> f64 {
    1.0
}

fn parse_event(evt: &EventTuple) -> Option<(String, InterpretationPayload)> {
    if evt.kind != "interpretation_assigned" {
        return None;
    }
    let p: InterpretationPayload = serde_json::from_slice(&evt.payload).ok()?;
    let key = format!("{}::{}", p.node_id, p.field_name);
    Some((key, p))
}

/// Pick the latest event by offset and convert it to state.
fn pick_latest(events: Vec<(LogOffset, InterpretationPayload)>) -> Option<InterpretationState> {
    let (offset, p) = events.into_iter().max_by_key(|(o, _)| *o)?;
    Some(InterpretationState {
        node_id: p.node_id,
        field_name: p.field_name,
        value_json: serde_json::to_string(&p.new_value).unwrap_or_else(|_| "null".to_string()),
        miner_pass_version: p.miner_pass_version,
        confidence_bits: p.confidence.to_bits(),
        assigned_at_offset: offset,
        supersedes_event_id: p.supersedes_event_id,
    })
}

// =============================================================================
// CurrentInterpretationsView — ViewSpec impl
// =============================================================================

pub struct CurrentInterpretationsView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, InterpretationState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl CurrentInterpretationsView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for CurrentInterpretationsView {
    fn name(&self) -> &'static str {
        "current_interpretations"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["interpretation_assigned"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(key, p)| (key, (evt.offset, p)))
        });

        let state_collection = keyed.reduce(|_key, input_events, output| {
            let events: Vec<(LogOffset, InterpretationPayload)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref p), _)| (offset, p.clone()))
                .collect();

            if let Some(state) = pick_latest(events) {
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((key, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(key.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(key) == Some(state) {
                        snap.remove(key);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(InterpretationsSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery impl
// =============================================================================

struct InterpretationsSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, InterpretationState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for InterpretationsSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("node_id".to_string(), serde_json::Value::String(s.node_id.clone()));
        json.insert(
            "field_name".to_string(),
            serde_json::Value::String(s.field_name.clone()),
        );
        json.insert("value".to_string(), s.value());
        json.insert(
            "miner_pass_version".to_string(),
            serde_json::Value::String(s.miner_pass_version.clone()),
        );
        json.insert(
            "confidence".to_string(),
            serde_json::Value::from(s.confidence()),
        );
        json.insert(
            "assigned_at_offset".to_string(),
            serde_json::Value::from(s.assigned_at_offset),
        );
        json.insert(
            "supersedes_event_id".to_string(),
            match &s.supersedes_event_id {
                Some(eid) => serde_json::Value::String(eid.clone()),
                None => serde_json::Value::Null,
            },
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(key, _)| {
                let v = self.query_bytes(key.as_bytes())?;
                Some((key.as_bytes().to_vec(), v))
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
    fn parse_keys_compose_node_id_and_field() {
        let evt = EventTuple {
            kind: "interpretation_assigned".to_string(),
            payload: serde_json::to_vec(&json!({
                "node_id": "n1",
                "field_name": "memory_type",
                "new_value": "episodic",
            }))
            .unwrap(),
            offset: 1,
        };
        let (key, _) = parse_event(&evt).unwrap();
        assert_eq!(key, "n1::memory_type");
    }

    #[test]
    fn parse_event_skips_unrelated_kinds() {
        let evt = EventTuple {
            kind: "node_created".to_string(),
            payload: b"{}".to_vec(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn pick_latest_chooses_highest_offset() {
        let events = vec![
            (
                1,
                InterpretationPayload {
                    node_id: "n1".to_string(),
                    field_name: "f".to_string(),
                    new_value: json!("first"),
                    confidence: 0.5,
                    ..Default::default()
                },
            ),
            (
                5,
                InterpretationPayload {
                    node_id: "n1".to_string(),
                    field_name: "f".to_string(),
                    new_value: json!("latest"),
                    confidence: 0.9,
                    ..Default::default()
                },
            ),
            (
                3,
                InterpretationPayload {
                    node_id: "n1".to_string(),
                    field_name: "f".to_string(),
                    new_value: json!("middle"),
                    confidence: 0.7,
                    ..Default::default()
                },
            ),
        ];
        let state = pick_latest(events).unwrap();
        assert_eq!(state.assigned_at_offset, 5);
        assert_eq!(state.value(), json!("latest"));
        assert!((state.confidence() - 0.9).abs() < 1e-9);
    }

    #[test]
    fn pick_latest_returns_none_for_empty() {
        let events: Vec<(LogOffset, InterpretationPayload)> = vec![];
        assert!(pick_latest(events).is_none());
    }

    #[test]
    fn snapshot_query_legacy_shape() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "n1::memory_type".to_string(),
            InterpretationState {
                node_id: "n1".to_string(),
                field_name: "memory_type".to_string(),
                value_json: r#""episodic""#.to_string(),
                miner_pass_version: "class1_v3".to_string(),
                confidence_bits: 0.85f64.to_bits(),
                assigned_at_offset: 7,
                supersedes_event_id: Some("evt_42".to_string()),
            },
        );

        let q = InterpretationsSnapshotQuery {
            snapshot: snap,
            frontier,
        };
        let bytes = q.query_bytes(b"n1::memory_type").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["node_id"], "n1");
        assert_eq!(v["field_name"], "memory_type");
        assert_eq!(v["value"], "episodic");
        assert_eq!(v["miner_pass_version"], "class1_v3");
        assert!((v["confidence"].as_f64().unwrap() - 0.85).abs() < 1e-9);
        assert_eq!(v["assigned_at_offset"], 7);
        assert_eq!(v["supersedes_event_id"], "evt_42");
    }
}
