//! `pending_review` as a Differential Dataflow view (sub-slice E).
//!
//! Aggregates items awaiting user review across the system, grouped
//! by category. Backs the daily/weekly review-mode UI and the
//! `mempalace_pending_review` MCP tool.
//!
//! # Multi-source aggregator
//!
//! Subscribes to many event kinds. Each contributes a different
//! category:
//!
//! | event kind                | category                |
//! |---------------------------|-------------------------|
//! | `schema_induced`          | schema_proposal         |
//! | `contradiction_asserted`  | contradiction_open      |
//! | `drawer_hash_collision`   | drawer_collision        |
//! | `canonical_promoted`      | canonical_promotion     |
//! | `canonical_rejected`      | canonical_rejection     |
//! | `contradiction_resolved`  | (removes contradiction_open by edge_id) |
//!
//! # Keying
//!
//! Items are keyed by the deterministic id `pri_{category}_{ref}`,
//! where `ref` is the `edge_id` / `schema_node_id` / `node_id` /
//! `incoming_drawer_id` extracted from the event payload. This
//! makes resolution-removal natural: a `contradiction_resolved`
//! event keys to the same id as the original `contradiction_asserted`,
//! and the reduce closure emits None for that key (DD retracts).
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/pending_review.rs` exists until sub-slice F.
//!
//! Spec ref: R3 §4.4.

use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

// TODO(rust-build): same DD operator imports.
use differential_dataflow::operators::Reduce;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::Collection;
use timely::dataflow::Scope;

use crate::dataflow::{
    BoxedTrace, DataflowTimestamp, EventTuple, TraceQuery, ViewSpec,
};
use crate::LogOffset;

// =============================================================================
// PendingItem state
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct PendingItem {
    pub item_id: String,
    pub category: String,
    pub summary: String,
    pub reference_node_id: Option<String>,
    pub asserted_at_offset: LogOffset,
    pub priority: u8,
}

// =============================================================================
// Event parsing
// =============================================================================

/// References extractable from a payload — first non-None wins
/// (matches legacy view's precedence: edge_id > schema_node_id >
/// node_id > incoming_drawer_id).
#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct ReferenceFields {
    #[serde(default)]
    edge_id: Option<String>,
    #[serde(default)]
    schema_node_id: Option<String>,
    #[serde(default)]
    node_id: Option<String>,
    #[serde(default)]
    incoming_drawer_id: Option<String>,
}

impl ReferenceFields {
    fn pick_reference(&self) -> Option<String> {
        self.edge_id
            .clone()
            .or(self.schema_node_id.clone())
            .or(self.node_id.clone())
            .or(self.incoming_drawer_id.clone())
    }
}

/// What this event contributes to the pending list.
#[derive(Clone, Debug)]
enum ParsedPendingEvent {
    /// Add (or refresh) an item with this category and summary.
    Add { category: String, summary: String },
    /// Remove an item under this category (used for
    /// contradiction_resolved → contradiction_open).
    Remove { category: String },
}

/// Parse an event into `(item_id, ParsedPendingEvent)`. Returns
/// None for events that don't apply.
fn parse_event(evt: &EventTuple) -> Option<(String, ParsedPendingEvent)> {
    let (category, summary, removes_under_category) = match evt.kind.as_str() {
        "schema_induced" => ("schema_proposal", "new schema induced", None),
        "contradiction_asserted" => ("contradiction_open", "contradiction detected", None),
        "drawer_hash_collision" => ("drawer_collision", "duplicate content captured", None),
        "canonical_promoted" => ("canonical_promotion", "new canonical promoted", None),
        "canonical_rejected" => ("canonical_rejection", "canonical proposal rejected", None),
        "contradiction_resolved" => {
            // Resolution: emit a Remove keyed to "contradiction_open".
            ("contradiction_open", "", Some("contradiction_open"))
        }
        _ => return None,
    };

    let refs: ReferenceFields = serde_json::from_slice(&evt.payload).unwrap_or_default();
    let reference = refs.pick_reference()?;
    let item_id = format!("pri_{}_{}", category, reference);

    let parsed = if removes_under_category.is_some() {
        ParsedPendingEvent::Remove {
            category: category.to_string(),
        }
    } else {
        ParsedPendingEvent::Add {
            category: category.to_string(),
            summary: summary.to_string(),
        }
    };
    Some((item_id, parsed))
}

/// Fold all events for an item_id. If the latest event is an Add
/// and there's no subsequent Remove, emits Some(item). Otherwise
/// None (item retracted).
fn fold_events(
    item_id: &str,
    events: Vec<(LogOffset, ParsedPendingEvent)>,
    reference: Option<String>,
) -> Option<PendingItem> {
    let mut sorted = events;
    sorted.sort_by_key(|(offset, _)| *offset);

    let mut current: Option<PendingItem> = None;
    for (offset, evt) in sorted {
        match evt {
            ParsedPendingEvent::Add { category, summary } => {
                current = Some(PendingItem {
                    item_id: item_id.to_string(),
                    category,
                    summary,
                    reference_node_id: reference.clone(),
                    asserted_at_offset: offset,
                    priority: 1,
                });
            }
            ParsedPendingEvent::Remove { .. } => {
                current = None;
            }
        }
    }
    current
}

// =============================================================================
// View
// =============================================================================

pub struct PendingReviewView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, PendingItem>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl PendingReviewView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for PendingReviewView {
    fn name(&self) -> &'static str {
        "pending_review"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &[
            "schema_induced",
            "contradiction_asserted",
            "contradiction_resolved",
            "drawer_hash_collision",
            "canonical_promoted",
            "canonical_rejected",
        ]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        // The reduce closure needs the reference id, which is part
        // of the parsed event's "key context." We carry it through
        // by extracting it again inside the reduce — but it's
        // actually encoded into the key already (item_id =
        // `pri_{category}_{reference}`), so we just split the key.
        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(item_id, parsed)| (item_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(|item_id, input_events, output| {
            let events: Vec<(LogOffset, ParsedPendingEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();

            // Recover the reference id from the key. Item ids are
            // `pri_{category}_{reference}`. The category is one of
            // a known set, so we split on the LAST underscore that
            // separates category from reference.
            let reference = extract_reference_from_item_id(item_id);

            if let Some(state) = fold_events(item_id, events, reference) {
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((item_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(item_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(item_id) == Some(state) {
                        snap.remove(item_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(PendingReviewQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

/// Extract the reference id (the part after `pri_{category}_`).
/// Categories are a fixed set, so we strip a known prefix.
fn extract_reference_from_item_id(item_id: &str) -> Option<String> {
    for cat in &[
        "schema_proposal",
        "contradiction_open",
        "drawer_collision",
        "canonical_promotion",
        "canonical_rejection",
    ] {
        let prefix = format!("pri_{}_", cat);
        if let Some(rest) = item_id.strip_prefix(&prefix) {
            return Some(rest.to_string());
        }
    }
    None
}

// =============================================================================
// TraceQuery
// =============================================================================

struct PendingReviewQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, PendingItem>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for PendingReviewQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert(
            "item_id".to_string(),
            serde_json::Value::String(s.item_id.clone()),
        );
        json.insert(
            "category".to_string(),
            serde_json::Value::String(s.category.clone()),
        );
        json.insert(
            "summary".to_string(),
            serde_json::Value::String(s.summary.clone()),
        );
        json.insert(
            "reference_node_id".to_string(),
            match &s.reference_node_id {
                Some(x) => serde_json::Value::String(x.clone()),
                None => serde_json::Value::Null,
            },
        );
        json.insert(
            "asserted_at_offset".to_string(),
            serde_json::Value::from(s.asserted_at_offset),
        );
        json.insert("priority".to_string(), serde_json::Value::from(s.priority));
        serde_json::to_vec(&serde_json::Value::Object(json)).ok()
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        let snap = self.snapshot.read();
        snap.iter()
            .filter_map(|(item_id, _)| {
                let v = self.query_bytes(item_id.as_bytes())?;
                Some((item_id.as_bytes().to_vec(), v))
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
    fn parse_schema_induced_to_proposal_category() {
        let evt = EventTuple {
            kind: "schema_induced".to_string(),
            payload: serde_json::to_vec(&json!({
                "schema_node_id": "sch_x",
            }))
            .unwrap(),
            offset: 1,
        };
        let (key, parsed) = parse_event(&evt).unwrap();
        assert_eq!(key, "pri_schema_proposal_sch_x");
        match parsed {
            ParsedPendingEvent::Add { category, .. } => {
                assert_eq!(category, "schema_proposal");
            }
            _ => panic!("expected Add"),
        }
    }

    #[test]
    fn parse_contradiction_resolved_emits_remove_with_same_key_as_assert() {
        let asserted = EventTuple {
            kind: "contradiction_asserted".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "edg_c1",
            }))
            .unwrap(),
            offset: 1,
        };
        let resolved = EventTuple {
            kind: "contradiction_resolved".to_string(),
            payload: serde_json::to_vec(&json!({
                "edge_id": "edg_c1",
            }))
            .unwrap(),
            offset: 5,
        };
        let (k1, _) = parse_event(&asserted).unwrap();
        let (k2, parsed2) = parse_event(&resolved).unwrap();
        assert_eq!(k1, k2, "asserted and resolved must key to the same item");
        assert!(matches!(parsed2, ParsedPendingEvent::Remove { .. }));
    }

    #[test]
    fn parse_unrelated_kind_returns_none() {
        let evt = EventTuple {
            kind: "node_created".to_string(),
            payload: b"{}".to_vec(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn parse_returns_none_when_no_reference_in_payload() {
        // schema_induced without schema_node_id (or any other ref) shouldn't crash
        let evt = EventTuple {
            kind: "schema_induced".to_string(),
            payload: b"{}".to_vec(),
            offset: 1,
        };
        assert!(parse_event(&evt).is_none());
    }

    #[test]
    fn fold_add_only_keeps_open() {
        let item_id = "pri_schema_proposal_sch_x";
        let events = vec![(
            1,
            ParsedPendingEvent::Add {
                category: "schema_proposal".to_string(),
                summary: "new schema induced".to_string(),
            },
        )];
        let s = fold_events(item_id, events, Some("sch_x".to_string())).unwrap();
        assert_eq!(s.category, "schema_proposal");
        assert_eq!(s.reference_node_id, Some("sch_x".to_string()));
    }

    #[test]
    fn fold_add_then_remove_returns_none() {
        let item_id = "pri_contradiction_open_edg_c1";
        let events = vec![
            (
                1,
                ParsedPendingEvent::Add {
                    category: "contradiction_open".to_string(),
                    summary: "x".to_string(),
                },
            ),
            (
                5,
                ParsedPendingEvent::Remove {
                    category: "contradiction_open".to_string(),
                },
            ),
        ];
        assert!(fold_events(item_id, events, Some("edg_c1".to_string())).is_none());
    }

    #[test]
    fn extract_reference_strips_known_category_prefix() {
        assert_eq!(
            extract_reference_from_item_id("pri_schema_proposal_sch_x"),
            Some("sch_x".to_string()),
        );
        assert_eq!(
            extract_reference_from_item_id("pri_contradiction_open_edg_c1"),
            Some("edg_c1".to_string()),
        );
        assert_eq!(extract_reference_from_item_id("not_a_pri_id"), None);
    }
}
