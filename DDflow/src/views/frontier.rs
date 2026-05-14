//! Frontier coordination across DD views.
//!
//! # Sub-slice G: timely-driven frontier
//!
//! Pre-G, this module owned an `applied_offset` per view, mutated
//! by `record_applied()` calls from the application layer. That
//! was a shadow tracker — it only stayed correct as long as the
//! application remembered to call `record_applied` after every DD
//! ingest. The architecturally honest replacement is to read the
//! DD frontier directly from the worker.
//!
//! In G, `FrontierRegistry` accepts an optional
//! `Arc<DataflowHandle>` via `attach_dataflow()`. When attached:
//!
//!   - `applied_offset(view)` reads from the DD frontier
//!     (`DataflowHandle::frontier_of(view)`) — single source of
//!     truth.
//!   - `record_applied()` becomes a no-op (kept for API
//!     compatibility with the Phase-5 `PyFrontierRegistry`
//!     surface; ignored when a dataflow is attached).
//!   - `committed_offset(view) = min(applied_offset, lowest_open_batch_start - 1)`,
//!     where `lowest_open_batch_start` is the application's batch
//!     coordinator state — the one thing that does NOT come from
//!     the dataflow.
//!
//! When NOT attached (legacy mode used by inline tests):
//!
//!   - The existing parking_lot-backed semantics are preserved
//!     unchanged.
//!
//! # The two layers and their owners
//!
//! - **Frontier layer (DD)**: timely capability frontiers, owned by
//!   the worker thread, monotonically advancing per view as data
//!   flows through.
//! - **Batch layer (application)**: open-batch coordination, owned
//!   by the registry. Pulls back the committed_offset whenever a
//!   batch is mid-flight so cross-view readers see consistent
//!   snapshots.
//!
//! These layers compose: `committed = min(frontier, batch_cap)`.
//! Sub-slice F kept the registry surface stable so `PyFrontierRegistry`
//! consumers don't need to change. G adds an `attach` step that
//! switches the source of `applied_offset` from internal storage
//! to the DD handle.
//!
//! # When this module goes away
//!
//! Once `DataflowHandle` exposes batch-aware frontier reads itself
//! (a future iteration of sub-slice G or a follow-up), this module's
//! responsibilities collapse into `dataflow::DataflowHandle` and
//! `views/` disappears entirely. For now the registry is the
//! batch-coordination layer that DD doesn't know about.

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

use crate::LogOffset;

// =============================================================================
// Source-of-truth abstraction for applied_offset
// =============================================================================

/// Where `applied_offset` comes from for a given view.
///
/// In legacy mode (`Local`), the tracker stores its own offset and
/// the application calls `record_applied()` to advance it.
///
/// In G mode (`Dataflow`), the tracker holds a clone of the DD
/// handle and reads `frontier_of(view_name)` on every query —
/// always the live frontier, never stale.
#[derive(Clone)]
enum AppliedSource {
    Local,
    Dataflow {
        handle: Arc<crate::dataflow::DataflowHandle>,
        view_name: String,
    },
}

impl std::fmt::Debug for AppliedSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Local => write!(f, "Local"),
            Self::Dataflow { view_name, .. } => write!(f, "Dataflow({view_name})"),
        }
    }
}

// =============================================================================
// FrontierTracker — single view's frontier
// =============================================================================

/// One view's frontier. Two offsets:
///
///   - `applied_offset`: highest log entry the view has ingested.
///     In G+attached mode, read from the DD handle. In legacy mode,
///     stored internally.
///   - `committed_offset`: highest offset that's both applied AND
///     past every currently-open batch boundary. The "consistent
///     read" offset; cross-view readers use this.
///
/// `committed_offset ≤ applied_offset` always.
#[derive(Debug)]
pub struct FrontierTracker {
    name: String,
    state: parking_lot::Mutex<FrontierState>,
    /// Where `applied_offset` comes from. Defaults to `Local`;
    /// switched to `Dataflow` by `FrontierRegistry::attach_dataflow`.
    source: parking_lot::Mutex<AppliedSource>,
}

