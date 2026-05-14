//! mempalace_core
//!
//! Append-only event log + DDflow-maintained incremental views.
//!
//! The Rust side of the MemPalace substrate. Python consumers reach this via
//! the PyO3 boundary in `pyo3/bindings.rs`.
//!
//! Module layout:
//!
//! - `log/` — append-only log, snapshot/compaction, replay
//! - `views/` — DDflow view definitions (one per master view)
//! - `pyo3/` — Python boundary
//!
//! Spec ref: Part 2 of mempalace_spec.md

pub mod log;
pub mod views;
pub mod dataflow;
pub mod pyo3 {
    //! PyO3 bindings exposing the Rust API to Python.
    pub mod bindings;
    pub mod types;
}

// Public types re-exported at the crate root for convenience.
pub use log::append::{LogAppender, LogError};
pub use log::replay::LogReplayer;
pub use log::snapshot::SnapshotManager;

/// Crate-wide result type.
pub type Result<T> = std::result::Result<T, LogError>;

/// Log offset — monotonic, assigned at append.
pub type LogOffset = u64;

/// Event kind string (matches Python schema/events.py EVENT_KIND values).
pub type EventKind = String;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crate_compiles() {
        // Sanity test that the crate is buildable. Real tests live in
        // each submodule.
        let _: LogOffset = 0;
    }
}

// PyO3 module exposure. The Python module name will be `mempalace_core`.
#[::pyo3::pymodule]
fn mempalace_core(py: ::pyo3::Python, m: &::pyo3::types::PyModule) -> ::pyo3::PyResult<()> {
    crate::pyo3::bindings::register(py, m)
}
