//! `heat_field` as a Differential Dataflow view (sub-slice D).
//!
//! Continuous heat value per node: bumped by access events, with a
//! canonical-floor that prevents canon nodes from cooling below a
//! threshold.
//!
//! # Architectural shift from the legacy view
//!
//! Legacy `views/heat_field.rs` had two update paths:
//!   1. `apply()` — observed `node_property_set("canonical", ...)`
//!      to keep the canonical flag in sync.
//!   2. **External `bump()` and `decay_to()` methods** — direct
//!      mutation by retrieval consumers when a node was accessed.
//!
//! Path (2) is incompatible with DD's "view is a function of the
//! event stream" model. The DD version replaces those external
//! mutations with subscription to a new event kind:
//!
//!     node_accessed { node_id, accessed_at_ms }
//!
//! Retrieval consumers append this event when they touch a node.
//! Heat then becomes a pure function of `(node_created, accessed,
//! property_set("canonical"))` events, with no side-channel state.
//!
//! # No external `bump()` / `decay_to()` API
//!
//! The legacy view's `bump(node_id, at_ms)` becomes "append a
//! `node_accessed` event." `decay_to(node_id, now_ms, half_life)`
//! becomes "query the view at `now`; decay is computed lazily in
//! the reduce closure based on the time elapsed since
//! `last_accessed_ms`."
//!
//! Because retrieval has not been wired to emit `node_accessed`
//! events yet, this view will be empty until that wiring exists.
//! That's the same observable behavior as the legacy view (which
//! had no callers of `bump()` either — see audit in sub-slice D
//! plan). Rankers reading `heat` get the configured `default_heat`
//! when no entry exists.
//!
//! # Decay model
//!
//! Heat is stored as an "anchor": `(heat_at_anchor, anchor_ms)`.
//! Queries compute `heat_now = decay(heat_at_anchor, now_ms -
//! anchor_ms, half_life)`. This avoids needing periodic
//! recomputation events to keep heat current; the ranker's query
//! at `now` does the math.
//!
//! # Operator chain
//!
//! Same shape: filter → flat_map → reduce → arrange. The reduce
//! folds the event stream into a single `HeatState` per node,
//! tracking the cumulative bump count and the last-bumped
//! timestamp.
//!
//! # Coexistence with the legacy view
//!
//! Legacy `views/heat_field.rs` exists until sub-slice F.

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

// =============================================================================
// Configuration constants
// =============================================================================

/// Bumped per `node_accessed` event.
pub const DEFAULT_BUMP_AMOUNT: f64 = 0.05;

/// Heat value canonical nodes don't fall below.
pub const DEFAULT_CANON_FLOOR: f64 = 0.95;

/// Initial heat for a brand-new node. Returned by queries when no
/// access has been observed yet.
pub const DEFAULT_HEAT: f64 = 0.5;

/// Half-life for time-based decay (in days). Decay is applied lazily
/// at query time, not in the reduce.
pub const DEFAULT_HALF_LIFE_DAYS: f64 = 30.0;

const MS_PER_DAY: f64 = 86_400_000.0;

// =============================================================================
// HeatState — DD-compatible
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct HeatState {
    pub node_id: String,
    /// Heat value at the anchor moment (after the most recent bump).
    /// Stored as IEEE-754 bits for DD Eq/Hash compat.
    pub heat_at_anchor_bits: u64,
    /// Wall-clock ms when `heat_at_anchor` was set.
    pub anchor_ms: u64,
    /// Cumulative count of `node_accessed` events for this node.
    pub access_count: u64,
    /// Whether the node is currently in canon-set. Flipped by
    /// `node_property_set("canonical", ...)`.
    pub canonical: bool,
}

impl HeatState {
    pub fn heat_at_anchor(&self) -> f64 {
        f64::from_bits(self.heat_at_anchor_bits)
    }

    pub fn set_heat_at_anchor(&mut self, h: f64) {
        self.heat_at_anchor_bits = h.to_bits();
    }

    /// Compute the current heat at `now_ms` using exponential decay.
    /// Canon nodes are floored at `canon_floor`.
    pub fn heat_at(&self, now_ms: u64, half_life_days: f64, canon_floor: f64) -> f64 {
        let elapsed_ms = now_ms.saturating_sub(self.anchor_ms) as f64;
        let elapsed_days = elapsed_ms / MS_PER_DAY;
        let decay = (-elapsed_days * std::f64::consts::LN_2 / half_life_days).exp();
        let mut h = self.heat_at_anchor() * decay;
        if self.canonical && h < canon_floor {
            h = canon_floor;
        }
        h
    }
}