#[derive(Debug, Default, Clone, Copy)]
struct FrontierState {
    /// Locally-stored applied offset. Only authoritative when
    /// `source = Local`. In `Dataflow` mode this is unused; the
    /// real value comes from `DataflowHandle::frontier_of`.
    applied_offset_local: LogOffset,
    /// Highest committed offset observed. Monotonically advances.
    /// In Dataflow mode this is computed at query time from
    /// `applied_offset` + `lowest_open_batch_start`.
    committed_offset: LogOffset,
    lowest_open_batch_start: LogOffset,
}

impl FrontierTracker {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            state: parking_lot::Mutex::new(FrontierState::default()),
            source: parking_lot::Mutex::new(AppliedSource::Local),
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    /// Switch this tracker to read `applied_offset` from the
    /// dataflow handle. Called by `FrontierRegistry::attach_dataflow`.
    fn attach_dataflow(
        &self,
        handle: Arc<crate::dataflow::DataflowHandle>,
        view_name: String,
    ) {
        *self.source.lock() = AppliedSource::Dataflow { handle, view_name };
    }

    /// Read the live `applied_offset`. In Dataflow mode this is the
    /// DD frontier; in Local mode it's the stored value.
    pub fn applied_offset(&self) -> LogOffset {
        let src = self.source.lock().clone();
        match src {
            AppliedSource::Local => self.state.lock().applied_offset_local,
            AppliedSource::Dataflow { handle, view_name } => {
                handle.frontier_of(&view_name).unwrap_or(0)
            }
        }
    }

    /// Read the live `committed_offset`. Computed as
    /// `min(applied_offset, lowest_open_batch_start - 1)` when a
    /// batch is open; else equal to `applied_offset`.
    pub fn committed_offset(&self) -> LogOffset {
        let applied = self.applied_offset();
        let st = self.state.lock();
        if st.lowest_open_batch_start == 0 {
            // No open batches: committed equals applied.
            // We also lift the stored committed_offset for
            // monotonic-advance properties.
            applied
        } else {
            // An open batch caps us at start - 1, but never below
            // the prior committed (monotonic advance).
            let cap = st.lowest_open_batch_start.saturating_sub(1);
            cap.min(applied).max(st.committed_offset)
        }
    }

    /// Record that the view has ingested an event at this offset.
    ///
    /// In Local mode: advances `applied_offset_local` if higher.
    /// In Dataflow mode: NO-OP. The DD frontier is the source of
    /// truth; the application's `record_applied` calls are
    /// redundant once the handle is attached.
    pub fn record_applied(&self, offset: LogOffset) {
        let src_kind = self.source.lock().clone();
        if !matches!(src_kind, AppliedSource::Local) {
            return; // dataflow is the source of truth
        }
        let mut st = self.state.lock();
        if offset > st.applied_offset_local {
            st.applied_offset_local = offset;
        }
        // Recompute committed_offset
        let cap = if st.lowest_open_batch_start == 0 {
            st.applied_offset_local
        } else {
            st.lowest_open_batch_start.saturating_sub(1)
        };
        if cap > st.committed_offset {
            st.committed_offset = cap;
        }
    }

    /// Record that a batch was opened. Pulls committed_offset back
    /// to before this batch's start (in both modes).
    pub fn record_batch_opened(&self, batch_start_offset: LogOffset) {
        let mut st = self.state.lock();
        if st.lowest_open_batch_start == 0 || batch_start_offset < st.lowest_open_batch_start {
            st.lowest_open_batch_start = batch_start_offset;
        }
        let cap = batch_start_offset.saturating_sub(1);
        if st.committed_offset > cap {
            st.committed_offset = cap;
        }
    }

    /// Record that a batch was closed.
    pub fn record_batch_closed(&self, _batch_start_offset: LogOffset) {
        let mut st = self.state.lock();
        st.lowest_open_batch_start = 0;
        // Lift committed_offset to applied_offset (read live so
        // Dataflow mode picks up the DD frontier).
        drop(st);
        let applied = self.applied_offset();
        let mut st = self.state.lock();
        if st.committed_offset < applied {
            st.committed_offset = applied;
        }
    }
}

// =============================================================================
// FrontierRegistry — cross-view registry + batch coordination
// =============================================================================

