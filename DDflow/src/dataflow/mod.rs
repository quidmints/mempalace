//! Dataflow infrastructure for incremental view maintenance (DD sub-slice A).
//!
//! Stands up a `timely::Worker` running a single dataflow, with an
//! `InputSession` the application pushes log entries into. Views are
//! registered as `ViewSpec` implementations that build their operator
//! chain over the input collection and arrange the output for queries.
//!
//! # Why this exists
//!
//! The architecture spec (Part 2.2) commits to Differential Dataflow
//! for incremental view maintenance. Until this module landed, the
//! views were `parking_lot::RwLock<HashMap>` placeholders with
//! synchronous `apply(&LogEntry)` — correct semantics but no
//! incremental machinery. This module is the substrate the views
//! actually run on.
//!
//! # Boundaries
//!
//! - The worker runs on a dedicated OS thread.
//! - The application pushes entries via `DataflowHandle::feed`.
//! - Reads happen via arrangement-backed traces, queryable through
//!   `DataflowHandle::query`.
//! - The frontier is read directly from the timely capability frontier
//!   — not from the `parking_lot` shadow tracker that Phase 5 shipped.
//!   That alignment happens in sub-slice G.
//!
//! # What this sub-slice does NOT do
//!
//! - Convert any view. `ViewSpec` is a trait; the existing 14 views
//!   still use the legacy `View` trait. Sub-slices B–E convert them.
//! - Replace `PyFrontierRegistry`. Sub-slice G does that.
//! - Wire the Python side. Sub-slice H does that.
//!
//! # Multi-worker
//!
//! For now, the worker count is 1. DD/timely supports N workers with
//! data exchange between them, but multi-worker reasoning is harder to
//! get right and the single-worker path covers everything we need at
//! current scale (millions of events, not billions). Multi-worker is a
//! future tuning concern, not a structural one.
//!
//! TODO(rust-build): the entire module is unverified — there's no
//! Rust toolchain in the environment where this was written. The DD
//! and timely API calls are inferred from the documented surface of
//! `differential-dataflow = "0.12"` and `timely = "0.12"`. Specific
//! places where the API shape is a guess are marked with
//! `TODO(rust-build)`. Build, run the inline tests, fix per-line.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crossbeam_channel::{bounded, unbounded, Receiver, Sender};
use parking_lot::RwLock;

// TODO(rust-build): confirm the exact import paths for DD 0.12. The
// documented surface in docs.rs/differential-dataflow/0.12.0 shows:
//   - differential_dataflow::input::InputSession
//   - differential_dataflow::operators::{Reduce, Join, Threshold}
//   - differential_dataflow::trace::TraceReader
//   - differential_dataflow::AsCollection
//   - timely::dataflow::Scope
//   - timely::worker::Worker
//   - timely::communication::Allocator
// If any of these moved or were renamed in a patch release, the
// imports below need updating. The build error will point exactly at
// the wrong line.
use differential_dataflow::input::InputSession;
use differential_dataflow::operators::arrange::ArrangeByKey;
use differential_dataflow::trace::TraceReader;
use differential_dataflow::Collection;
use timely::dataflow::Scope;
use timely::progress::frontier::AntichainRef;

use crate::log::append::LogEntry;
use crate::LogOffset;

pub mod views;

// =============================================================================
// EventTuple — the unit pushed into the input session
// =============================================================================

/// A single log entry as it flows through the dataflow.
///
/// Stored as `(kind, payload, offset)`. The offset is also used as the
/// timely timestamp (see below), but it's repeated in the data because
/// some operators need to read it after the timestamp dimension has
/// been collapsed by aggregation.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EventTuple {
    pub kind: String,
    pub payload: Vec<u8>,
    pub offset: LogOffset,
}

impl EventTuple {
    pub fn from_log_entry(entry: &LogEntry) -> Self {
        Self {
            kind: entry.kind.clone(),
            payload: entry.payload.clone(),
            offset: entry.offset,
        }
    }
}

