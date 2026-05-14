//! `active_iams` as a Differential Dataflow view (sub-slice D).
//!
//! Maintains role-edges from the self-entity. Powers I-am queries
//! ("I am someone who..." per Conway's working-self framework).
//!
//! # Key choice
//!
//! Unlike the per-node views, this one is keyed by **edge_id** —
//! each role-binding is an independent fact. Queries that ask for
//! "all current I-am bindings" iterate the snapshot. Legacy view's
//! `current()` method returns a `Vec<IamBinding>`; this DD version
//! exposes the same shape via `snapshot_bytes()`.
//!
//! # Operator chain
//!
//! Same shape as `current_edges`: filter to edge_created /
//! edge_invalidated where the source is the self-entity and the
//! kind is `role_in_period`. Reduce by edge_id; fold create +
//! optional invalidate into a single state. Invalidated bindings
//! are retracted from the trace (so `snapshot_bytes` returns only
//! current bindings).
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/active_iams.rs` exists until sub-slice F.

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

/// Self-entity identifier — must match the legacy view's constant
/// and the Python-side identifier convention.
pub const SELF_ENTITY_ID: &str = "ent_self_self0000";

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct IamBinding {
    pub edge_id: String,
    pub target_node_id: String,
    pub role: String,
    pub period_id: Option<String>,
    pub created_at_offset: LogOffset,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct EdgeCreatedPayload {
    edge_id: String,
    edge_kind: String,
    source_node_id: String,
    target_node_id: String,
    #[serde(default)]
    properties: serde_json::Value,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct EdgeInvalidatedPayload {
    edge_id: String,
}

#[derive(Clone, Debug)]
enum ParsedIamEvent {
    Created {
        target_node_id: String,
        role: String,
        period_id: Option<String>,
    },
    Invalidated,
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedIamEvent)> {
    match evt.kind.as_str() {
        "edge_created" => {
            let p: EdgeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            // Filter: only role_in_period edges from self-entity
            if p.edge_kind != "role_in_period" || p.source_node_id != SELF_ENTITY_ID {
                return None;
            }
            let role = p
                .properties
                .get("role")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let period_id = p
                .properties
                .get("period_id")
                .and_then(|v| v.as_str())
                .map(String::from);
            Some((
                p.edge_id,
                ParsedIamEvent::Created {
                    target_node_id: p.target_node_id,
                    role,
                    period_id,
                },
            ))
        }
        "edge_invalidated" => {
            let p: EdgeInvalidatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            Some((p.edge_id, ParsedIamEvent::Invalidated))
        }
        _ => None,
    }
}

/// Fold a list of events for one edge_id. Returns:
///   - `Some(binding)` if the latest seen state is "created and not
///      yet invalidated"
///   - `None` if invalidated or never created
fn fold_events(events: Vec<(LogOffset, ParsedIamEvent)>) -> Option<IamBinding> {
    let mut binding: Option<IamBinding> = None;
    let mut invalidated = false;
    for (offset, evt) in events {
        match evt {
            ParsedIamEvent::Created {
                target_node_id,
                role,
                period_id,
            } => {
                binding = Some(IamBinding {
                    edge_id: String::new(), // filled in by reduce-key context
                    target_node_id,
                    role,
                    period_id,
                    created_at_offset: offset,
                });
                invalidated = false;
            }
            ParsedIamEvent::Invalidated => {
                invalidated = true;
            }
        }
    }
    if invalidated {
        None
    } else {
        binding
    }
}

pub struct ActiveIamsView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, IamBinding>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl ActiveIamsView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for ActiveIamsView {
    fn name(&self) -> &'static str {
        "active_iams"
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

        let state_collection = keyed.reduce(|edge_id, input_events, output| {
            let mut events: Vec<(LogOffset, ParsedIamEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();
            events.sort_by(|a, b| a.0.cmp(&b.0));

            if let Some(mut b) = fold_events(events) {
                b.edge_id = edge_id.clone();
                output.push((b, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((edge_id, b), diff) in updates {
                if *diff > 0 {
                    snap.insert(edge_id.clone(), b.clone());
                } else if *diff < 0 {
                    if snap.get(edge_id) == Some(b) {
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

        Box::new(ActiveIamsSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct ActiveIamsSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, IamBinding>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl ActiveIamsSnapshotQuery {
    fn binding_to_json(b: &IamBinding) -> serde_json::Value {
        let mut m = serde_json::Map::new();
        m.insert("edge_id".to_string(), serde_json::Value::String(b.edge_id.clone()));
        m.insert(
            "target_node_id".to_string(),
            serde_json::Value::String(b.target_node_id.clone()),
        );
        m.insert("role".to_string(), serde_json::Value::String(b.role.clone()));
        m.insert(
            "period_id".to_string(),
            match &b.period_id {
                Some(p) => serde_json::Value::String(p.clone()),
                None => serde_json::Value::Null,
            },
        );
        m.insert(
            "created_at_offset".to_string(),
            serde_json::Value::from(b.created_at_offset),
        );
        serde_json::Value::Object(m)
    }
}

impl TraceQuery for ActiveIamsSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let b = snap.get(&key)?;
        serde_json::to_vec(&Self::binding_to_json(b)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(edge_id, b)| {
                let v = serde_json::to_vec(&Self::binding_to_json(b)).ok()?;
                Some((edge_id.as_bytes().to_vec(), v))
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
    fn parse_skips_non_self_source() {
        let evt = EventTuple {
            kind: "edge_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "e1",
                "edge_kind": "role_in_period",
                "source_node_id": "ent_other",
                "target_node_id": "schema_x",
            }))
            .unwrap(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn parse_skips_non_role_edge_kind() {
        let evt = EventTuple {
            kind: "edge_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "e1",
                "edge_kind": "contains",
                "source_node_id": SELF_ENTITY_ID,
                "target_node_id": "x",
            }))
            .unwrap(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn parse_extracts_role_in_period() {
        let evt = EventTuple {
            kind: "edge_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "e1",
                "edge_kind": "role_in_period",
                "source_node_id": SELF_ENTITY_ID,
                "target_node_id": "schema_engineer",
                "properties": {
                    "role": "engineer",
                    "period_id": "p_q4",
                },
            }))
            .unwrap(),
            offset: 5,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "e1");
        match parsed {
            ParsedIamEvent::Created {
                target_node_id,
                role,
                period_id,
            } => {
                assert_eq!(target_node_id, "schema_engineer");
                assert_eq!(role, "engineer");
                assert_eq!(period_id.as_deref(), Some("p_q4"));
            }
            _ => panic!("expected Created"),
        }
    }

    #[test]
    fn fold_invalidate_yields_none() {
        let events = vec![
            (
                1,
                ParsedIamEvent::Created {
                    target_node_id: "schema".to_string(),
                    role: "x".to_string(),
                    period_id: None,
                },
            ),
            (5, ParsedIamEvent::Invalidated),
        ];
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn fold_create_only_yields_binding() {
        let events = vec![(
            3,
            ParsedIamEvent::Created {
                target_node_id: "schema".to_string(),
                role: "x".to_string(),
                period_id: Some("p1".to_string()),
            },
        )];
        let b = fold_events(events).unwrap();
        assert_eq!(b.created_at_offset, 3);
        assert_eq!(b.role, "x");
        assert_eq!(b.period_id.as_deref(), Some("p1"));
    }
}