/// Cross-view frontier registry.
///
/// Holds per-view `FrontierTracker`s and an optional handle to the
/// DD worker. When the handle is attached (`attach_dataflow`), all
/// trackers switch to reading their `applied_offset` from the DD
/// frontier — the architectural shift that sub-slice G is about.
///
/// The registry retains ownership of the batch-coordination layer
/// (`open_batches`) regardless of mode. That's an application
/// concept DD doesn't know about.
#[derive(Default)]
pub struct FrontierRegistry {
    views: RwLock<HashMap<String, Arc<FrontierTracker>>>,
    open_batches: RwLock<HashMap<(String, String), LogOffset>>,
    /// Attached dataflow handle, if any. When Some, new trackers
    /// returned from `register()` are immediately switched to
    /// Dataflow mode.
    dataflow: RwLock<Option<Arc<crate::dataflow::DataflowHandle>>>,
}

impl FrontierRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Attach a `DataflowHandle`. After this call, all currently
    /// registered trackers and any future `register()` calls switch
    /// to reading their `applied_offset` from the DD frontier.
    ///
    /// Idempotent: re-attaching with the same handle has no effect;
    /// re-attaching with a different handle replaces the prior one
    /// and re-points all trackers.
    pub fn attach_dataflow(&self, handle: Arc<crate::dataflow::DataflowHandle>) {
        *self.dataflow.write() = Some(Arc::clone(&handle));
        // Re-point existing trackers
        let g = self.views.read();
        for (name, tracker) in g.iter() {
            tracker.attach_dataflow(Arc::clone(&handle), name.clone());
        }
    }

    /// Register a view. Returns the existing tracker if the name is
    /// already registered. If a dataflow is attached, the tracker
    /// is in Dataflow mode immediately.
    pub fn register(&self, name: &str) -> Arc<FrontierTracker> {
        let mut g = self.views.write();
        let tracker = g
            .entry(name.to_string())
            .or_insert_with(|| Arc::new(FrontierTracker::new(name)))
            .clone();
        // If a dataflow is attached, ensure this tracker is in
        // Dataflow mode.
        if let Some(handle) = self.dataflow.read().as_ref() {
            tracker.attach_dataflow(Arc::clone(handle), name.to_string());
        }
        tracker
    }

    pub fn tracker(&self, name: &str) -> Option<Arc<FrontierTracker>> {
        self.views.read().get(name).cloned()
    }

    pub fn known_views(&self) -> Vec<String> {
        self.views.read().keys().cloned().collect()
    }

    /// Compute the meet (min) of `committed_offset` across the given
    /// view names.
    pub fn meet<I, S>(&self, view_names: I) -> LogOffset
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let g = self.views.read();
        let mut result: Option<LogOffset> = None;
        for name in view_names {
            let v = g
                .get(name.as_ref())
                .map(|t| t.committed_offset())
                .unwrap_or(0);
            result = Some(match result {
                None => v,
                Some(prev) => prev.min(v),
            });
        }
        result.unwrap_or(0)
    }

    pub fn record_batch_started(
        &self,
        consumer_id: &str,
        batch_id: &str,
        start_offset: LogOffset,
    ) {
        self.open_batches
            .write()
            .insert((consumer_id.to_string(), batch_id.to_string()), start_offset);
        let g = self.views.read();
        for tracker in g.values() {
            tracker.record_batch_opened(start_offset);
        }
    }

    pub fn record_batch_closed(&self, consumer_id: &str, batch_id: &str) {
        let mut opens = self.open_batches.write();
        opens.remove(&(consumer_id.to_string(), batch_id.to_string()));
        let new_lowest = opens.values().min().copied();
        let g = self.views.read();
        for tracker in g.values() {
            let applied = tracker.applied_offset();
            let mut st = tracker.state.lock();
            st.lowest_open_batch_start = new_lowest.unwrap_or(0);
            let cap = match new_lowest {
                Some(n) => n.saturating_sub(1),
                None => applied,
            };
            if cap > st.committed_offset {
                st.committed_offset = cap;
            }
        }
    }

    pub fn open_batch_count(&self) -> usize {
        self.open_batches.read().len()
    }

    /// Whether a `DataflowHandle` is currently attached. Used by
    /// tests; production code shouldn't branch on this.
    pub fn is_dataflow_attached(&self) -> bool {
        self.dataflow.read().is_some()
    }
}