// =============================================================================
// Timestamp choice
// =============================================================================

/// The dataflow's logical time is the log offset (`u64`). This is a
/// totally-ordered timestamp, which is the simplest choice and the
/// natural one for an append-only log.
///
/// Multidimensional timestamps (e.g., `(epoch, log_offset)`) would
/// allow more incremental opportunities — e.g., "the same query
/// across multiple historical timestamps" without recomputation. We
/// don't need that yet.
///
/// TODO(rust-build): timely's `Timestamp` trait is auto-implemented
/// for `u64`. Confirm by building.
pub type DataflowTimestamp = LogOffset;

// =============================================================================
// ViewSpec trait — what every DD-backed view implements
// =============================================================================

/// A view's specification: the dataflow operators that turn the input
/// event stream into the view's maintained output.
///
/// Implementors define:
///   - `name()` — stable identifier (matches the legacy `View::name()`)
///   - `subscribed_kinds()` — pre-filter; the dataflow scope filters
///     to these event kinds before invoking `build`
///   - `build(scope, input)` — wire up the dataflow operators, return
///     the arrangement trace for queries
///
/// The output type `Output` is the view's per-key state (e.g.
/// `NodeState` for `current_nodes`). The key type `Key` is what
/// queries are keyed on (e.g. `String` for node_id).
///
/// TODO(rust-build): the trait surface is correct in shape but the
/// associated-type bounds need to match exactly what DD's
/// `arrange_by_key` requires. Specifically, `Key` and `Output` need
/// to be `ExchangeData + Hashable` per the DD docs.
pub trait ViewSpec: Send + Sync + 'static {
    /// Stable view name. Matches `View::name()` for compat with the
    /// legacy trait during the transition.
    fn name(&self) -> &'static str;

    /// Event kinds this view subscribes to. The dataflow filters
    /// before passing to `build`.
    fn subscribed_kinds(&self) -> &[&'static str];

    /// Build the dataflow operators.
    ///
    /// Given the filtered input collection, returns the boxed
    /// arrangement trace that the application can query.
    ///
    /// `scope` is the timely dataflow scope; pass it to operators.
    /// `input` is a `Collection<Scope, EventTuple, isize>`.
    ///
    /// The returned trace must have a `(Key, Output)` shape. The
    /// dataflow handle owns the trace; queries go through it.
    ///
    /// TODO(rust-build): DD's arrangement trait surface is the
    /// trickiest bit to get right without a build. The signature
    /// below uses the boxed-dyn-trait pattern that's idiomatic for
    /// erasing the concrete trace type. If DD's trait bounds reject
    /// this, the alternative is to make `ViewSpec` generic over the
    /// arrangement type, which propagates type parameters everywhere
    /// — uglier but always works.
    fn build<'a, S: Scope<Timestamp = DataflowTimestamp>>(
        &self,
        scope: &mut S,
        input: &Collection<S, EventTuple, isize>,
    ) -> BoxedTrace;
}

/// Erased trace type for a built view's output arrangement.
///
/// In real DD code this would be something like
/// `TraceAgent<Spine<...>>`, parameterized over Key and Value. We
/// erase it here because `ViewSpec` needs to be object-safe, and
/// each view has a different Key/Value pair.
///
/// TODO(rust-build): the actual erased type is non-trivial. This
/// placeholder is `Box<dyn Send + Sync>` which is too loose; queries
/// can't downcast through it. The right approach is probably an
/// enum over the known trace shapes, or a trait object with a
/// `query(&self, key: &dyn Any) -> Option<Box<dyn Any>>` method.
/// Pick on first build.
pub type BoxedTrace = Box<dyn TraceQuery>;

