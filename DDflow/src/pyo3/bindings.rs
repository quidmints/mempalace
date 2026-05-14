//! PyO3 bindings.
//!
//! Exposes the Rust log + DD dataflow to Python consumers. The
//! Python side reaches these via `import mempalace_core` (the
//! lib.rs registers this module name).
//!
//! # Sub-slice F change
//!
//! Before sub-slice F, `PyLogClient` constructed a legacy
//! `ViewBuilder` from `parking_lot::RwLock<HashMap>` views and
//! called `apply_batch` synchronously on every append. The legacy
//! `View` trait, the views, and the `ViewBuilder` itself are all
//! deleted in this sub-slice. This file is the only place
//! externally visible from Python that needed rewiring.
//!
//! The replacement: `PyLogClient` now constructs a `DataflowHandle`
//! from the 14 `ViewSpec` impls in `crate::dataflow::views::*` and
//! uses `feed` + `advance_to` to drive it. The Python-visible
//! semantics are preserved: when `append()` returns, the views are
//! up-to-date through the appended offset.
//!
//! # PyFrontierRegistry — unchanged in F
//!
//! Phase 5's `parking_lot`-backed `FrontierRegistry` (in
//! `crate::views::frontier`) continues to back `PyFrontierRegistry`.
//! Sub-slice G replaces it with a registry driven by actual timely
//! capability frontiers from the `DataflowHandle`. Until then, the
//! Python-side `mempalace/log/rust_bridge.py` keeps using it as
//! before — no Python changes in F.
//!
//! Spec ref: Part 2.4

use std::path::PathBuf;
use std::sync::Arc;

use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;

use crate::dataflow::views::{
    active_iams::ActiveIamsView, active_periods::ActivePeriodsView,
    canon_set::CanonSetView, current_edges::CurrentEdgesView,
    current_interpretations::CurrentInterpretationsView, current_nodes::CurrentNodesView,
    current_schemas::CurrentSchemasView, heat_field::HeatFieldView,
    match_cache::MatchCacheView, matched_against::MatchedAgainstView,
    open_contradictions::OpenContradictionsView, pending_review::PendingReviewView,
    recurrence_clusters::RecurrenceClustersView, velocity_field::VelocityFieldView,
};
use crate::dataflow::{DataflowError, DataflowHandle, ViewSpec};
use crate::log::append::{LogAppender, LogError};
use crate::pyo3::types::{entries_to_pylist, pydict_to_json_bytes};

fn map_log_err(e: LogError) -> PyErr {
    match e {
        LogError::Io(e) => PyIOError::new_err(e.to_string()),
        other => PyValueError::new_err(other.to_string()),
    }
}

fn map_df_err(e: DataflowError) -> PyErr {
    PyValueError::new_err(format!("dataflow error: {e}"))
}

/// Build the standard set of 14 master views for a fresh palace.
///
/// TODO(rust-build): this returns `Vec<Box<dyn ViewSpec>>`. Each
/// `*::new()` returns a concrete struct; `Box::new(...)` coerces
/// to the trait object. Confirm the trait-object coercion compiles
/// — DD's `ViewSpec` requires `Send + Sync + 'static`, which all
/// the concrete views satisfy.
fn standard_views() -> Vec<Box<dyn ViewSpec>> {
    vec![
        Box::new(CurrentNodesView::new()),
        Box::new(CurrentEdgesView::new()),
        Box::new(CurrentInterpretationsView::new()),
        Box::new(CurrentSchemasView::new()),
        Box::new(HeatFieldView::new()),
        Box::new(VelocityFieldView::new()),
        Box::new(RecurrenceClustersView::new()),
        Box::new(ActivePeriodsView::new()),
        Box::new(ActiveIamsView::new()),
        Box::new(OpenContradictionsView::new()),
        Box::new(CanonSetView::new()),
        Box::new(PendingReviewView::new()),
        Box::new(MatchCacheView::new()),
        Box::new(MatchedAgainstView::new()),
    ]
}