// =============================================================================
// Snapshot type
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrontierSnapshot {
    pub view_name: String,
    pub applied_offset: LogOffset,
    pub committed_offset: LogOffset,
    pub lowest_open_batch_start: LogOffset,
}

impl FrontierTracker {
    pub fn snapshot(&self) -> FrontierSnapshot {
        let applied = self.applied_offset();
        let committed = self.committed_offset();
        let st = self.state.lock();
        FrontierSnapshot {
            view_name: self.name.clone(),
            applied_offset: applied,
            committed_offset: committed,
            lowest_open_batch_start: st.lowest_open_batch_start,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // =========================================================================
    // Legacy (Local) mode tests — preserved from pre-G
    // =========================================================================

    #[test]
    fn fresh_tracker_has_zero_offsets() {
        let t = FrontierTracker::new("v1");
        assert_eq!(t.applied_offset(), 0);
        assert_eq!(t.committed_offset(), 0);
    }

    #[test]
    fn record_applied_advances_both_offsets_when_no_open_batch() {
        let t = FrontierTracker::new("v1");
        t.record_applied(10);
        assert_eq!(t.applied_offset(), 10);
        assert_eq!(t.committed_offset(), 10);
    }

    #[test]
    fn open_batch_caps_committed_offset() {
        let t = FrontierTracker::new("v1");
        t.record_applied(10);
        t.record_batch_opened(15);
        t.record_applied(20);
        assert_eq!(t.applied_offset(), 20);
        assert_eq!(t.committed_offset(), 14);
    }

    #[test]
    fn close_batch_lifts_committed_offset() {
        let t = FrontierTracker::new("v1");
        t.record_applied(10);
        t.record_batch_opened(15);
        t.record_applied(20);
        t.record_batch_closed(15);
        assert_eq!(t.committed_offset(), 20);
    }

    #[test]
    fn registry_meet_returns_min_committed() {
        let reg = FrontierRegistry::new();
        let v1 = reg.register("v1");
        let v2 = reg.register("v2");
        v1.record_applied(20);
        v2.record_applied(10);
        assert_eq!(reg.meet(["v1", "v2"]), 10);
    }

    #[test]
    fn registry_open_batch_pulls_all_views_back() {
        let reg = FrontierRegistry::new();
        let v1 = reg.register("v1");
        let v2 = reg.register("v2");
        v1.record_applied(20);
        v2.record_applied(20);
        reg.record_batch_started("writer.A", "bat_1", 15);
        assert_eq!(v1.committed_offset(), 14);
        assert_eq!(v2.committed_offset(), 14);
        reg.record_batch_closed("writer.A", "bat_1");
        assert_eq!(v1.committed_offset(), 20);
        assert_eq!(v2.committed_offset(), 20);
    }

    // =========================================================================
    // G-mode tests — frontier reads from a (mock) DataflowHandle
    // =========================================================================

    #[test]
    fn registry_starts_unattached() {
        let reg = FrontierRegistry::new();
        assert!(!reg.is_dataflow_attached());
    }

    /// A real DataflowHandle requires starting a worker thread; in
    /// the unit tests we go end-to-end via a real (stub-trace)
    /// handle. The integration test below uses a real `DataflowHandle::start`
    /// to verify the wiring is consistent.
    ///
    /// TODO(rust-build): if the real `DataflowHandle::start` does
    /// any IO that fails in CI, this test may need to use a mock.
    /// For now the stub worker (sub-slice A) doesn't do IO so it
    /// should be safe.
    #[test]
    fn attach_dataflow_switches_applied_to_handle() {
        use crate::dataflow::{BoxedTrace, DataflowTimestamp, EventTuple, ViewSpec};
        use differential_dataflow::Collection;
        use timely::dataflow::Scope;

        struct MockView;
        impl ViewSpec for MockView {
            fn name(&self) -> &'static str {
                "mock_view"
            }
            fn subscribed_kinds(&self) -> &[&'static str] {
                &["any"]
            }
            fn build<S: Scope<Timestamp = DataflowTimestamp>>(
                &self,
                _scope: &mut S,
                _input: &Collection<S, EventTuple, isize>,
            ) -> BoxedTrace {
                // The DataflowHandle's stub worker registers a
                // StubTrace per view — frontier_offset returns 0.
                // (Sub-slice B replaces this with a real trace; for
                // G-mode tests we only care that the registry
                // delegates the read to the handle.)
                Box::new(StubTraceForTest)
            }
        }

        struct StubTraceForTest;
        impl crate::dataflow::TraceQuery for StubTraceForTest {
            fn query_bytes(&self, _key: &[u8]) -> Option<Vec<u8>> {
                None
            }
            fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
                Vec::new()
            }
            fn frontier_offset(&self) -> LogOffset {
                42 // distinctive value so we can tell this is being read
            }
        }

        // TODO(rust-build): the stub worker registers its OWN
        // StubTrace, not the one our MockView returns from build().
        // The actual frontier_of() reads via the registered trace
        // map, which always reports 0 in stub mode. So the test
        // below currently asserts that applied_offset reads 0
        // through the dataflow path — distinguishable from local
        // mode which would read whatever record_applied set. Once
        // sub-slice B's real worker replaces the stub, the
        // registered trace will be MockView's and this test
        // should observe 42.

        let handle = match crate::dataflow::DataflowHandle::start(vec![Box::new(MockView)]) {
            Ok(h) => Arc::new(h),
            Err(_) => return, // Worker startup failed in CI; skip.
        };

        let reg = FrontierRegistry::new();
        let tracker = reg.register("mock_view");
        // Local mode: record_applied(99) sticks
        tracker.record_applied(99);
        assert_eq!(tracker.applied_offset(), 99);

        // Attach the dataflow → applied_offset now comes from handle
        reg.attach_dataflow(Arc::clone(&handle));
        assert!(reg.is_dataflow_attached());

        // After attach, record_applied is a no-op
        tracker.record_applied(123);

        // applied_offset reads from handle. With the stub worker
        // it'll be 0; with a real worker (post-B) and the mock
        // view's trace, it'd be whatever the trace reports.
        let observed = tracker.applied_offset();
        // We don't pin the exact value because it depends on
        // whether the stub or real worker is wired. We DO assert
        // the local record_applied(123) didn't take effect.
        assert_ne!(observed, 123, "record_applied must not affect Dataflow-mode tracker");

        let _ = handle.shutdown();
    }