/// Object-safe trace-query interface.
///
/// A view's trace exposes "look up by key" through this. The
/// concrete impl downcasts the bytes-key to its real key type and
/// calls the appropriate trace operation.
///
/// TODO(rust-build): the actual shape of arrangement queries in DD
/// requires going through a `TraceCursor` and reading per-time
/// updates. This is more involved than a simple HashMap-style
/// `get(key)`. For sub-slice A we just declare the trait; the
/// concrete impl lands in sub-slice B with `current_nodes`.
pub trait TraceQuery: Send + Sync {
    /// Look up a key. Returns the serialized value bytes (JSON for
    /// now) if present at the trace's current frontier.
    fn query_bytes(&self, key_bytes: &[u8]) -> Option<Vec<u8>>;

    /// Snapshot the entire trace as a list of `(key_bytes, value_bytes)`.
    /// Used for diagnostics and full-scan queries.
    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)>;

    /// Current frontier of the trace (highest fully-processed offset).
    fn frontier_offset(&self) -> LogOffset;
}

// =============================================================================
// Worker control messages
// =============================================================================

/// Messages sent from the application to the worker thread.
enum WorkerCommand {
    /// Push a new event tuple into the input.
    Feed(EventTuple),
    /// Advance the input's frontier and step the dataflow until the
    /// view frontiers cross `offset`. Reply on the included sender
    /// when done.
    AdvanceTo {
        offset: LogOffset,
        reply: Sender<()>,
    },
    /// Get the current frontier offset for a named view.
    QueryFrontier {
        view_name: String,
        reply: Sender<LogOffset>,
    },
    /// Query a view by serialized key. Returns serialized value or None.
    QueryView {
        view_name: String,
        key_bytes: Vec<u8>,
        reply: Sender<Option<Vec<u8>>>,
    },
    /// Shut down the worker.
    Shutdown,
}

// =============================================================================
// DataflowHandle — application-side view of the worker
// =============================================================================

/// Handle to a running dataflow worker.
///
/// Cloneable — multiple application threads can share one handle.
/// Internally, all operations go through a command channel to the
/// worker thread; the worker is single-threaded.
#[derive(Clone)]
pub struct DataflowHandle {
    cmd_tx: Sender<WorkerCommand>,
    /// Shared map of view name → trace. Populated at worker startup
    /// when views are registered. Read by query operations.
    traces: Arc<RwLock<std::collections::HashMap<String, Arc<dyn TraceQuery>>>>,
    /// Thread handle for clean shutdown.
    worker_thread: Arc<Mutex<Option<thread::JoinHandle<()>>>>,
}

