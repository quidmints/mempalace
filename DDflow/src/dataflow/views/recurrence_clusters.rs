//! `recurrence_clusters` as a Differential Dataflow view (sub-slice D).
//!
//! Drawers grouped by substantive similarity. Each cluster has a
//! list of member drawer_ids and a representative drawer_id.
//!
//! Driven entirely by `recurrence_cluster_member` events. Each event
//! carries `(drawer_id, cluster_id, similarity_to_representative)`
//! and adds the drawer to the cluster.
//!
//! # Operator chain
//!
//! Keyed by `cluster_id`. The reduce closure aggregates all
//! membership events for the cluster, deduplicates drawer_ids, and
//! emits a single `ClusterState` with the accumulated membership.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/recurrence_clusters.rs` exists until sub-slice F.

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
pub struct ClusterState {
    pub cluster_id: String,
    pub member_drawer_ids: Vec<String>,
    pub representative_drawer_id: Option<String>,
    pub first_seen_offset: LogOffset,
    pub last_seen_offset: LogOffset,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct MembershipPayload {
    drawer_id: String,
    cluster_id: String,
    #[serde(default = "default_sim")]
    similarity_to_representative: f64,
}

fn default_sim() -> f64 {
    1.0
}

fn parse_event(evt: &EventTuple) -> Option<(String, (LogOffset, String))> {
    if evt.kind != "recurrence_cluster_member" {
        return None;
    }
    let p: MembershipPayload = serde_json::from_slice(&evt.payload).ok()?;
    Some((p.cluster_id, (evt.offset, p.drawer_id)))
}

fn fold_memberships(events: Vec<(LogOffset, String)>) -> Option<ClusterState> {
    if events.is_empty() {
        return None;
    }
    // Sort by offset for deterministic ordering
    let mut events = events;
    events.sort_by(|a, b| a.0.cmp(&b.0));

    let first_offset = events.first().map(|(o, _)| *o).unwrap_or(0);
    let last_offset = events.last().map(|(o, _)| *o).unwrap_or(0);
    let representative = events.first().map(|(_, d)| d.clone());

    // Deduplicated, ordered membership
    let mut members: Vec<String> = Vec::new();
    for (_, drawer_id) in events {
        if !members.contains(&drawer_id) {
            members.push(drawer_id);
        }
    }

    Some(ClusterState {
        cluster_id: String::new(), // filled in by reduce-key context
        member_drawer_ids: members,
        representative_drawer_id: representative,
        first_seen_offset: first_offset,
        last_seen_offset: last_offset,
    })
}

pub struct RecurrenceClustersView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, ClusterState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl RecurrenceClustersView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for RecurrenceClustersView {
    fn name(&self) -> &'static str {
        "recurrence_clusters"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["recurrence_cluster_member"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        let keyed = input.flat_map(|evt: EventTuple| parse_event(&evt));

        let state_collection = keyed.reduce(|cluster_id, input_events, output| {
            let events: Vec<(LogOffset, String)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref drawer_id), _)| (offset, drawer_id.clone()))
                .collect();

            if let Some(mut state) = fold_memberships(events) {
                state.cluster_id = cluster_id.clone();
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((cluster_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(cluster_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(cluster_id) == Some(state) {
                        snap.remove(cluster_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(RecurrenceClustersSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct RecurrenceClustersSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, ClusterState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for RecurrenceClustersSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("cluster_id".to_string(), serde_json::Value::String(s.cluster_id.clone()));
        json.insert(
            "member_drawer_ids".to_string(),
            serde_json::Value::Array(
                s.member_drawer_ids
                    .iter()
                    .map(|d| serde_json::Value::String(d.clone()))
                    .collect(),
            ),
        );
        json.insert(
            "representative_drawer_id".to_string(),
            match &s.representative_drawer_id {
                Some(d) => serde_json::Value::String(d.clone()),
                None => serde_json::Value::Null,
            },
        );
        json.insert(
            "first_seen_offset".to_string(),
            serde_json::Value::from(s.first_seen_offset),
        );
        json.insert(
            "last_seen_offset".to_string(),
            serde_json::Value::from(s.last_seen_offset),
        );
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(cluster_id, _)| {
                let v = self.query_bytes(cluster_id.as_bytes())?;
                Some((cluster_id.as_bytes().to_vec(), v))
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
    fn parse_event_extracts_cluster_and_drawer() {
        let evt = EventTuple {
            kind: "recurrence_cluster_member".to_string(),
            payload: serde_json::to_vec(&json!({
                "drawer_id": "d1",
                "cluster_id": "c1",
            }))
            .unwrap(),
            offset: 5,
        };
        let (cluster_id, (offset, drawer_id)) = parse_event(&evt).unwrap();
        assert_eq!(cluster_id, "c1");
        assert_eq!(drawer_id, "d1");
        assert_eq!(offset, 5);
    }

    #[test]
    fn fold_first_drawer_is_representative() {
        let events = vec![(1, "d1".to_string()), (2, "d2".to_string())];
        let s = fold_memberships(events).unwrap();
        assert_eq!(s.representative_drawer_id.as_deref(), Some("d1"));
    }

    #[test]
    fn fold_dedupe_preserves_order() {
        let events = vec![
            (1, "d1".to_string()),
            (2, "d2".to_string()),
            (3, "d1".to_string()), // dup
            (4, "d3".to_string()),
        ];
        let s = fold_memberships(events).unwrap();
        assert_eq!(s.member_drawer_ids, vec!["d1", "d2", "d3"]);
        assert_eq!(s.first_seen_offset, 1);
        assert_eq!(s.last_seen_offset, 4);
    }

    #[test]
    fn fold_empty_returns_none() {
        let events: Vec<(LogOffset, String)> = vec![];
        assert!(fold_memberships(events).is_none());
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
}