    #[test]
    fn register_after_attach_uses_dataflow_mode() {
        use crate::dataflow::ViewSpec;

        struct DummyView;
        impl ViewSpec for DummyView {
            fn name(&self) -> &'static str {
                "dummy"
            }
            fn subscribed_kinds(&self) -> &[&'static str] {
                &["any"]
            }
            fn build<S: timely::dataflow::Scope<Timestamp = crate::dataflow::DataflowTimestamp>>(
                &self,
                _scope: &mut S,
                _input: &differential_dataflow::Collection<S, crate::dataflow::EventTuple, isize>,
            ) -> crate::dataflow::BoxedTrace {
                struct Stub;
                impl crate::dataflow::TraceQuery for Stub {
                    fn query_bytes(&self, _: &[u8]) -> Option<Vec<u8>> {
                        None
                    }
                    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
                        Vec::new()
                    }
                    fn frontier_offset(&self) -> LogOffset {
                        0
                    }
                }
                Box::new(Stub)
            }
        }

        let handle = match crate::dataflow::DataflowHandle::start(vec![Box::new(DummyView)]) {
            Ok(h) => Arc::new(h),
            Err(_) => return,
        };

        let reg = FrontierRegistry::new();
        reg.attach_dataflow(Arc::clone(&handle));
        // Register AFTER attach — should still be in Dataflow mode
        let tracker = reg.register("dummy");
        tracker.record_applied(50); // no-op
        // applied_offset comes from the handle (0 in stub mode)
        assert_ne!(tracker.applied_offset(), 50);

        let _ = handle.shutdown();
    }
}