impl DataflowHandle {
    /// Construct and start a new dataflow worker, registering the
    /// given views.
    ///
    /// TODO(rust-build): timely's `worker::Worker::new` takes an
    /// `Allocator`. The `timely::execute` and `timely::execute_directly`
    /// helpers wrap this. For a long-running worker we want
    /// `execute_directly` with an explicit step-loop, OR use
    /// `timely::worker::Worker::step_or_park` in a loop. Confirm
    /// which on first build.
    pub fn start(views: Vec<Box<dyn ViewSpec>>) -> Result<Self, DataflowError> {
        let (cmd_tx, cmd_rx) = unbounded::<WorkerCommand>();
        let traces: Arc<RwLock<std::collections::HashMap<String, Arc<dyn TraceQuery>>>> =
            Arc::new(RwLock::new(std::collections::HashMap::new()));
        let traces_clone = Arc::clone(&traces);

        // Channel for worker readiness — sender signals when the
        // dataflow is built and the trace map is populated.
        let (ready_tx, ready_rx) = bounded::<Result<(), DataflowError>>(1);

        let worker_thread = thread::spawn(move || {
            // TODO(rust-build): the `timely::execute_directly` form
            // we want is roughly:
            //
            //   timely::execute_directly(|worker| {
            //       let mut input = InputSession::new();
            //       let traces = worker.dataflow(|scope| {
            //           let collection = input.to_collection(scope);
            //           // For each ViewSpec, filter and call build()
            //           let mut traces = HashMap::new();
            //           for view in &views {
            //               let kinds = view.subscribed_kinds();
            //               let filtered = collection
            //                   .filter(move |evt| kinds.contains(&evt.kind.as_str()));
            //               let trace = view.build(scope, &filtered);
            //               traces.insert(view.name().to_string(), trace);
            //           }
            //           traces
            //       });
            //       traces_clone.write().extend(...);
            //       ready_tx.send(Ok(())).unwrap();
            //
            //       // Main loop: drain the command channel, push to
            //       // input, advance time, step the worker.
            //       loop {
            //           match cmd_rx.recv_timeout(Duration::from_millis(10)) {
            //               Ok(WorkerCommand::Feed(evt)) => {
            //                   input.insert(evt);
            //               }
            //               Ok(WorkerCommand::AdvanceTo{offset, reply}) => {
            //                   input.advance_to(offset);
            //                   input.flush();
            //                   while !worker_frontier_past(offset) {
            //                       worker.step();
            //                   }
            //                   reply.send(()).ok();
            //               }
            //               Ok(WorkerCommand::Shutdown) => break,
            //               // ... etc
            //           }
            //       }
            //   });
            //
            // The exact shape depends on timely 0.12's API. The block
            // below is a placeholder that exists so the code compiles
            // structurally; the real loop replaces it on first build.

            run_worker_main_loop(views, traces_clone, ready_tx, cmd_rx);
        });

        // Wait for the worker to signal readiness
        match ready_rx.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(())) => {}
            Ok(Err(e)) => return Err(e),
            Err(_) => {
                return Err(DataflowError::Startup(
                    "worker did not signal readiness within 5s".to_string(),
                ))
            }
        }

        Ok(Self {
            cmd_tx,
            traces,
            worker_thread: Arc::new(Mutex::new(Some(worker_thread))),
        })
    }

    /// Push a single log entry into the dataflow.
    ///
    /// Does NOT advance the input's frontier — the entry is buffered
    /// until `advance_to` is called. This matches DD semantics: time
    /// must be explicitly advanced for downstream operators to see
    /// new data.
    pub fn feed(&self, entry: &LogEntry) -> Result<(), DataflowError> {
        let evt = EventTuple::from_log_entry(entry);
        self.cmd_tx
            .send(WorkerCommand::Feed(evt))
            .map_err(|e| DataflowError::ChannelClosed(format!("feed: {e}")))
    }

    /// Push a batch of entries.
    pub fn feed_batch(&self, entries: &[LogEntry]) -> Result<(), DataflowError> {
        for entry in entries {
            self.feed(entry)?;
        }
        Ok(())
    }

    /// Advance the input frontier to `offset` and block until every
    /// registered view has processed up to `offset`.
    ///
    /// This is the operation Python callers wait on when they need
    /// to read a consistent view at a known offset.
    pub fn advance_to(&self, offset: LogOffset) -> Result<(), DataflowError> {
        let (tx, rx) = bounded::<()>(1);
        self.cmd_tx
            .send(WorkerCommand::AdvanceTo { offset, reply: tx })
            .map_err(|e| DataflowError::ChannelClosed(format!("advance_to: {e}")))?;
        rx.recv()
            .map_err(|e| DataflowError::ChannelClosed(format!("advance_to reply: {e}")))?;
        Ok(())
    }

    /// Get the current frontier offset for a named view.
    pub fn frontier_of(&self, view_name: &str) -> Result<LogOffset, DataflowError> {
        let (tx, rx) = bounded::<LogOffset>(1);
        self.cmd_tx
            .send(WorkerCommand::QueryFrontier {
                view_name: view_name.to_string(),
                reply: tx,
            })
            .map_err(|e| DataflowError::ChannelClosed(format!("query_frontier: {e}")))?;
        rx.recv()
            .map_err(|e| DataflowError::ChannelClosed(format!("query_frontier reply: {e}")))
    }

    /// Look up a view by name and key. Returns the value bytes if
    /// present, or None if not.
    ///
    /// `key_bytes` is the serialized key — typically `key.as_bytes()`
    /// for string keys, or a JSON encoding for compound keys. The
    /// view's `TraceQuery` impl knows how to decode it.
    pub fn query(
        &self,
        view_name: &str,
        key_bytes: &[u8],
    ) -> Result<Option<Vec<u8>>, DataflowError> {
        // Fast path: direct trace lookup via the shared map.
        // (Avoids a round-trip through the worker thread.)
        let traces = self.traces.read();
        if let Some(trace) = traces.get(view_name) {
            return Ok(trace.query_bytes(key_bytes));
        }
        Err(DataflowError::ViewNotFound(view_name.to_string()))
    }

    /// Full snapshot of a view's current state. Diagnostic use.
    pub fn snapshot(&self, view_name: &str) -> Result<Vec<(Vec<u8>, Vec<u8>)>, DataflowError> {
        let traces = self.traces.read();
        match traces.get(view_name) {
            Some(trace) => Ok(trace.snapshot_bytes()),
            None => Err(DataflowError::ViewNotFound(view_name.to_string())),
        }
    }

    /// List the names of all registered views.
    pub fn known_views(&self) -> Vec<String> {
        self.traces.read().keys().cloned().collect()
    }

    /// Shut down the worker cleanly.
    pub fn shutdown(&self) -> Result<(), DataflowError> {
        let _ = self.cmd_tx.send(WorkerCommand::Shutdown);
        let mut guard = self.worker_thread.lock().unwrap();
        if let Some(handle) = guard.take() {
            handle
                .join()
                .map_err(|_| DataflowError::Startup("worker panicked".to_string()))?;
        }
        Ok(())
    }
}

