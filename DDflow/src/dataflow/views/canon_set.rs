//! `canon_set` as a Differential Dataflow view (sub-slice D).
//!
//! Membership in canon: `node_id → CanonState` for nodes with
//! `canonical=true`. Driven by:
//!   - `node_created` with `canonical=true` → insert
//!   - `node_property_set("canonical", true)` → insert
//!   - `node_property_set("canonical", false)` → retract
//!   - `node_property_set("canon_path", ...)` → update path
//!   - `node_property_set("structural_leverage", ...)` → update leverage
//!
//! Same `flat_map → reduce → arrange_by_key` operator chain as the
//! sub-slice B/C views. The reduce closure folds events in offset
//! order; the final state is `Some` if the latest known canonical
//! flag is true, `None` if it's false (which causes DD to retract
//! the entry from the trace).
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/canon_set.rs` exists until sub-slice F.

use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct CanonState {
    pub node_id: String,
    pub node_kind: String,
    pub canon_path: String,
    pub structural_leverage_bits: u64,
    pub since_offset: LogOffset,
}

impl CanonState {
    pub fn structural_leverage(&self) -> f64 {
        f64::from_bits(self.structural_leverage_bits)
    }
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct CreatedPayload {
    node_id: String,
    node_kind: String,
    #[serde(default)]
    canonical: bool,
    #[serde(default)]
    canon_path: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct PropertySetPayload {
    node_id: String,
    field_name: String,
    new_value: serde_json::Value,
}

/// Internal event type after parsing.
#[derive(Clone, Debug)]
enum ParsedCanonEvent {
    Created(CreatedPayload),
    Canonical(bool),
    CanonPath(String),
    StructuralLeverage(f64),
    /// Property-set event we don't care about. Kept so reduce's
    /// input still includes it (no-op fold step).
    Other,
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedCanonEvent)> {
    match evt.kind.as_str() {
        "node_created" => {
            let p: CreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            let id = p.node_id.clone();
            Some((id, ParsedCanonEvent::Created(p)))
        }
        "node_property_set" => {
            let p: PropertySetPayload = serde_json::from_slice(&evt.payload).ok()?;
            let id = p.node_id.clone();
            let parsed = match p.field_name.as_str() {
                "canonical" => p
                    .new_value
                    .as_bool()
                    .map(ParsedCanonEvent::Canonical)
                    .unwrap_or(ParsedCanonEvent::Other),
                "canon_path" => p
                    .new_value
                    .as_str()
                    .map(|s| ParsedCanonEvent::CanonPath(s.to_string()))
                    .unwrap_or(ParsedCanonEvent::Other),
                "structural_leverage" => p
                    .new_value
                    .as_f64()
                    .map(ParsedCanonEvent::StructuralLeverage)
                    .unwrap_or(ParsedCanonEvent::Other),
                _ => ParsedCanonEvent::Other,
            };
            Some((id, parsed))
        }
        _ => None,
    }
}

/// Fold events for one node into the latest canon state. Returns
/// `None` when the node should be absent from canon_set (either
/// never canonical, or explicitly set to `canonical=false`).
fn fold_events(events: Vec<(LogOffset, ParsedCanonEvent)>) -> Option<CanonState> {
    // We track "is currently canonical" + a working state object.
    let mut is_canonical: bool = false;
    let mut state: Option<CanonState> = None;
    for (offset, evt) in events {
        match evt {
            ParsedCanonEvent::Created(p) => {
                if p.canonical {
                    is_canonical = true;
                    state = Some(CanonState {
                        node_id: p.node_id,
                        node_kind: p.node_kind,
                        canon_path: p.canon_path.unwrap_or_default(),
                        structural_leverage_bits: 0u64,
                        since_offset: offset,
                    });
                } else {
                    // Created but not canonical — track basic identity
                    // in case a later property_set flips it on.
                    state = Some(CanonState {
                        node_id: p.node_id,
                        node_kind: p.node_kind,
                        canon_path: p.canon_path.unwrap_or_default(),
                        structural_leverage_bits: 0u64,
                        since_offset: offset,
                    });
                    is_canonical = false;
                }
            }
            ParsedCanonEvent::Canonical(true) => {
                if state.is_none() {
                    // Canonical flipped on without prior create.
                    // Track minimal state.
                    state = Some(CanonState {
                        node_id: String::new(),
                        node_kind: String::new(),
                        canon_path: String::new(),
                        structural_leverage_bits: 0u64,
                        since_offset: offset,
                    });
                }
                is_canonical = true;
                if let Some(ref mut s) = state {
                    s.since_offset = offset;
                }
            }
            ParsedCanonEvent::Canonical(false) => {
                is_canonical = false;
            }
            ParsedCanonEvent::CanonPath(path) => {
                if let Some(ref mut s) = state {
                    s.canon_path = path;
                }
            }
            ParsedCanonEvent::StructuralLeverage(v) => {
                if let Some(ref mut s) = state {
                    s.structural_leverage_bits = v.to_bits();
                }
            }
            ParsedCanonEvent::Other => {}
        }
    }
    if is_canonical {
        state
    } else {
        None
    }
}

pub struct CanonSetView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, CanonState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl CanonSetView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for CanonSetView {
    fn name(&self) -> &'static str {
        "canon_set"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["node_created", "node_property_set"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(node_id, parsed)| (node_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(|_node_id, input_events, output| {
            let mut events: Vec<(LogOffset, ParsedCanonEvent)> = input_events
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
            for ((node_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(node_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(node_id) == Some(state) {
                        snap.remove(node_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(CanonSetSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct CanonSetSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, CanonState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for CanonSetSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("node_id".to_string(), serde_json::Value::String(s.node_id.clone()));
        json.insert(
            "node_kind".to_string(),
            serde_json::Value::String(s.node_kind.clone()),
        );
        json.insert(
            "canon_path".to_string(),
            serde_json::Value::String(s.canon_path.clone()),
        );
        json.insert(
            "structural_leverage".to_string(),
            serde_json::Value::from(s.structural_leverage()),
        );
        json.insert(
            "since_offset".to_string(),
            serde_json::Value::from(s.since_offset),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(node_id, _)| {
                let v = self.query_bytes(node_id.as_bytes())?;
                Some((node_id.as_bytes().to_vec(), v))
            })
            .collect()
    }

    fn frontier_offset(&self) -> LogOffset {
        *self.frontier.lock()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fold_created_canonical_emits_state() {
        let events = vec![(
            1,
            ParsedCanonEvent::Created(CreatedPayload {
                node_id: "n1".to_string(),
                node_kind: "schema".to_string(),
                canonical: true,
                canon_path: Some("soul/loyalty.md".to_string()),
            }),
        )];
        let s = fold_events(events).unwrap();
        assert_eq!(s.node_id, "n1");
        assert_eq!(s.canon_path, "soul/loyalty.md");
    }

    #[test]
    fn fold_created_non_canonical_then_flipped_on() {
        let events = vec![
            (
                1,
                ParsedCanonEvent::Created(CreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    canonical: false,
                    canon_path: None,
                }),
            ),
            (5, ParsedCanonEvent::Canonical(true)),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.node_id, "n1");
        assert_eq!(s.since_offset, 5);
    }

    #[test]
    fn fold_canonical_then_off_yields_none() {
        let events = vec![
            (
                1,
                ParsedCanonEvent::Created(CreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    canonical: true,
                    canon_path: None,
                }),
            ),
            (3, ParsedCanonEvent::Canonical(false)),
        ];
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn canon_path_update_applied() {
        let events = vec![
            (
                1,
                ParsedCanonEvent::Created(CreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    canonical: true,
                    canon_path: None,
                }),
            ),
            (
                2,
                ParsedCanonEvent::CanonPath("architecture/x.md".to_string()),
            ),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.canon_path, "architecture/x.md");
    }

    #[test]
    fn structural_leverage_update_applied() {
        let events = vec![
            (
                1,
                ParsedCanonEvent::Created(CreatedPayload {
                    node_id: "n1".to_string(),
                    node_kind: "schema".to_string(),
                    canonical: true,
                    canon_path: None,
                }),
            ),
            (2, ParsedCanonEvent::StructuralLeverage(0.85)),
        ];
        let s = fold_events(events).unwrap();
        assert!((s.structural_leverage() - 0.85).abs() < 1e-9);
    }

    #[test]
    fn parse_event_picks_node_id_from_canonical_set() {
        let evt = EventTuple {
            kind: "node_property_set".to_string(),
            payload: serde_json::to_vec(&json!({
                "node_id": "n1",
                "field_name": "canonical",
                "new_value": true,
            }))
            .unwrap(),
            offset: 1,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "n1");
        assert!(matches!(parsed, ParsedCanonEvent::Canonical(true)));
    }
}