#[pyclass]
pub struct PyLogClient {
    appender: Arc<LogAppender>,
    /// DD-backed dataflow handle. Replaces the legacy `ViewBuilder`.
    handle: DataflowHandle,
}

#[pymethods]
impl PyLogClient {
    #[new]
    pub fn new(path: String) -> PyResult<Self> {
        let appender = Arc::new(LogAppender::open(PathBuf::from(path)).map_err(map_log_err)?);

        // Spin up the DD worker with all 14 views registered.
        let handle = DataflowHandle::start(standard_views()).map_err(map_df_err)?;

        Ok(Self { appender, handle })
    }

    /// Append an event. Payload is a Python dict; converted to JSON bytes
    /// internally. Returns the assigned log offset.
    ///
    /// On success, the views have been advanced past `offset` —
    /// consumers reading immediately after this call see the
    /// updated state. Internally this is a `feed` + `advance_to`
    /// against the DD handle, replacing the legacy
    /// `ViewBuilder::apply_batch` path.
    pub fn append(
        &self,
        py: Python<'_>,
        event_kind: String,
        payload: &pyo3::types::PyDict,
    ) -> PyResult<u64> {
        let bytes = pydict_to_json_bytes(py, payload)?;
        let offset = self.appender.append(&event_kind, &bytes).map_err(map_log_err)?;

        // Pull the entry back from the appender so we have its
        // canonical timestamp + bytes.
        let entries = self
            .appender
            .read_range(offset, offset + 1)
            .map_err(map_log_err)?;
        if entries.is_empty() {
            return Err(PyValueError::new_err(
                "appender returned empty range after append",
            ));
        }

        // Feed into the DD dataflow and wait for views to catch up.
        // `feed` is non-blocking; `advance_to(offset)` blocks until
        // every registered view's frontier is past `offset`.
        for entry in &entries {
            self.handle.feed(entry).map_err(map_df_err)?;
        }
        self.handle.advance_to(offset).map_err(map_df_err)?;

        Ok(offset)
    }

    pub fn current_offset(&self) -> u64 {
        self.appender.current_offset()
    }

    /// Read entries in [start, end). Returns list of (offset, kind, payload_dict).
    pub fn read_range<'py>(
        &self,
        py: Python<'py>,
        start: u64,
        end: u64,
    ) -> PyResult<&'py pyo3::types::PyList> {
        let entries = self.appender.read_range(start, end).map_err(map_log_err)?;
        let tuples: Vec<(u64, String, Vec<u8>)> = entries
            .into_iter()
            .map(|e| (e.offset, e.kind, e.payload))
            .collect();
        entries_to_pylist(py, tuples)
    }

    /// Replay-style batch ingest. One frontier-await at the end
    /// instead of N. Returns the highest offset fed.
    pub fn feed_batch_replay<'py>(
        &self,
        py: Python<'py>,
        entries: &pyo3::types::PyList,
    ) -> PyResult<u64> {
        let mut highest: u64 = 0;
        for item in entries.iter() {
            let tuple: &pyo3::types::PyTuple = item.downcast()?;
            let kind: String = tuple.get_item(0)?.extract()?;
            let payload_dict: &pyo3::types::PyDict = tuple.get_item(1)?.downcast()?;
            let bytes = pydict_to_json_bytes(py, payload_dict)?;
            let offset = self.appender.append(&kind, &bytes).map_err(map_log_err)?;
            highest = offset;

            let read = self
                .appender
                .read_range(offset, offset + 1)
                .map_err(map_log_err)?;
            for entry in &read {
                self.handle.feed(entry).map_err(map_df_err)?;
            }
        }
        if highest > 0 {
            self.handle.advance_to(highest).map_err(map_df_err)?;
        }
        Ok(highest)
    }
}