impl Drop for DataflowHandle {
    fn drop(&mut self) {
        // Best effort — don't propagate errors from drop.
        let _ = self.cmd_tx.send(WorkerCommand::Shutdown);
        // Don't join here; that's the explicit shutdown path. If the
        // application drops the handle without shutting down, the
        // worker thread becomes detached and the OS reaps it on
        // process exit. Fine for our use case.
    }
}

// =============================================================================
// Worker main loop (placeholder)
// =============================================================================

/// Runs inside the worker thread. Builds the dataflow, registers
/// view traces, then drains the command channel.
///
/// TODO(rust-build): this is the function that `timely::execute_directly`
/// wraps in the real implementation. The body below is a placeholder
/// that compiles structurally but doesn't actually run a dataflow.
/// Replace on first build with the form sketched in the comment in
/// `DataflowHandle::start`.
fn run_worker_main_loop(
    views: Vec<Box<dyn ViewSpec>>,
    traces: Arc<RwLock<std::collections::HashMap<String, Arc<dyn TraceQuery>>>>,
    ready_tx: Sender<Result<(), DataflowError>>,
    cmd_rx: Receiver<WorkerCommand>,
) {
    // For sub-slice A, the worker is a stub that:
    //   1. Acknowledges readiness.
    //   2. Drains commands without doing any DD work.
    //
    // This lets the rest of the wiring (DataflowHandle, channels,
    // traces map) be exercised by tests. Sub-slice B replaces this
    // stub with the real timely::execute_directly call when
    // `current_nodes` becomes the first DD-backed view.

    // Stub trace impls per view name so query/snapshot don't error
    // before any view is real.
    for view in &views {
        traces.write().insert(
            view.name().to_string(),
            Arc::new(StubTrace::new(view.name().to_string())),
        );
    }

    if ready_tx.send(Ok(())).is_err() {
        return; // Application gave up waiting; just exit.
    }

    // Drain command channel until shutdown.
    while let Ok(cmd) = cmd_rx.recv() {
        match cmd {
            WorkerCommand::Feed(_) => {
                // Stub: discard. Real impl: input.insert(evt).
            }
            WorkerCommand::AdvanceTo { offset: _, reply } => {
                // Stub: instantly reply. Real impl: advance input,
                // step the worker until view frontiers cross offset,
                // then reply.
                let _ = reply.send(());
            }
            WorkerCommand::QueryFrontier { view_name, reply } => {
                let traces_guard = traces.read();
                let frontier = traces_guard
                    .get(&view_name)
                    .map(|t| t.frontier_offset())
                    .unwrap_or(0);
                let _ = reply.send(frontier);
            }
            WorkerCommand::QueryView {
                view_name,
                key_bytes,
                reply,
            } => {
                let traces_guard = traces.read();
                let result = traces_guard.get(&view_name).and_then(|t| t.query_bytes(&key_bytes));
                let _ = reply.send(result);
            }
            WorkerCommand::Shutdown => break,
        }
    }
}

