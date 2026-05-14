//! Log module.
//!
//! The append-only event log: append, replay, snapshot, compaction.
//! Backed by a memory-mapped file with fsync discipline.
//!
//! Spec ref: Part 2.1, 2.3

pub mod append;
pub mod replay;
pub mod snapshot;
pub mod compaction;