// =============================================================================
// PyDataflowHandle — Python-facing surface for direct view queries.
//
// Sub-slice F adds this minimal handle so Python tests / consumers
// can talk to the dataflow without going through PyLogClient. Used
// primarily by the behavioral tests in test_dataflow_subslice_*.py
// and by sub-slice H's full Python integration.
// =============================================================================

#[pyclass]
pub struct PyDataflowHandle {
    inner: DataflowHandle,
}

#[pymethods]
impl PyDataflowHandle {
    /// Start a fresh dataflow worker with the named subset of views,
    /// or all 14 if `view_names` is empty.
    ///
    /// TODO(rust-build): selecting a subset is convenient for tests
    /// (sub-slice B/C/D/E behavioral tests instantiate just the
    /// view they're testing). The match arms below must match the
    /// constructors in `standard_views()` — keep them in sync.
    #[staticmethod]
    pub fn start(view_names: Vec<String>) -> PyResult<Self> {
        let views: Vec<Box<dyn ViewSpec>> = if view_names.is_empty() {
            standard_views()
        } else {
            let mut selected: Vec<Box<dyn ViewSpec>> = Vec::new();
            for name in &view_names {
                let view: Box<dyn ViewSpec> = match name.as_str() {
                    "current_nodes" => Box::new(CurrentNodesView::new()),
                    "current_edges" => Box::new(CurrentEdgesView::new()),
                    "current_interpretations" => Box::new(CurrentInterpretationsView::new()),
                    "current_schemas" => Box::new(CurrentSchemasView::new()),
                    "heat_field" => Box::new(HeatFieldView::new()),
                    "velocity_field" => Box::new(VelocityFieldView::new()),
                    "recurrence_clusters" => Box::new(RecurrenceClustersView::new()),
                    "active_periods" => Box::new(ActivePeriodsView::new()),
                    "active_iams" => Box::new(ActiveIamsView::new()),
                    "open_contradictions" => Box::new(OpenContradictionsView::new()),
                    "canon_set" => Box::new(CanonSetView::new()),
                    "pending_review" => Box::new(PendingReviewView::new()),
                    "match_cache" => Box::new(MatchCacheView::new()),
                    "matched_against" => Box::new(MatchedAgainstView::new()),
                    other => {
                        return Err(PyValueError::new_err(format!(
                            "unknown view name: {other}"
                        )))
                    }
                };
                selected.push(view);
            }
            selected
        };
        let inner = DataflowHandle::start(views).map_err(map_df_err)?;
        Ok(Self { inner })
    }

    /// Feed a single event tuple into the dataflow. `payload` is
    /// raw JSON bytes; the view's parser deserializes per its
    /// expected shape.
    pub fn feed(
        &self,
        offset: u64,
        kind: String,
        payload: Vec<u8>,
    ) -> PyResult<()> {
        let entry = crate::log::append::LogEntry {
            offset,
            timestamp_ms: 0,
            kind,
            payload,
        };
        self.inner.feed(&entry).map_err(map_df_err)
    }

    pub fn advance_to(&self, offset: u64) -> PyResult<()> {
        self.inner.advance_to(offset).map_err(map_df_err)
    }

    pub fn frontier_of(&self, view_name: String) -> PyResult<u64> {
        self.inner.frontier_of(&view_name).map_err(map_df_err)
    }

    /// Look up a view by name + key. Returns the value bytes or None.
    pub fn query<'py>(
        &self,
        py: Python<'py>,
        view_name: String,
        key_bytes: Vec<u8>,
    ) -> PyResult<Option<&'py pyo3::types::PyBytes>> {
        match self.inner.query(&view_name, &key_bytes).map_err(map_df_err)? {
            Some(b) => Ok(Some(pyo3::types::PyBytes::new(py, &b))),
            None => Ok(None),
        }
    }

    pub fn known_views(&self) -> Vec<String> {
        self.inner.known_views()
    }

    pub fn shutdown(&self) -> PyResult<()> {
        self.inner.shutdown().map_err(map_df_err)
    }
}

