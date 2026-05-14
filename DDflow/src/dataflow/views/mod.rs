//! DD-backed view implementations.
//!
//! Each view in this module implements the `ViewSpec` trait from the
//! parent `dataflow` module. Sub-slices B–E populate this directory:
//!
//! - Sub-slice B: `current_nodes`
//! - Sub-slice C: `current_edges`, `current_interpretations`
//! - Sub-slice D: `heat_field`, `velocity_field`, `recurrence_clusters`,
//!   `active_periods`, `active_iams`, `canon_set`
//! - Sub-slice E: `open_contradictions`, `pending_review`, `match_cache`,
//!   `matched_against`, `current_schemas`
//!
//! Sub-slice F deletes the legacy `views/` directory once all views
//! have moved here.

pub mod current_nodes;
pub mod current_edges;
pub mod current_interpretations;
pub mod canon_set;
pub mod active_periods;
pub mod active_iams;
pub mod recurrence_clusters;
pub mod heat_field;
pub mod velocity_field;
pub mod current_schemas;
pub mod open_contradictions;
pub mod pending_review;
pub mod match_cache;
pub mod matched_against;
