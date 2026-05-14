//! `current_schemas` as a Differential Dataflow view (sub-slice E).
//!
//! Each schema's latest induced version. Driven by `schema_induced`
//! events. Schema versions form a supersession chain via the
//! `supersedes_schema_id` field; this view stores all versions
//! keyed by `schema_node_id`, and the consumer's `active_heads()`
//! computation runs over the snapshot.
//!
//! # Operator chain
//!
//! Same shape as `current_nodes`: filter → flat_map (key by
//! schema_node_id) → reduce (latest event by offset wins) →
//! arrange_by_key. Each `schema_induced` for a given
//! `schema_node_id` fully replaces the prior state.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/current_schemas.rs` exists until sub-slice F.
//!
//! Spec ref: Part 2.2, Part 3.1.

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
// SchemaState — DD-compatible
// =============================================================================

/// One schema's latest induced version. The legacy `SchemaState`
/// uses `f64` and `Vec<String>`; the DD version stores floats as
/// IEEE bits (Eq/Hash compat) and `Vec<String>` is fine because
/// `Vec` is `Hash + Ord` over a `Hash + Ord` element type.
#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SchemaState {
    pub schema_node_id: String,
    pub schema_kind: String,
    pub name: String,
    pub description: String,
    pub stability_score_bits: u64,
    pub coverage_score_bits: u64,
    pub miner_pass_version: String,
    pub induced_at_offset: LogOffset,
    pub supersedes_schema_id: Option<String>,
    pub derived_from_events: Vec<String>,
    pub derived_from_assertions: Vec<String>,
    pub derived_from_drawers: Vec<String>,
}

impl SchemaState {
    pub fn stability_score(&self) -> f64 {
        f64::from_bits(self.stability_score_bits)
    }
    pub fn coverage_score(&self) -> f64 {
        f64::from_bits(self.coverage_score_bits)
    }
}

// =============================================================================
// Payload
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct SchemaInducedPayload {
    schema_node_id: String,
    #[serde(default)]
    schema_kind: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    miner_pass_version: String,
    #[serde(default)]
    stability_score: f64,
    #[serde(default)]
    coverage_score: f64,
    #[serde(default)]
    supersedes_schema_id: Option<String>,
    #[serde(default)]
    derived_from_events: Vec<String>,
    #[serde(default)]
    derived_from_assertions: Vec<String>,
    #[serde(default)]
    derived_from_drawers: Vec<String>,
}

fn parse_event(evt: &EventTuple) -> Option<(String, SchemaInducedPayload)> {
    if evt.kind != "schema_induced" {
        return None;
    }
    let p: SchemaInducedPayload = serde_json::from_slice(&evt.payload).ok()?;
    let key = p.schema_node_id.clone();
    Some((key, p))
}

/// Pick the highest-offset event for a schema and convert to state.
fn pick_latest(events: Vec<(LogOffset, SchemaInducedPayload)>) -> Option<SchemaState> {
    let (offset, p) = events.into_iter().max_by_key(|(o, _)| *o)?;
    Some(SchemaState {
        schema_node_id: p.schema_node_id,
        schema_kind: p.schema_kind,
        name: p.name,
        description: p.description,
        stability_score_bits: p.stability_score.to_bits(),
        coverage_score_bits: p.coverage_score.to_bits(),
        miner_pass_version: p.miner_pass_version,
        induced_at_offset: offset,
        supersedes_schema_id: p.supersedes_schema_id,
        derived_from_events: p.derived_from_events,
        derived_from_assertions: p.derived_from_assertions,
        derived_from_drawers: p.derived_from_drawers,
    })
}

// =============================================================================
// CurrentSchemasView
// =============================================================================

