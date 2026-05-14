//! `matched_against` as a Differential Dataflow view (sub-slice E).
//!
//! Per-requester ledger of prior matches — used by the rate-limiter
//! and audit. Each ledger entry is one (request → finding) cycle.
//!
//! # Operator chain
//!
//! Same join-by-match_id pattern as `match_cache`: `match_request_received`
//! and `finding_emitted` are folded per match_id by reduce. Unlike
//! `match_cache`, this view emits the entry as soon as the request
//! arrives — the finding just stamps the `completed_at_ms` field.
//! That matches the legacy view, which inserted on request and
//! mutated on finding.
//!
//! # Per-requester grouping
//!
//! The legacy view stored per-requester `VecDeque<MatchLedgerEntry>`.
//! In DD we key by `match_id` and let consumers filter by
//! `requester_pubkey` at query time. The `count_in_window` helper
//! that powers rate-limiting is exposed via the snapshot scan.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/matched_against.rs` exists until sub-slice F.
//!
//! Spec ref: R3 §3.3.

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
// MatchLedgerEntry
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct MatchLedgerEntry {
    pub match_id: String,
    pub requester_pubkey: String,
    pub target_palace_id: String,
    pub requested_at_ms: u64,
    pub completed_at_ms: Option<u64>,
    pub asserted_at_offset: LogOffset,
}

// =============================================================================
// Payload structs and parsing
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct RequestPayload {
    match_id: String,
    requester_pubkey: String,
    target_palace_id: String,
    #[serde(default)]
    _timestamp_ms: u64,
}

#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct FindingPayload {
    match_id: String,
    #[serde(default)]
    _timestamp_ms: u64,
}

#[derive(Clone, Debug)]
enum ParsedLedgerEvent {
    Request {
        offset: LogOffset,
        timestamp_ms: u64,
        payload: RequestPayload,
    },
    Finding {
        timestamp_ms: u64,
    },
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedLedgerEvent)> {
    match evt.kind.as_str() {
        "match_request_received" => {
            let p: RequestPayload = serde_json::from_slice(&evt.payload).ok()?;
            let ts = if p._timestamp_ms > 0 {
                p._timestamp_ms
            } else {
                evt.offset
            };
            let id = p.match_id.clone();
            Some((
                id,
                ParsedLedgerEvent::Request {
                    offset: evt.offset,
                    timestamp_ms: ts,
                    payload: p,
                },
            ))
        }
        "finding_emitted" => {
            let p: FindingPayload = serde_json::from_slice(&evt.payload).ok()?;
            let ts = if p._timestamp_ms > 0 {
                p._timestamp_ms
            } else {
                evt.offset
            };
            Some((p.match_id, ParsedLedgerEvent::Finding { timestamp_ms: ts }))
        }
        _ => None,
    }
}

/// Fold all events for a match_id. Emits Some(entry) once a Request
/// is seen. The finding's timestamp_ms (if any) populates
/// `completed_at_ms`; otherwise it remains None.
fn fold_events(events: Vec<(LogOffset, ParsedLedgerEvent)>) -> Option<MatchLedgerEntry> {
    let mut sorted = events;
    sorted.sort_by_key(|(offset, _)| *offset);

    let mut request: Option<(LogOffset, RequestPayload, u64)> = None;
    let mut completion_ts: Option<u64> = None;

    for (_offset, evt) in sorted {
        match evt {
            ParsedLedgerEvent::Request {
                offset,
                timestamp_ms,
                payload,
            } => {
                request = Some((offset, payload, timestamp_ms));
            }
            ParsedLedgerEvent::Finding { timestamp_ms } => {
                completion_ts = Some(timestamp_ms);
            }
        }
    }

    let (offset, p, ts) = request?;
    Some(MatchLedgerEntry {
        match_id: p.match_id,
        requester_pubkey: p.requester_pubkey,
        target_palace_id: p.target_palace_id,
        requested_at_ms: ts,
        completed_at_ms: completion_ts,
        asserted_at_offset: offset,
    })
}

// =============================================================================
// View
// =============================================================================

