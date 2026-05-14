//! `active_periods` as a Differential Dataflow view (sub-slice D).
//!
//! Periods with state ∈ {open, closed_recent}. Driven by:
//!   - `node_created` of kind `period` → insert with initial state
//!   - `node_property_set("state", ...)` → state transition
//!   - `node_property_set("ended_at", ...)` → record closure time
//!   - `node_property_set("precedence", ...)` → update precedence
//!
//! The "recently_closed" filter (`ended_at >= now - 7d`) is applied
//! at *query time*, not in the reduce closure, because the reduce
//! has no notion of "now". This matches the legacy view's approach.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/active_periods.rs` exists until sub-slice F.

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
pub struct ActivePeriodState {
    pub period_id: String,
    pub theme_id: Option<String>,
    pub name: String,
    pub state: String, // "open" | "closed" | "sealed"
    pub started_at_ms: Option<u64>,
    pub ended_at_ms: Option<u64>,
    pub precedence: i32,
    pub last_change_offset: LogOffset,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeCreatedPayload {
    node_id: String,
    node_kind: String,
    #[serde(default)]
    properties: serde_json::Value,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct PropertySetPayload {
    node_id: String,
    field_name: String,
    new_value: serde_json::Value,
}

#[derive(Clone, Debug)]
enum ParsedPeriodEvent {
    Created {
        theme_id: Option<String>,
        name: String,
        started_at_ms: Option<u64>,
        precedence: i32,
        state: String,
    },
    StateChange(String),
    EndedAt(Option<u64>),
    Precedence(i32),
    Other,
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedPeriodEvent)> {
    match evt.kind.as_str() {
        "node_created" => {
            let p: NodeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            if p.node_kind != "period" {
                return None;
            }
            let theme_id = p
                .properties
                .get("theme_id")
                .and_then(|v| v.as_str())
                .map(String::from);
            let name = p
                .properties
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let started_at_ms = p.properties.get("started_at").and_then(|v| v.as_u64());
            let precedence = p
                .properties
                .get("precedence")
                .and_then(|v| v.as_i64())
                .unwrap_or(0) as i32;
            let state = p
                .properties
                .get("state")
                .and_then(|v| v.as_str())
                .unwrap_or("open")
                .to_string();
            Some((
                p.node_id,
                ParsedPeriodEvent::Created {
                    theme_id,
                    name,
                    started_at_ms,
                    precedence,
                    state,
                },
            ))
        }
        "node_property_set" => {
            let p: PropertySetPayload = serde_json::from_slice(&evt.payload).ok()?;
            let id = p.node_id.clone();
            let parsed = match p.field_name.as_str() {
                "state" => p
                    .new_value
                    .as_str()
                    .map(|s| ParsedPeriodEvent::StateChange(s.to_string()))
                    .unwrap_or(ParsedPeriodEvent::Other),
                "ended_at" => ParsedPeriodEvent::EndedAt(p.new_value.as_u64()),
                "precedence" => p
                    .new_value
                    .as_i64()
                    .map(|v| ParsedPeriodEvent::Precedence(v as i32))
                    .unwrap_or(ParsedPeriodEvent::Other),
                _ => ParsedPeriodEvent::Other,
            };
            Some((id, parsed))
        }
        _ => None,
    }
}

fn fold_events(events: Vec<(LogOffset, ParsedPeriodEvent)>) -> Option<ActivePeriodState> {
    let mut state: Option<ActivePeriodState> = None;
    for (offset, evt) in events {
        match evt {
            ParsedPeriodEvent::Created {
                theme_id,
                name,
                started_at_ms,
                precedence,
                state: s,
            } => {
                state = Some(ActivePeriodState {
                    period_id: String::new(), // filled in by reduce-key context
                    theme_id,
                    name,
                    state: s,
                    started_at_ms,
                    ended_at_ms: None,
                    precedence,
                    last_change_offset: offset,
                });
            }
            ParsedPeriodEvent::StateChange(s) => {
                if let Some(ref mut p) = state {
                    p.state = s;
                    p.last_change_offset = offset;
                }
            }
            ParsedPeriodEvent::EndedAt(t) => {
                if let Some(ref mut p) = state {
                    p.ended_at_ms = t;
                    p.last_change_offset = offset;
                }
            }
            ParsedPeriodEvent::Precedence(v) => {
                if let Some(ref mut p) = state {
                    p.precedence = v;
                    p.last_change_offset = offset;
                }
            }
            ParsedPeriodEvent::Other => {}
        }
    }
    state
}

pub struct ActivePeriodsView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, ActivePeriodState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl ActivePeriodsView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for ActivePeriodsView {
    fn name(&self) -> &'static str {
        "active_periods"
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
            parse_event(&evt).map(|(period_id, parsed)| (period_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(|period_id, input_events, output| {
            let mut events: Vec<(LogOffset, ParsedPeriodEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();
            events.sort_by(|a, b| a.0.cmp(&b.0));

            if let Some(mut state) = fold_events(events) {
                state.period_id = period_id.clone();
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((period_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(period_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(period_id) == Some(state) {
                        snap.remove(period_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(ActivePeriodsSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct ActivePeriodsSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, ActivePeriodState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for ActivePeriodsSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("period_id".to_string(), serde_json::Value::String(s.period_id.clone()));
        json.insert(
            "theme_id".to_string(),
            match &s.theme_id {
                Some(t) => serde_json::Value::String(t.clone()),
                None => serde_json::Value::Null,
            },
        );
        json.insert("name".to_string(), serde_json::Value::String(s.name.clone()));
        json.insert("state".to_string(), serde_json::Value::String(s.state.clone()));
        json.insert(
            "started_at_ms".to_string(),
            s.started_at_ms
                .map(serde_json::Value::from)
                .unwrap_or(serde_json::Value::Null),
        );
        json.insert(
            "ended_at_ms".to_string(),
            s.ended_at_ms
                .map(serde_json::Value::from)
                .unwrap_or(serde_json::Value::Null),
        );
        json.insert(
            "precedence".to_string(),
            serde_json::Value::from(s.precedence),
        );
        json.insert(
            "last_change_offset".to_string(),
            serde_json::Value::from(s.last_change_offset),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(period_id, _)| {
                let v = self.query_bytes(period_id.as_bytes())?;
                Some((period_id.as_bytes().to_vec(), v))
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
    fn parse_skips_non_period_node_created() {
        let evt = EventTuple {
            kind: "node_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "node_id": "n1",
                "node_kind": "entity",
            }))
            .unwrap(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn parse_period_created_extracts_props() {
        let evt = EventTuple {
            kind: "node_created".to_string(),
            payload: serde_json::to_vec(&json!({
                "node_id": "p1",
                "node_kind": "period",
                "properties": {
                    "theme_id": "th_work",
                    "name": "Q4 push",
                    "started_at": 1700000000_u64,
                    "precedence": 5,
                    "state": "open",
                },
            }))
            .unwrap(),
            offset: 1,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "p1");
        match parsed {
            ParsedPeriodEvent::Created {
                theme_id,
                name,
                started_at_ms,
                precedence,
                state,
            } => {
                assert_eq!(theme_id.as_deref(), Some("th_work"));
                assert_eq!(name, "Q4 push");
                assert_eq!(started_at_ms, Some(1700000000));
                assert_eq!(precedence, 5);
                assert_eq!(state, "open");
            }
            _ => panic!("expected Created"),
        }
    }

    #[test]
    fn fold_state_change() {
        let events = vec![
            (
                1,
                ParsedPeriodEvent::Created {
                    theme_id: None,
                    name: "x".to_string(),
                    started_at_ms: Some(1000),
                    precedence: 0,
                    state: "open".to_string(),
                },
            ),
            (2, ParsedPeriodEvent::StateChange("closed".to_string())),
            (3, ParsedPeriodEvent::EndedAt(Some(3000))),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.state, "closed");
        assert_eq!(s.ended_at_ms, Some(3000));
        assert_eq!(s.last_change_offset, 3);
    }

    #[test]
    fn fold_orphan_state_change_noop() {
        let events = vec![(1, ParsedPeriodEvent::StateChange("open".to_string()))];
        // No Created → no state. Reduce won't emit anything.
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn fold_precedence_update() {
        let events = vec![
            (
                1,
                ParsedPeriodEvent::Created {
                    theme_id: None,
                    name: "x".to_string(),
                    started_at_ms: None,
                    precedence: 0,
                    state: "open".to_string(),
                },
            ),
            (2, ParsedPeriodEvent::Precedence(7)),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.precedence, 7);
    }
}
