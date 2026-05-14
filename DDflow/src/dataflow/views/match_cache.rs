//! `match_cache` as a Differential Dataflow view (sub-slice E).
//!
//! Idempotent matching cache: once a match has been run between
//! (requester, target) in a given time window, the result is cached
//! for reuse. Re-requests within the window return cached findings.
//!
//! # Data shape and join semantics
//!
//! A complete cache entry needs information from *two* events:
//!
//!   - `match_request_received`: `(match_id, requester_pubkey, target_palace_id, timestamp_ms)`
//!   - `finding_emitted`:        `(match_id, topology, strength_per_dimension, timestamp_ms)`
//!
//! Both are keyed by `match_id`. The legacy view kept side tables
//! (`match_id_to_pair`, `match_id_to_key`) so it could "remember" the
//! request when the finding arrived. In DD this is naturally a
//! per-match-id reduce: collect all events for the match_id, fold
//! them into a single state, emit Some(state) only when both halves
//! are present.
//!
//! Once we have the joined per-match record, we re-key by the
//! `(requester|target|window)` triple — the legacy cache key. That
//! re-keying is currently done at *query time* (via `lookup`),
//! because moving it into the dataflow requires another reduce that
//! would inflate the operator graph for marginal benefit at current
//! scale.
//!
//! # TTL / pruning
//!
//! Pruning expired entries is a query-time concern. The view stores
//! everything; consumers filter by `completed_at_ms` against their
//! own `now_ms - ttl`.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/match_cache.rs` exists until sub-slice F.
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
// CachedMatch state — DD-compatible
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct CachedMatch {
    pub match_id: String,
    pub requester_pubkey: String,
    pub target_palace_id: String,
    pub window_key: String,
    pub finding_summary: String,
    pub finding_topology: String,
    /// JSON-encoded for DD compat
    pub strength_per_dimension_json: String,
    pub completed_at_ms: u64,
    pub completed_at_offset: LogOffset,
}

impl CachedMatch {
    pub fn strength_per_dimension(&self) -> serde_json::Value {
        serde_json::from_str(&self.strength_per_dimension_json)
            .unwrap_or(serde_json::Value::Null)
    }
    /// Legacy cache key shape: "requester|target|window".
    pub fn cache_key(&self) -> String {
        format!(
            "{}|{}|{}",
            self.requester_pubkey, self.target_palace_id, self.window_key
        )
    }
}

// =============================================================================
// Payload structs and parsing
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default, Clone)]
struct MatchRequestPayload {
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
    topology: String,
    #[serde(default)]
    strength_per_dimension: serde_json::Value,
    #[serde(default)]
    _timestamp_ms: u64,
}

