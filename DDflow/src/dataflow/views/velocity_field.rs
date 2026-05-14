//! `velocity_field` as a Differential Dataflow view (sub-slice D).
//!
//! Multi-window rate of change in access frequency. 7d, 30d, 90d
//! rolling windows. Velocity is signed: positive = rising,
//! negative = falling.
//!
//! # Same architectural shift as `heat_field`
//!
//! Legacy `views/velocity_field.rs` had an external `record_access`
//! method that mutated state directly. The DD version replaces that
//! with subscription to the `node_accessed` event kind (same event
//! `heat_field` consumes).
//!
//! # Storage shape
//!
//! Per node, we store the *full list* of access timestamps within
//! the 90-day window. Each event adds a timestamp; the reduce
//! recomputes the windowed counts and velocity ratios on demand.
//!
//! For DD-compat, the timestamp list is stored as `Vec<u64>` —
//! `Vec` is `Hash + Ord` because `u64` is. The list is sorted
//! ascending and trimmed to the 90-day window.
//!
//! # Window math
//!
//! - `count_7d`: timestamps in `[now - 7d, now]`
//! - `count_7d_prior`: timestamps in `[now - 14d, now - 7d)`
//! - `velocity_7d`: `(count_7d - count_7d_prior) / max(1, count_7d_prior)`
//!   (zero-prior special case: just `count_7d`)
//!
//! The `now` reference is the *latest access timestamp seen*, not
//! wall-clock now. Queries that want true-now velocity should pass
//! the wall-clock to a `velocity_at(now_ms)` helper. (Spec lives in
//! the legacy view; the same helper is exposed here.)
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/velocity_field.rs` exists until sub-slice F.

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

const MS_PER_DAY: u64 = 86_400_000;

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct VelocityState {
    pub node_id: String,
    /// All access timestamps within 90 days of the most-recent
    /// access. Sorted ascending. Bounded length: at typical access
    /// rates this is small per node.
    pub access_times_ms: Vec<u64>,
    /// Most-recent access timestamp; provides the `now` reference
    /// for window math when queries don't supply one.
    pub last_access_ms: u64,
}

impl VelocityState {
    /// Compute window counts and velocities at `now_ms`. If no
    /// `now_ms` is provided, uses `last_access_ms`.
    pub fn velocity_at(&self, now_ms: u64) -> VelocitySummary {
        let cutoff_90d = now_ms.saturating_sub(90 * MS_PER_DAY);
        let cutoff_60d = now_ms.saturating_sub(60 * MS_PER_DAY);
        let cutoff_30d = now_ms.saturating_sub(30 * MS_PER_DAY);
        let cutoff_14d = now_ms.saturating_sub(14 * MS_PER_DAY);
        let cutoff_7d = now_ms.saturating_sub(7 * MS_PER_DAY);

        let mut count_7d: u32 = 0;
        let mut count_7d_prior: u32 = 0;
        let mut count_30d: u32 = 0;
        let mut count_30d_prior: u32 = 0;
        let mut count_90d: u32 = 0;

        for &t in &self.access_times_ms {
            if t > now_ms {
                continue; // future access (clock skew); skip
            }
            if t >= cutoff_90d {
                count_90d += 1;
            }
            if t >= cutoff_60d && t < cutoff_30d {
                count_30d_prior += 1;
            }
            if t >= cutoff_30d {
                count_30d += 1;
            }
            if t >= cutoff_14d && t < cutoff_7d {
                count_7d_prior += 1;
            }
            if t >= cutoff_7d {
                count_7d += 1;
            }
        }

        let velocity_7d = if count_7d_prior == 0 {
            count_7d as f64
        } else {
            (count_7d as f64 - count_7d_prior as f64) / (count_7d_prior as f64)
        };
        let velocity_30d = if count_30d_prior == 0 {
            count_30d as f64 / 30.0
        } else {
            (count_30d as f64 - count_30d_prior as f64) / (count_30d_prior as f64)
        };
        let velocity_90d = count_90d as f64 / 90.0;

        VelocitySummary {
            access_count_7d: count_7d,
            access_count_30d: count_30d,
            access_count_90d: count_90d,
            velocity_7d,
            velocity_30d,
            velocity_90d,
        }
    }
}

#[derive(Debug, Clone)]
pub struct VelocitySummary {
    pub access_count_7d: u32,
    pub access_count_30d: u32,
    pub access_count_90d: u32,
    pub velocity_7d: f64,
    pub velocity_30d: f64,
    pub velocity_90d: f64,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeAccessedPayload {
    node_id: String,
    #[serde(default)]
    accessed_at_ms: u64,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeCreatedPayload {
    node_id: String,
}

#[derive(Clone, Debug)]
enum ParsedVelocityEvent {
    Created,
    Accessed { at_ms: u64 },
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedVelocityEvent)> {
    match evt.kind.as_str() {
        "node_created" => {
            let p: NodeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            Some((p.node_id, ParsedVelocityEvent::Created))
        }
        "node_accessed" => {
            let p: NodeAccessedPayload = serde_json::from_slice(&evt.payload).ok()?;
            let at_ms = if p.accessed_at_ms == 0 {
                evt.offset
            } else {
                p.accessed_at_ms
            };
            Some((p.node_id, ParsedVelocityEvent::Accessed { at_ms }))
        }
        _ => None,
    }
}

/// Fold all events into a single VelocityState. The access list is
/// sorted ascending; we do NOT trim to 90 days here because the
/// "now" reference for trimming is the latest access timestamp,
/// which may shift as new events arrive. Trimming happens at the
/// next reduce when we know the latest timestamp.
fn fold_events(events: Vec<(LogOffset, ParsedVelocityEvent)>) -> Option<VelocityState> {
    let mut access_times: Vec<u64> = Vec::new();
    let mut seen_create = false;
    for (_offset, evt) in events {
        match evt {
            ParsedVelocityEvent::Created => seen_create = true,
            ParsedVelocityEvent::Accessed { at_ms } => access_times.push(at_ms),
        }
    }
    if !seen_create && access_times.is_empty() {
        return None;
    }
    access_times.sort();
    let last_access_ms = access_times.last().copied().unwrap_or(0);

    // Trim to 90-day window relative to the latest access
    let cutoff = last_access_ms.saturating_sub(90 * MS_PER_DAY);
    let trimmed: Vec<u64> = access_times.into_iter().filter(|&t| t >= cutoff).collect();

    Some(VelocityState {
        node_id: String::new(),
        access_times_ms: trimmed,
        last_access_ms,
    })
}

pub struct VelocityFieldView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, VelocityState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl VelocityFieldView {
    pub fn new() -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
        }
    }
}

impl ViewSpec for VelocityFieldView {
    fn name(&self) -> &'static str {
        "velocity_field"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        &["node_created", "node_accessed"]
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

        let state_collection = keyed.reduce(|node_id, input_events, output| {
            let events: Vec<(LogOffset, ParsedVelocityEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();

            if let Some(mut state) = fold_events(events) {
                state.node_id = node_id.clone();
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

        Box::new(VelocityFieldSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct VelocityFieldSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, VelocityState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for VelocityFieldSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        // Return summary at the most-recent access timestamp (the
        // natural "now" for stored state). Callers wanting
        // wall-clock-now need a separate API path; the snapshot
        // view is good enough for the default query shape.
        let summary = s.velocity_at(s.last_access_ms);

        let mut json = serde_json::Map::new();
        json.insert("node_id".to_string(), serde_json::Value::String(s.node_id.clone()));
        json.insert(
            "last_access_ms".to_string(),
            serde_json::Value::from(s.last_access_ms),
        );
        json.insert(
            "access_count_7d".to_string(),
            serde_json::Value::from(summary.access_count_7d),
        );
        json.insert(
            "access_count_30d".to_string(),
            serde_json::Value::from(summary.access_count_30d),
        );
        json.insert(
            "access_count_90d".to_string(),
            serde_json::Value::from(summary.access_count_90d),
        );
        json.insert(
            "velocity_7d".to_string(),
            serde_json::Value::from(summary.velocity_7d),
        );
        json.insert(
            "velocity_30d".to_string(),
            serde_json::Value::from(summary.velocity_30d),
        );
        json.insert(
            "velocity_90d".to_string(),
            serde_json::Value::from(summary.velocity_90d),
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

    fn ms_days(d: u64) -> u64 {
        d * MS_PER_DAY
    }

    #[test]
    fn fold_only_create_returns_empty_state() {
        let events = vec![(1, ParsedVelocityEvent::Created)];
        let s = fold_events(events).unwrap();
        assert_eq!(s.access_times_ms, Vec::<u64>::new());
        assert_eq!(s.last_access_ms, 0);
    }

    #[test]
    fn fold_only_accesses_returns_state() {
        // No `Created` event but accesses still build state.
        let events = vec![
            (1, ParsedVelocityEvent::Accessed { at_ms: 1000 }),
            (2, ParsedVelocityEvent::Accessed { at_ms: 5000 }),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.access_times_ms, vec![1000, 5000]);
        assert_eq!(s.last_access_ms, 5000);
    }

    #[test]
    fn fold_trims_to_90_day_window() {
        let now = ms_days(100);
        let events = vec![
            (1, ParsedVelocityEvent::Accessed { at_ms: 0 }),       // out (~100d ago)
            (2, ParsedVelocityEvent::Accessed { at_ms: ms_days(15) }),  // in (85d ago)
            (3, ParsedVelocityEvent::Accessed { at_ms: now }),
        ];
        let s = fold_events(events).unwrap();
        assert_eq!(s.access_times_ms.len(), 2);
        assert_eq!(s.last_access_ms, now);
    }

    #[test]
    fn velocity_at_zero_prior_returns_count_7d() {
        let now = ms_days(100);
        let s = VelocityState {
            node_id: "n1".to_string(),
            access_times_ms: vec![now - ms_days(1), now - ms_days(2), now - ms_days(3)],
            last_access_ms: now,
        };
        let summary = s.velocity_at(now);
        assert_eq!(summary.access_count_7d, 3);
        // Prior 7d (days 7-14 ago) is zero → velocity = count_7d
        assert_eq!(summary.velocity_7d, 3.0);
    }

    #[test]
    fn velocity_at_with_prior_uses_ratio() {
        let now = ms_days(100);
        let s = VelocityState {
            node_id: "n1".to_string(),
            access_times_ms: vec![
                now - ms_days(10), // prior window (7-14d ago)
                now - ms_days(11), // prior window
                now - ms_days(1),  // current window
                now - ms_days(2),  // current
                now - ms_days(3),  // current
            ],
            last_access_ms: now,
        };
        let summary = s.velocity_at(now);
        assert_eq!(summary.access_count_7d, 3);
        // count_7d_prior = 2, velocity = (3 - 2) / 2 = 0.5
        assert!((summary.velocity_7d - 0.5).abs() < 1e-9);
    }

    #[test]
    fn velocity_at_skips_future_timestamps() {
        let now = ms_days(50);
        let s = VelocityState {
            node_id: "n1".to_string(),
            access_times_ms: vec![now + ms_days(10), now - ms_days(1)],
            last_access_ms: now + ms_days(10),
        };
        // Only the past timestamp should count
        let summary = s.velocity_at(now);
        assert_eq!(summary.access_count_7d, 1);
    }

    #[test]
    fn parse_node_accessed_falls_back_to_offset() {
        let evt = EventTuple {
            kind: "node_accessed".to_string(),
            payload: serde_json::to_vec(&serde_json::json!({"node_id": "n1"})).unwrap(),
            offset: 99,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "n1");
        match parsed {
            ParsedVelocityEvent::Accessed { at_ms } => assert_eq!(at_ms, 99),
            _ => panic!("expected Accessed"),
        }
    }
}