// =============================================================================
// PyFrontierRegistry — UNCHANGED in sub-slice F.
//
// Phase 5's parking_lot-backed FrontierRegistry continues to back
// this. Sub-slice G replaces the underlying tracker with one driven
// by actual timely capability frontiers; the Python-visible API
// stays the same so `mempalace/log/rust_bridge.py` doesn't change.
// =============================================================================

#[pyclass]
pub struct PyFrontierRegistry {
    inner: Arc<crate::views::FrontierRegistry>,
}

#[pymethods]
impl PyFrontierRegistry {
    #[new]
    pub fn new() -> Self {
        Self {
            inner: Arc::new(crate::views::FrontierRegistry::new()),
        }
    }

    pub fn register(&self, name: String) -> PyResult<()> {
        self.inner.register(&name);
        Ok(())
    }

    pub fn record_applied(&self, view_name: String, offset: u64) -> PyResult<()> {
        let tracker = self.inner.register(&view_name);
        tracker.record_applied(offset);
        Ok(())
    }

    pub fn record_batch_started(
        &self,
        consumer_id: String,
        batch_id: String,
        start_offset: u64,
    ) -> PyResult<()> {
        self.inner
            .record_batch_started(&consumer_id, &batch_id, start_offset);
        Ok(())
    }

    pub fn record_batch_closed(
        &self,
        consumer_id: String,
        batch_id: String,
    ) -> PyResult<()> {
        self.inner.record_batch_closed(&consumer_id, &batch_id);
        Ok(())
    }

    pub fn committed_offset(&self, view_name: String) -> u64 {
        self.inner
            .tracker(&view_name)
            .map(|t| t.committed_offset())
            .unwrap_or(0)
    }

    pub fn applied_offset(&self, view_name: String) -> u64 {
        self.inner
            .tracker(&view_name)
            .map(|t| t.applied_offset())
            .unwrap_or(0)
    }

    pub fn meet(&self, view_names: Vec<String>) -> u64 {
        self.inner.meet(&view_names)
    }

    pub fn known_views(&self) -> Vec<String> {
        self.inner.known_views()
    }

    pub fn open_batch_count(&self) -> usize {
        self.inner.open_batch_count()
    }

    /// Attach a `PyDataflowHandle` so frontier reads come from the
    /// DD frontier rather than the local parking_lot tracker.
    /// Sub-slice G's architectural switch.
    ///
    /// TODO(rust-build): the `PyDataflowHandle` here is the
    /// PyO3-wrapped struct from this same crate. We need its inner
    /// `DataflowHandle` (an `Arc<DataflowHandle>` to feed
    /// `FrontierRegistry::attach_dataflow`). The cleanest way is a
    /// helper method on `PyDataflowHandle` that returns an
    /// `Arc<DataflowHandle>` clone — but that needs the inner
    /// field to be `Arc`-wrapped. Currently it's a bare
    /// `DataflowHandle`; clone-ability is built into the struct
    /// (`DataflowHandle: Clone` per sub-slice A's design).
    pub fn attach_dataflow(&self, handle: &PyDataflowHandle) -> PyResult<()> {
        let inner_clone = Arc::new(handle.inner.clone());
        self.inner.attach_dataflow(inner_clone);
        Ok(())
    }

    /// Whether a dataflow is currently attached to the registry.
    /// Used by tests; production code shouldn't branch on this.
    pub fn is_dataflow_attached(&self) -> bool {
        self.inner.is_dataflow_attached()
    }
}

/// Register all classes and functions on the Python module.
pub fn register(_py: Python<'_>, m: &pyo3::types::PyModule) -> PyResult<()> {
    m.add_class::<PyLogClient>()?;
    m.add_class::<PyFrontierRegistry>()?;
    m.add_class::<PyDataflowHandle>()?;
    Ok(())
}
