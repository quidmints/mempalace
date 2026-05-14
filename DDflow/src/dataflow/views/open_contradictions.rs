//! `open_contradictions` as a Differential Dataflow view (sub-slice E).
//!
//! Contradicts edges that haven't been resolved. Surface for the
//! review-mode UI and for retrieval-side weighting.
//!
//! # Operator chain
//!
//! Keyed by `edge_id`. Each contradiction starts with a
//! `contradiction_asserted` event and is closed by a
//! `contradiction_resolved` event. The reduce closure folds events
//! per edge_id; if a resolution event exists for that edge_id, the
//! reduce emits nothing (DD retracts the entry from the trace).
//! This mirrors `canon_set`'s "emit Some only when membership
//! holds" pattern.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/open_contradictions.rs` exists until sub-slice F.
//!
//! Spec ref: Part 2.2, Part 10.6.

use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

// TODO(rust-build): same DD operator imports as the prior views.
use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

// =============================================================================
// OpenContradiction state
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct OpenContradiction {
    pub edge_id: String,
    pub contradicting_assertion_id: String,
    pub contradicted_assertion_id: String,
    pub detected_by: String,
    pub asserted_at_offset: LogOffset,
}

// =============================================================================
// Payload structs and parsing
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct AssertedPayload {
    edge_id: String,
    #[serde(default)]
    contradicting_assertion_id: String,
    #[serde(default)]
    contradicted_assertion_id: String,
    #[serde(default)]
    detected_by: String,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct ResolvedPayload {
    edge_id: String,
}

#[derive(Clone, Debug)]
enum ParsedContradictionEvent {
    Asserted(AssertedPayload),
    Resolved,
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedContradictionEvent)> {
    match evt.kind.as_str() {
        "contradiction_asserted" => {
            let p: AssertedPayload = serde_json::from_slice(&evt.payload).ok()?;
            let edge_id = p.edge_id.clone();
            Some((edge_id, ParsedContradictionEvent::Asserted(p)))
        }
        "contradiction_resolved" => {
            let p: ResolvedPayload = serde_json::from_slice(&evt.payload).ok()?;
            Some((p.edge_id, ParsedContradictionEvent::Resolved))
        }
        _ => None,
    }
}

/// Fold all events for an edge_id. Emits Some(state) only if the
/// edge is asserted and not subsequently resolved. This pattern
/// causes DD to retract resolved contradictions from the trace.
fn fold_events(events: Vec<(LogOffset, ParsedContradictionEvent)>) -> Option<OpenContradiction> {
    let mut sorted = events;
    sorted.sort_by_key(|(offset, _)| *offset);

    let mut current: Option<OpenContradiction> = None;
    for (offset, evt) in sorted {
        match evt {
            ParsedContradictionEvent::Asserted(p) => {
                current = Some(OpenContradiction {
                    edge_id: p.edge_id,
                    contradicting_assertion_id: p.contradicting_assertion_id,
                    contradicted_assertion_id: p.contradicted_assertion_id,
                    detected_by: p.detected_by,
                    asserted_at_offset: offset,
                });
            }
            ParsedContradictionEvent::Resolved => {
                // Resolution closes whatever was open. (If a later
                // re-assertion happens, the next iteration of this
                // loop would set `current` again — that's correct
                // bitemporal behavior.)
                current = None;
            }
        }
    }
    current
}

// =============================================================================
// View
// =============================================================================

pub struct OpenContradictionsView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, OpenContradiction>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl OpenContradictionsView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for OpenContradictionsView {
    fn name(&self) -> &'static str {
        "open_contradictions"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["contradiction_asserted", "contradiction_resolved"]
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
            let events: Vec<(LogOffset, ParsedContradictionEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();

            if let Some(state) = fold_events(events) {
                output.push((state, 1));
            }
            // If fold_events returns None, no output is pushed; the
            // entry is retracted from the trace.
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

        Box::new(OpenContradictionsQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery
// =============================================================================

struct OpenContradictionsQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, OpenContradiction>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for OpenContradictionsQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("edge_id".to_string(), serde_json::Value::String(s.edge_id.clone()));
        json.insert(
            "contradicting_assertion_id".to_string(),
            serde_json::Value::String(s.contradicting_assertion_id.clone()),
        );
        json.insert(
            "contradicted_assertion_id".to_string(),
            serde_json::Value::String(s.contradicted_assertion_id.clone()),
        );
        json.insert(
            "detected_by".to_string(),
            serde_json::Value::String(s.detected_by.clone()),
        );
        json.insert(
            "asserted_at_offset".to_string(),
            serde_json::Value::from(s.asserted_at_offset),
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
    fn parse_asserted_keys_by_edge_id() {
        let evt = EventTuple {
            kind: "contradiction_asserted".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "edg_c1",
                "contradicting_assertion_id": "asn_a",
                "contradicted_assertion_id": "asn_b",
                "detected_by": "miner_class2",
            }))
            .unwrap(),
            offset: 1,
        };
        let (key, _) = parse_event(&evt).unwrap();
        assert_eq!(key, "edg_c1");
    }

    #[test]
    fn parse_resolved_keys_by_edge_id() {
        let evt = EventTuple {
            kind: "contradiction_resolved".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "edg_c1",
            }))
            .unwrap(),
            offset: 5,
        };
        let (key, parsed) = parse_event(&evt).unwrap();
        assert_eq!(key, "edg_c1");
        assert!(matches!(parsed, ParsedContradictionEvent::Resolved));
    }

    #[test]
    fn fold_asserted_only_keeps_open() {
        let events = vec![(
            1,
            ParsedContradictionEvent::Asserted(AssertedPayload {
                edge_id: "edg_c1".to_string(),
                contradicting_assertion_id: "asn_a".to_string(),
                contradicted_assertion_id: "asn_b".to_string(),
                detected_by: "miner".to_string(),
            }),
        )];
        let s = fold_events(events).unwrap();
        assert_eq!(s.edge_id, "edg_c1");
        assert_eq!(s.asserted_at_offset, 1);
    }

    #[test]
    fn fold_resolved_returns_none() {
        let events = vec![
            (
                1,
                ParsedContradictionEvent::Asserted(AssertedPayload {
                    edge_id: "edg_c1".to_string(),
                    ..Default::default()
                }),
            ),
            (5, ParsedContradictionEvent::Resolved),
        ];
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn fold_re_asserted_after_resolution_reopens() {
        let events = vec![
            (
                1,
                ParsedContradictionEvent::Asserted(AssertedPayload {
                    edge_id: "edg_c1".to_string(),
                    ..Default::default()
                }),
            ),
            (5, ParsedContradictionEvent::Resolved),
            (
                10,
                ParsedContradictionEvent::Asserted(AssertedPayload {
                    edge_id: "edg_c1".to_string(),
                    detected_by: "miner_v2".to_string(),
                    ..Default::default()
                }),
            ),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.asserted_at_offset, 10);
        assert_eq!(s.detected_by, "miner_v2");
    }

    #[test]
    fn snapshot_query_legacy_shape() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "edg_c1".to_string(),
            OpenContradiction {
                edge_id: "edg_c1".to_string(),
                contradicting_assertion_id: "asn_a".to_string(),
                contradicted_assertion_id: "asn_b".to_string(),
                detected_by: "miner_class2".to_string(),
                asserted_at_offset: 7,
            },
        );

        let q = OpenContradictionsQuery { snapshot: snap, frontier };
        let bytes = q.query_bytes(b"edg_c1").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["edge_id"], "edg_c1");
        assert_eq!(v["contradicting_assertion_id"], "asn_a");
        assert_eq!(v["contradicted_assertion_id"], "asn_b");
        assert_eq!(v["detected_by"], "miner_class2");
        assert_eq!(v["asserted_at_offset"], 7);
    }
}