pub struct MatchedAgainstView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, MatchLedgerEntry>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl MatchedAgainstView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for MatchedAgainstView {
    fn name(&self) -> &'static str {
        "matched_against"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["match_request_received", "finding_emitted"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);

        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(match_id, parsed)| (match_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(|_match_id, input_events, output| {
            let events: Vec<(LogOffset, ParsedLedgerEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();

            if let Some(state) = fold_events(events) {
                output.push((state, 1));
            }
        });

        state_collection.inspect_batch(move |time, updates| {
            let mut snap = snapshot_inspect.write();
            for ((match_id, state), diff) in updates {
                if *diff > 0 {
                    snap.insert(match_id.clone(), state.clone());
                } else if *diff < 0 {
                    if snap.get(match_id) == Some(state) {
                        snap.remove(match_id);
                    }
                }
            }
            let mut f = frontier_inspect.lock();
            if *time > *f {
                *f = *time;
            }
        });

        let _arranged = state_collection.arrange_by_key();

        Box::new(MatchedAgainstQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery
// =============================================================================

struct MatchedAgainstQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, MatchLedgerEntry>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for MatchedAgainstQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert(
            "match_id".to_string(),
            serde_json::Value::String(s.match_id.clone()),
        );
        json.insert(
            "requester_pubkey".to_string(),
            serde_json::Value::String(s.requester_pubkey.clone()),
        );
        json.insert(
            "target_palace_id".to_string(),
            serde_json::Value::String(s.target_palace_id.clone()),
        );
        json.insert(
            "requested_at_ms".to_string(),
            serde_json::Value::from(s.requested_at_ms),
        );
        json.insert(
            "completed_at_ms".to_string(),
            match s.completed_at_ms {
                Some(t) => serde_json::Value::from(t),
                None => serde_json::Value::Null,
            },
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
            .filter_map(|(match_id, _)| {
                let v = self.query_bytes(match_id.as_bytes())?;
                Some((match_id.as_bytes().to_vec(), v))
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
    fn fold_request_only_produces_open_entry() {
        let events = vec![(
            1,
            ParsedLedgerEvent::Request {
                offset: 1,
                timestamp_ms: 1000,
                payload: RequestPayload {
                    match_id: "m1".to_string(),
                    requester_pubkey: "pk_a".to_string(),
                    target_palace_id: "pk_b".to_string(),
                    ..Default::default()
                },
            },
        )];
        let s = fold_events(events).unwrap();
        assert_eq!(s.match_id, "m1");
        assert_eq!(s.requester_pubkey, "pk_a");
        assert_eq!(s.completed_at_ms, None);
        assert_eq!(s.requested_at_ms, 1000);
    }

    #[test]
    fn fold_request_then_finding_stamps_completion() {
        let events = vec![
            (
                1,
                ParsedLedgerEvent::Request {
                    offset: 1,
                    timestamp_ms: 1000,
                    payload: RequestPayload {
                        match_id: "m1".to_string(),
                        requester_pubkey: "pk_a".to_string(),
                        target_palace_id: "pk_b".to_string(),
                        ..Default::default()
                    },
                },
            ),
            (5, ParsedLedgerEvent::Finding { timestamp_ms: 5000 }),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.completed_at_ms, Some(5000));
        // Asserted-at-offset is the request's offset (when the entry first opened)
        assert_eq!(s.asserted_at_offset, 1);
    }

    #[test]
    fn fold_finding_only_returns_none() {
        // A finding without a prior request shouldn't appear in the
        // ledger — this matches the legacy view's behavior (the
        // legacy view's `for q in guard.values_mut() ... find` would
        // simply find nothing).
        let events = vec![(1, ParsedLedgerEvent::Finding { timestamp_ms: 1000 })];
        assert!(fold_events(events).is_none());
    }

    #[test]
    fn parse_request_keys_by_match_id() {
        let evt = EventTuple {
            kind: "match_request_received".to_string(),
            payload: serde_json::to_vec(&json!({
                "match_id": "m_x",
                "requester_pubkey": "pk_a",
                "target_palace_id": "pk_b",
            }))
            .unwrap(),
            offset: 7,
        };
        let (key, parsed) = parse_event(&evt).unwrap();
        assert_eq!(key, "m_x");
        match parsed {
            ParsedLedgerEvent::Request { offset, .. } => assert_eq!(offset, 7),
            _ => panic!("expected Request"),
        }
    }

    #[test]
    fn snapshot_query_legacy_shape() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "m1".to_string(),
            MatchLedgerEntry {
                match_id: "m1".to_string(),
                requester_pubkey: "pk_a".to_string(),
                target_palace_id: "pk_b".to_string(),
                requested_at_ms: 1000,
                completed_at_ms: Some(2000),
                asserted_at_offset: 5,
            },
        );

        let q = MatchedAgainstQuery { snapshot: snap, frontier };
        let bytes = q.query_bytes(b"m1").unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["match_id"], "m1");
        assert_eq!(v["requester_pubkey"], "pk_a");
        assert_eq!(v["completed_at_ms"], 2000);
    }

    #[test]
    fn snapshot_query_completed_null_when_open() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        snap.write().insert(
            "m1".to_string(),
            MatchLedgerEntry {
                match_id: "m1".to_string(),
                requester_pubkey: "pk_a".to_string(),
                target_palace_id: "pk_b".to_string(),
                requested_at_ms: 1000,
                completed_at_ms: None,
                asserted_at_offset: 5,
            },
        );

        let q = MatchedAgainstQuery { snapshot: snap, frontier };
        let v: serde_json::Value =
            serde_json::from_slice(&q.query_bytes(b"m1").unwrap()).unwrap();
        assert_eq!(v["completed_at_ms"], serde_json::Value::Null);
    }
}