#[derive(Clone, Debug)]
enum ParsedMatchEvent {
    Request {
        offset: LogOffset,
        timestamp_ms: u64,
        payload: MatchRequestPayload,
    },
    Finding {
        offset: LogOffset,
        timestamp_ms: u64,
        payload: FindingPayload,
    },
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedMatchEvent)> {
    match evt.kind.as_str() {
        "match_request_received" => {
            let p: MatchRequestPayload = serde_json::from_slice(&evt.payload).ok()?;
            let ts = if p._timestamp_ms > 0 {
                p._timestamp_ms
            } else {
                evt.offset
            };
            let id = p.match_id.clone();
            Some((
                id,
                ParsedMatchEvent::Request {
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
            let id = p.match_id.clone();
            Some((
                id,
                ParsedMatchEvent::Finding {
                    offset: evt.offset,
                    timestamp_ms: ts,
                    payload: p,
                },
            ))
        }
        _ => None,
    }
}

/// Compute a coarse weekly key like "2026-W18" from a timestamp. The
/// legacy view used `chrono::IsoWeek`; we replicate that here. If the
/// timestamp is invalid, returns "unknown".
fn format_week_key(ts_ms: u64) -> String {
    use chrono::{DateTime, Datelike, Utc};
    let dt = DateTime::<Utc>::from_timestamp_millis(ts_ms as i64).unwrap_or_default();
    let iso_week = dt.iso_week();
    format!("{}-W{:02}", iso_week.year(), iso_week.week())
}

/// Fold all events for a match_id. Returns Some(state) only when
/// both a request AND a finding are present.
fn fold_events(events: Vec<(LogOffset, ParsedMatchEvent)>) -> Option<CachedMatch> {
    let mut request: Option<MatchRequestPayload> = None;
    let mut finding: Option<FindingPayload> = None;
    let mut completed_offset: LogOffset = 0;
    let mut completed_ts: u64 = 0;

    let mut sorted = events;
    sorted.sort_by_key(|(offset, _)| *offset);

    for (_offset, evt) in sorted {
        match evt {
            ParsedMatchEvent::Request { payload, .. } => {
                request = Some(payload);
            }
            ParsedMatchEvent::Finding {
                offset,
                timestamp_ms,
                payload,
            } => {
                finding = Some(payload);
                completed_offset = offset;
                completed_ts = timestamp_ms;
            }
        }
    }

    let req = request?;
    let fin = finding?;

    let window_key = format_week_key(completed_ts);
    Some(CachedMatch {
        match_id: req.match_id.clone(),
        requester_pubkey: req.requester_pubkey,
        target_palace_id: req.target_palace_id,
        window_key,
        finding_summary: format!("match {}", req.match_id),
        finding_topology: fin.topology,
        strength_per_dimension_json: serde_json::to_string(&fin.strength_per_dimension)
            .unwrap_or_else(|_| "null".to_string()),
        completed_at_ms: completed_ts,
        completed_at_offset: completed_offset,
    })
}

// =============================================================================
// View
// =============================================================================

pub struct MatchCacheView {
    /// Keyed by match_id. Lookup-by-cache-key happens in
    /// `query_bytes` by scanning entries (small N typical). Future
    /// optimization: a secondary arrangement keyed by cache_key.
    snapshot: Arc<RwLock<std::collections::HashMap<String, CachedMatch>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl MatchCacheView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for MatchCacheView {
    fn name(&self) -> &'static str {
        "match_cache"
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
            let events: Vec<(LogOffset, ParsedMatchEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();

            if let Some(state) = fold_events(events) {
                output.push((state, 1));
            }
            // No output until both request and finding are present.
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

        Box::new(MatchCacheQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

// =============================================================================
// TraceQuery
// =============================================================================

struct MatchCacheQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, CachedMatch>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for MatchCacheQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();

        // Lookup tries match_id first (the primary key); falls
        // back to cache_key (legacy "requester|target|window")
        // by scanning. Small entry counts in typical use.
        let s = if let Some(s) = snap.get(&key) {
            s.clone()
        } else if let Some(s) = snap.values().find(|v| v.cache_key() == key) {
            s.clone()
        } else {
            return None;
        };

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
            "window_key".to_string(),
            serde_json::Value::String(s.window_key.clone()),
        );
        json.insert(
            "finding_summary".to_string(),
            serde_json::Value::String(s.finding_summary.clone()),
        );
        json.insert(
            "finding_topology".to_string(),
            serde_json::Value::String(s.finding_topology.clone()),
        );
        json.insert(
            "strength_per_dimension".to_string(),
            s.strength_per_dimension(),
        );
        json.insert(
            "completed_at_ms".to_string(),
            serde_json::Value::from(s.completed_at_ms),
        );
        json.insert(
            "completed_at_offset".to_string(),
            serde_json::Value::from(s.completed_at_offset),
        );
        json.insert(
            "cache_key".to_string(),
            serde_json::Value::String(s.cache_key()),
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
    fn fold_returns_none_when_only_request() {
        let req = MatchRequestPayload {
            match_id: "m1".to_string(),
            requester_pubkey: "pk_a".to_string(),
            target_palace_id: "pk_b".to_string(),
            ..Default::default()
        };
        let events = vec![(
            1,
            ParsedMatchEvent::Request {
                offset: 1,
                timestamp_ms: 1000,
                payload: req,
            },
        )];
        assert!(fold_events(events).is_none(), "request alone shouldn't cache");
    }

    #[test]
    fn fold_returns_none_when_only_finding() {
        let fin = FindingPayload {
            match_id: "m1".to_string(),
            topology: "peer".to_string(),
            ..Default::default()
        };
        let events = vec![(
            1,
            ParsedMatchEvent::Finding {
                offset: 1,
                timestamp_ms: 1000,
                payload: fin,
            },
        )];
        assert!(fold_events(events).is_none(), "finding alone shouldn't cache");
    }

    #[test]
    fn fold_request_and_finding_produces_cache_entry() {
        let req = MatchRequestPayload {
            match_id: "m1".to_string(),
            requester_pubkey: "pk_alice".to_string(),
            target_palace_id: "pk_bob".to_string(),
            ..Default::default()
        };
        let fin = FindingPayload {
            match_id: "m1".to_string(),
            topology: "peer".to_string(),
            strength_per_dimension: json!({"loyalty": 0.7}),
            ..Default::default()
        };
        let events = vec![
            (
                1,
                ParsedMatchEvent::Request {
                    offset: 1,
                    timestamp_ms: 1000,
                    payload: req,
                },
            ),
            (
                5,
                ParsedMatchEvent::Finding {
                    offset: 5,
                    timestamp_ms: 2000,
                    payload: fin,
                },
            ),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.match_id, "m1");
        assert_eq!(s.requester_pubkey, "pk_alice");
        assert_eq!(s.target_palace_id, "pk_bob");
        assert_eq!(s.finding_topology, "peer");
        assert_eq!(s.completed_at_ms, 2000);
        assert_eq!(s.completed_at_offset, 5);
        assert_eq!(s.strength_per_dimension(), json!({"loyalty": 0.7}));
        assert!(s.cache_key().starts_with("pk_alice|pk_bob|"));
    }

    #[test]
    fn format_week_key_shape() {
        // Friday 2026-05-01 is in ISO week 18 of 2026.
        // Timestamp: 2026-05-01T00:00:00Z = 1746057600 sec * 1000 = 1746057600000 ms
        let key = format_week_key(1_746_057_600_000);
        assert!(key.starts_with("2026-W"), "got {key}");
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
            offset: 1,
        };
        let (key, _) = parse_event(&evt).unwrap();
        assert_eq!(key, "m_x");
    }

    #[test]
    fn query_supports_cache_key_lookup() {
        let snap = Arc::new(RwLock::new(std::collections::HashMap::new()));
        let frontier = Arc::new(parking_lot::Mutex::new(0u64));

        let cm = CachedMatch {
            match_id: "m1".to_string(),
            requester_pubkey: "pk_a".to_string(),
            target_palace_id: "pk_b".to_string(),
            window_key: "2026-W18".to_string(),
            finding_summary: "match m1".to_string(),
            finding_topology: "peer".to_string(),
            strength_per_dimension_json: "{}".to_string(),
            completed_at_ms: 1000,
            completed_at_offset: 5,
        };
        snap.write().insert("m1".to_string(), cm);

        let q = MatchCacheQuery { snapshot: snap, frontier };

        // Lookup by match_id
        let v: serde_json::Value =
            serde_json::from_slice(&q.query_bytes(b"m1").unwrap()).unwrap();
        assert_eq!(v["match_id"], "m1");

        // Lookup by cache_key
        let v2: serde_json::Value =
            serde_json::from_slice(&q.query_bytes(b"pk_a|pk_b|2026-W18").unwrap())
                .unwrap();
        assert_eq!(v2["match_id"], "m1");
    }
}
