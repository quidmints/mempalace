//! Phase-5 frontier registry (parking_lot-backed).
//!
//! After sub-slice F, the `views` module exists ONLY to host
//! `frontier::FrontierRegistry` — the Phase-5 frontier coordinator
//! that the Python side (`mempalace/log/rust_bridge.py`) writes
//! into. The 14 master views and the legacy `View` trait that used
//! to live here have been replaced by DD-backed `ViewSpec`
//! implementations in `crate::dataflow::views`.
//!
//! # Why this module still exists
//!
//! `FrontierRegistry` is `parking_lot::Mutex`-backed. Sub-slice G
//! replaces it with a registry driven by actual timely capability
//! frontiers from the `DataflowHandle`. Until G lands, the Python
//! side keeps writing into this registry through
//! `PyFrontierRegistry`, and the API stays unchanged.
//!
//! After G, this whole module goes away — frontier reads will route
//! through `DataflowHandle::frontier_of(view_name)` directly.

pub mod frontier;

pub use frontier::{FrontierRegistry, FrontierSnapshot, FrontierTracker};