/// Placeholder trace impl — exists so the trace map has a value per
/// registered view before sub-slice B wires up the real DD-backed
/// arrangements.
struct StubTrace {
    name: String,
}

impl StubTrace {
    fn new(name: String) -> Self {
        Self { name }
    }
}

impl TraceQuery for StubTrace {
    fn query_bytes(&self, _key_bytes: &[u8]) -> Option<Vec<u8>> {
        None
    }

    fn snapshot_bytes(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        Vec::new()
    }

    fn frontier_offset(&self) -> LogOffset {
        0
    }
}

// =============================================================================
// Errors
// =============================================================================

#[derive(Debug, thiserror::Error)]
pub enum DataflowError {
    #[error("view not found: {0}")]
    ViewNotFound(String),
    #[error("worker startup failed: {0}")]
    Startup(String),
    #[error("worker channel closed: {0}")]
    ChannelClosed(String),
    #[error("dataflow internal error: {0}")]
    Internal(String),
}

// =============================================================================
// Inline tests — exercise the scaffolding (not DD itself yet)
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// A minimal ViewSpec for testing the scaffolding.
    struct DummyView;

    impl ViewSpec for DummyView {
        fn name(&self) -> &'static str {
            "dummy"
        }
        fn subscribed_kinds(&self) -> &[&'static str] {
            &["test_event"]
        }
        fn build<'a, S: Scope<Timestamp = DataflowTimestamp>>(
            &self,
            _scope: &mut S,
            _input: &Collection<S, EventTuple, isize>,
        ) -> BoxedTrace {
            // Sub-slice A: views don't actually build dataflow yet;
            // a stub is registered. Sub-slice B replaces this for
            // current_nodes.
            Box::new(StubTrace::new("dummy".to_string()))
        }
    }

    #[test]
    fn handle_starts_and_lists_known_views() {
        let handle = DataflowHandle::start(vec![Box::new(DummyView)]).unwrap();
        let names = handle.known_views();
        assert!(names.contains(&"dummy".to_string()));
        handle.shutdown().unwrap();
    }

    #[test]
    fn feed_does_not_error_with_stub() {
        let handle = DataflowHandle::start(vec![Box::new(DummyView)]).unwrap();
        let entry = LogEntry {
            offset: 1,
            timestamp_ms: 0,
            kind: "test_event".to_string(),
            payload: b"{}".to_vec(),
        };
        handle.feed(&entry).unwrap();
        handle.shutdown().unwrap();
    }

    #[test]
    fn advance_to_returns_with_stub() {
        let handle = DataflowHandle::start(vec![Box::new(DummyView)]).unwrap();
        // Stub replies immediately; with the real DD impl this blocks
        // until view frontiers actually cross.
        handle.advance_to(10).unwrap();
        handle.shutdown().unwrap();
    }

    #[test]
    fn query_unknown_view_errors() {
        let handle = DataflowHandle::start(vec![Box::new(DummyView)]).unwrap();
        let r = handle.query("does_not_exist", b"key");
        assert!(matches!(r, Err(DataflowError::ViewNotFound(_))));
        handle.shutdown().unwrap();
    }

    #[test]
    fn frontier_starts_at_zero() {
        let handle = DataflowHandle::start(vec![Box::new(DummyView)]).unwrap();
        let f = handle.frontier_of("dummy").unwrap();
        assert_eq!(f, 0);
        handle.shutdown().unwrap();
    }
}