pub struct CurrentSchemasView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, SchemaState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl CurrentSchemasView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for CurrentSchemasView {
    fn name(&self) -> &'static str {
        "current_schemas"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["schema_induced"]
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
            let events: Vec<(LogOffset, SchemaInducedPayload)> = input_events
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
            for ((schema_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(schema_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(schema_id) == Some(state) {
                        snap.remove(schema_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(SchemasSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery
// =============================================================================

struct SchemasSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, SchemaState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for SchemasSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert(
            "schema_node_id".to_string(),
            serde_json::Value::String(s.schema_node_id.clone()),
        );
        json.insert(
            "schema_kind".to_string(),
            serde_json::Value::String(s.schema_kind.clone()),
        );
        json.insert("name".to_string(), serde_json::Value::String(s.name.clone()));
        json.insert(
            "description".to_string(),
            serde_json::Value::String(s.description.clone()),
        );
        json.insert(
            "stability_score".to_string(),
            serde_json::Value::from(s.stability_score()),
        );
        json.insert(
            "coverage_score".to_string(),
            serde_json::Value::from(s.coverage_score()),
        );
        json.insert(
            "miner_pass_version".to_string(),
            serde_json::Value::String(s.miner_pass_version.clone()),
        );
        json.insert(
            "induced_at_offset".to_string(),
            serde_json::Value::from(s.induced_at_offset),
        );
        json.insert(
            "supersedes_schema_id".to_string(),
            match &s.supersedes_schema_id {
                Some(x) => serde_json::Value::String(x.clone()),
                None => serde_json::Value::Null,
            },
        );
        json.insert(
            "derived_from_events".to_string(),
            serde_json::Value::Array(
                s.derived_from_events
                    .iter()
                    .map(|x| serde_json::Value::String(x.clone()))
                    .collect(),
            ),
        );
        json.insert(
            "derived_from_assertions".to_string(),
            serde_json::Value::Array(
                s.derived_from_assertions
                    .iter()
                    .map(|x| serde_json::Value::String(x.clone()))
                    .collect(),
            ),
        );
        json.insert(
            "derived_from_drawers".to_string(),
            serde_json::Value::Array(
                s.derived_from_drawers
                    .iter()
                    .map(|x| serde_json::Value::String(x.clone()))
                    .collect(),
            ),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(schema_id, _)| {
                let v = self.query_bytes(schema_id.as_bytes())?;
                Some((schema_id.as_bytes().to_vec(), v))
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
    fn parse_picks_schema_node_id_as_key() {
        let evt = EventTuple {
            kind: "schema_induced".to_string(),
            payload: serde_json::to_vec(&json!({
                "schema_node_id": "sch_loyalty",
                "schema_kind": "trait",
                "name": "asymmetric_loyalty",
                "stability_score": 0.85,
            }))
            .unwrap(),
            offset: 1,
        };
        let (key, p) = parse_event(&evt).unwrap();
        assert_eq!(key, "sch_loyalty");
        assert_eq!(p.name, "asymmetric_loyalty");
        assert!((p.stability_score - 0.85).abs() < 1e-9);
    }

    #[test]
    fn parse_skips_unrelated_kinds() {
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
                SchemaInducedPayload {
                    schema_node_id: "sch_x".to_string(),
                    name: "v1".to_string(),
                    stability_score: 0.5,
                    ..Default::default()
                },
            ),
            (
                10,
                SchemaInducedPayload {
                    schema_node_id: "sch_x".to_string(),
                    name: "v3".to_string(),
                    stability_score: 0.9,
                    ..Default::default()
                },
            ),
            (
                5,
                SchemaInducedPayload {
                    schema_node_id: "sch_x".to_string(),
                    name: "v2".to_string(),
                    stability_score: 0.7,
                    ..Default::default()
                },
            ),
        ];
        let s = pick_latest(events).unwrap();
        assert_eq!(s.name, "v3");
        assert_eq!(s.induced_at_offset, 10);
        assert!((s.stability_score() - 0.9).abs() < 1e-9);
    }

    #[test]
    fn snapshot_query_legacy_shape() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "sch_x".to_string(),
            SchemaState {
                schema_node_id: "sch_x".to_string(),
                schema_kind: "trait".to_string(),
                name: "loyalty".to_string(),
                description: "asymmetric loyalty".to_string(),
                stability_score_bits: 0.8f64.to_bits(),
                coverage_score_bits: 0.6f64.to_bits(),
                miner_pass_version: "class3_v2".to_string(),
                induced_at_offset: 42,
                supersedes_schema_id: Some("sch_old".to_string()),
                derived_from_events: vec!["evt1".to_string()],
                derived_from_assertions: vec![],
                derived_from_drawers: vec!["drw1".to_string(), "drw2".to_string()],
            },
        );

        let q = SchemasSnapshotQuery { snapshot: snap, frontier };
        let bytes = q.query_bytes(b"sch_x").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["schema_node_id"], "sch_x");
        assert_eq!(v["schema_kind"], "trait");
        assert_eq!(v["name"], "loyalty");
        assert!((v["stability_score"].as_f64().unwrap() - 0.8).abs() < 1e-9);
        assert!((v["coverage_score"].as_f64().unwrap() - 0.6).abs() < 1e-9);
        assert_eq!(v["supersedes_schema_id"], "sch_old");
        assert_eq!(v["derived_from_drawers"].as_array().unwrap().len(), 2);
    }
}