// =============================================================================
// Payloads
// =============================================================================

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeAccessedPayload {
    node_id: String,
    #[serde(default)]
    accessed_at_ms: u64,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct NodeCreatedPayload {
    node_id: String,
    #[serde(default)]
    canonical: bool,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct PropertySetPayload {
    node_id: String,
    field_name: String,
    new_value: serde_json::Value,
}

#[derive(Clone, Debug)]
enum ParsedHeatEvent {
    Created { canonical: bool },
    Accessed { at_ms: u64 },
    CanonicalChanged(bool),
}

fn parse_event(evt: &EventTuple) -> Option<(String, ParsedHeatEvent)> {
    match evt.kind.as_str() {
        "node_created" => {
            let p: NodeCreatedPayload = serde_json::from_slice(&evt.payload).ok()?;
            Some((
                p.node_id,
                ParsedHeatEvent::Created {
                    canonical: p.canonical,
                },
            ))
        }
        "node_accessed" => {
            let p: NodeAccessedPayload = serde_json::from_slice(&evt.payload).ok()?;
            // If accessed_at_ms is 0 (default), fall back to the offset
            // as a stand-in monotonic time. Real consumers should
            // populate it.
            let at_ms = if p.accessed_at_ms == 0 {
                evt.offset
            } else {
                p.accessed_at_ms
            };
            Some((p.node_id, ParsedHeatEvent::Accessed { at_ms }))
        }
        "node_property_set" => {
            let p: PropertySetPayload = serde_json::from_slice(&evt.payload).ok()?;
            if p.field_name == "canonical" {
                p.new_value
                    .as_bool()
                    .map(|b| (p.node_id, ParsedHeatEvent::CanonicalChanged(b)))
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Fold heat events into a single state. Each access adds
/// `bump_amount` to heat (capped at 1.0). Decay is *not* applied
/// in the fold — it's applied at query time so the stored state
/// represents the heat *at the most recent access*, not at "now".
fn fold_events(events: Vec<(LogOffset, ParsedHeatEvent)>, bump_amount: f64) -> Option<HeatState> {
    let mut state: Option<HeatState> = None;
    for (_offset, evt) in events {
        match evt {
            ParsedHeatEvent::Created { canonical } => {
                state = Some(HeatState {
                    node_id: String::new(),
                    heat_at_anchor_bits: DEFAULT_HEAT.to_bits(),
                    anchor_ms: 0,
                    access_count: 0,
                    canonical,
                });
            }
            ParsedHeatEvent::Accessed { at_ms } => {
                let s = state.get_or_insert(HeatState {
                    node_id: String::new(),
                    heat_at_anchor_bits: DEFAULT_HEAT.to_bits(),
                    anchor_ms: 0,
                    access_count: 0,
                    canonical: false,
                });
                let new_heat = (s.heat_at_anchor() + bump_amount).min(1.0);
                s.set_heat_at_anchor(new_heat);
                s.anchor_ms = at_ms;
                s.access_count += 1;
            }
            ParsedHeatEvent::CanonicalChanged(b) => {
                if let Some(ref mut s) = state {
                    s.canonical = b;
                }
            }
        }
    }
    state
}

// =============================================================================
// View
// =============================================================================

pub struct HeatFieldView {
    snapshot: Arc<RwLock<std::collections::HashMap<String, HeatState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
    bump_amount: f64,
}

impl HeatFieldView {
    pub fn new() -> Self {
        Self::with_bump(DEFAULT_BUMP_AMOUNT)
    }

    pub fn with_bump(bump_amount: f64) -> Self {
        Self {
            snapshot: Arc::new(RwLock::new(std::collections::HashMap::new())),
            frontier: Arc::new(parking_lot::Mutex::new(0)),
            bump_amount,
        }
    }
}

impl ViewSpec for HeatFieldView {
    fn name(&self) -> &'static str {
        "heat_field"
    }

    fn subscribed_kinds(&self) -> &[&'static str] {
        // Note: `node_accessed` is a NEW event kind (sub-slice D).
        // Until retrieval consumers wire up emission of these, the
        // view will only see node_created + canonical changes. This
        // is a deliberate, documented architectural shift — see the
        // module-level docstring.
        &["node_created", "node_accessed", "node_property_set"]
    }

    fn build<S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        _scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace {
        let snapshot_inspect = Arc::clone(&self.snapshot);
        let frontier_inspect = Arc::clone(&self.frontier);
        let bump = self.bump_amount;

        let keyed = input.flat_map(|evt: EventTuple| {
            parse_event(&evt).map(|(node_id, parsed)| (node_id, (evt.offset, parsed)))
        });

        let state_collection = keyed.reduce(move |node_id, input_events, output| {
            let mut events: Vec<(LogOffset, ParsedHeatEvent)> = input_events
                .iter()
                .filter(|&&(_, count)| count > 0)
                .map(|&(&(offset, ref parsed), _)| (offset, parsed.clone()))
                .collect();
            events.sort_by(|a, b| a.0.cmp(&b.0));

            if let Some(mut state) = fold_events(events, bump) {
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

        Box::new(HeatFieldSnapshotQuery {
            snapshot: Arc::clone(&self.snapshot),
            frontier: Arc::clone(&self.frontier),
        })
    }
}

struct HeatFieldSnapshotQuery {
    snapshot: Arc<RwLock<std::collections::HashMap<String, HeatState>>>,
    frontier: Arc<parking_lot::Mutex<LogOffset>>,
}

impl TraceQuery for HeatFieldSnapshotQuery {
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>> {
        let key = std::str::from_utf8(key_bytes).ok()?.to_string();
        let snap = self.snapshot.read();
        let s = snap.get(&key)?;
        let mut json = serde_json::Map::new();
        json.insert("node_id".to_string(), serde_json::Value::String(s.node_id.clone()));
        json.insert(
            "heat_at_anchor".to_string(),
            serde_json::Value::from(s.heat_at_anchor()),
        );
        json.insert("anchor_ms".to_string(), serde_json::Value::from(s.anchor_ms));
        json.insert(
            "access_count".to_string(),
            serde_json::Value::from(s.access_count),
        );
        json.insert("canonical".to_string(), serde_json::Value::Bool(s.canonical));
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

    #[test]
    fn fold_two_accesses_increases_heat() {
        let events = vec![
            (1, ParsedHeatEvent::Created { canonical: false }),
            (2, ParsedHeatEvent::Accessed { at_ms: 1000 }),
            (3, ParsedHeatEvent::Accessed { at_ms: 2000 }),
        ];
        let s = fold_events(events, 0.1).unwrap();
        // heat = 0.5 (default) + 0.1 + 0.1 = 0.7
        assert!((s.heat_at_anchor() - 0.7).abs() < 1e-9);
        assert_eq!(s.access_count, 2);
        assert_eq!(s.anchor_ms, 2000);
    }

    #[test]
    fn fold_caps_heat_at_one() {
        let events = vec![
            (1, ParsedHeatEvent::Created { canonical: false }),
            (2, ParsedHeatEvent::Accessed { at_ms: 1 }),
            (3, ParsedHeatEvent::Accessed { at_ms: 2 }),
            (4, ParsedHeatEvent::Accessed { at_ms: 3 }),
            (5, ParsedHeatEvent::Accessed { at_ms: 4 }),
            (6, ParsedHeatEvent::Accessed { at_ms: 5 }),
            (7, ParsedHeatEvent::Accessed { at_ms: 6 }),
            (8, ParsedHeatEvent::Accessed { at_ms: 7 }),
        ];
        let s = fold_events(events, 0.5).unwrap(); // 0.5 + 7*0.5 = 4.0 capped at 1.0
        assert!((s.heat_at_anchor() - 1.0).abs() < 1e-9);
    }

    #[test]
    fn heat_at_decays_over_time() {
        let s = HeatState {
            node_id: "n1".to_string(),
            heat_at_anchor_bits: 1.0f64.to_bits(),
            anchor_ms: 0,
            access_count: 1,
            canonical: false,
        };
        // 30 days later with 30-day half-life → 0.5
        let h = s.heat_at(30 * 86_400_000, 30.0, 0.0);
        assert!((h - 0.5).abs() < 1e-6);
    }

    #[test]
    fn canon_floor_bounds_decay() {
        let s = HeatState {
            node_id: "n1".to_string(),
            heat_at_anchor_bits: 0.5f64.to_bits(),
            anchor_ms: 0,
            access_count: 1,
            canonical: true,
        };
        // Decay would take it to ~0.001; canon floor at 0.95
        let h = s.heat_at(365 * 86_400_000, 30.0, 0.95);
        assert!((h - 0.95).abs() < 1e-6);
    }

    #[test]
    fn parse_node_accessed_uses_event_field() {
        let evt = EventTuple {
            kind: "node_accessed".to_string(),
            payload: serde_json::to_vec(&serde_json::json!({
                "node_id": "n1",
                "accessed_at_ms": 12345,
            }))
            .unwrap(),
            offset: 7,
        };
        let (id, parsed) = parse_event(&evt).unwrap();
        assert_eq!(id, "n1");
        match parsed {
            ParsedHeatEvent::Accessed { at_ms } => assert_eq!(at_ms, 12345),
            _ => panic!("expected Accessed"),
        }
    }

    #[test]
    fn parse_node_accessed_falls_back_to_offset() {
        let evt = EventTuple {
            kind: "node_accessed".to_string(),
            payload: serde_json::to_vec(&serde_json::json!({
                "node_id": "n1",
            }))
            .unwrap(),
            offset: 99,
        };
        let (_, parsed) = parse_event(&evt).unwrap();
        match parsed {
            ParsedHeatEvent::Accessed { at_ms } => assert_eq!(at_ms, 99),
            _ => panic!("expected Accessed"),
        }
    }

    #[test]
    fn parse_property_set_only_canonical_field() {
        let canonical_evt = EventTuple {
            kind: "node_property_set".to_string(),
            payload: serde_json::to_vec(&serde_json::json!({
                "node_id": "n1",
                "field_name": "canonical",
                "new_value": true,
            }))
            .unwrap(),
            offset: 1,
        };
        let parsed = parse_event(&canonical_evt).unwrap().1;
        assert!(matches!(parsed, ParsedHeatEvent::CanonicalChanged(true)));

        let other_evt = EventTuple {
            kind: "node_property_set".to_string(),
            payload: serde_json::to_vec(&serde_json::json!({
                "node_id": "n1",
                "field_name": "importance",
                "new_value": 0.9,
            }))
            .unwrap(),
            offset: 2,
        };
        assert!(parse_event(&other_evt).is_none());
    }
}
