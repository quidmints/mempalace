//! Snapshot / checkpoint management.
//!
//! Replaying the entire log on startup gets prohibitive at scale. Snapshots
//! checkpoint materialized view state at a log offset; new replays start
//! from snapshot + tail-replay.
//!
//! Snapshot policy (Part 2.3):
//! - Time-based: daily snapshot at low-activity hour
//! - Size-based: when log has grown N events since last snapshot
//! - Event-based: after major schema-induction passes
//!
//! Snapshots do NOT delete events. The full log is preserved for audit,
//! time-travel, bitemporal correction.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::log::append::LogError;
use crate::LogOffset;

/// Metadata for a snapshot. The actual view-state payload is stored alongside;
/// this struct is the manifest.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SnapshotManifest {
    pub log_offset: LogOffset,
    pub created_at_ms: u64,
    pub view_count: u32,
    pub total_size_bytes: u64,
    pub trigger: SnapshotTrigger,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub enum SnapshotTrigger {
    Time,
    Size,
    Event(String),    // event kind that triggered (e.g., "schema_induced")
    Manual,
}

/// Manages snapshot lifecycle: creation, retrieval, pruning.
///
/// Snapshots are stored as files under `dir/`:
///   - `dir/manifest_<offset>.json`  — SnapshotManifest
///   - `dir/views_<offset>.bin`      — view-state payload (bincode)
///
/// Restoration: load latest manifest, load corresponding view payload,
/// replay log from manifest.log_offset+1 to head.
pub struct SnapshotManager {
    dir: PathBuf,
}

impl SnapshotManager {
    pub fn new(dir: impl AsRef<Path>) -> Result<Self, LogError> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir)?;
        Ok(Self { dir })
    }

    /// Create a new snapshot at the given offset with the provided view payload.
    pub fn create(
        &self,
        offset: LogOffset,
        view_payload: &[u8],
        trigger: SnapshotTrigger,
        view_count: u32,
    ) -> Result<SnapshotManifest, LogError> {
        let manifest = SnapshotManifest {
            log_offset: offset,
            created_at_ms: current_timestamp_ms(),
            view_count,
            total_size_bytes: view_payload.len() as u64,
            trigger,
        };

        let payload_path = self.dir.join(format!("views_{offset}.bin"));
        let manifest_path = self.dir.join(format!("manifest_{offset}.json"));

        // Write payload first, then manifest. If we crash between these two
        // writes, the manifest is missing and the payload is orphaned —
        // pruning will remove it on next startup.
        let mut payload_file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&payload_path)?;
        payload_file.write_all(view_payload)?;
        payload_file.sync_all()?;

        let manifest_json = serde_json::to_vec_pretty(&manifest).map_err(|e| {
            LogError::Corrupted {
                offset,
                reason: format!("manifest serialize failed: {e}"),
            }
        })?;
        let mut manifest_file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&manifest_path)?;
        manifest_file.write_all(&manifest_json)?;
        manifest_file.sync_all()?;

        Ok(manifest)
    }

    /// Find the latest snapshot manifest, if any.
    pub fn latest(&self) -> Result<Option<SnapshotManifest>, LogError> {
        let mut best: Option<SnapshotManifest> = None;

        for entry in std::fs::read_dir(&self.dir)? {
            let entry = entry?;
            let name = entry.file_name();
            let name_str = match name.to_str() {
                Some(s) => s,
                None => continue,
            };
            if !name_str.starts_with("manifest_") || !name_str.ends_with(".json") {
                continue;
            }
            let mut file = File::open(entry.path())?;
            let mut buf = Vec::new();
            file.read_to_end(&mut buf)?;
            let manifest: SnapshotManifest = match serde_json::from_slice(&buf) {
                Ok(m) => m,
                Err(_) => continue, // corrupted manifest; skip
            };
            match &best {
                None => best = Some(manifest),
                Some(b) if manifest.log_offset > b.log_offset => {
                    best = Some(manifest);
                }
                _ => {}
            }
        }
        Ok(best)
    }

    /// Load the view payload for a snapshot.
    pub fn load_payload(&self, offset: LogOffset) -> Result<Vec<u8>, LogError> {
        let path = self.dir.join(format!("views_{offset}.bin"));
        let mut file = File::open(path)?;
        let mut buf = Vec::new();
        file.read_to_end(&mut buf)?;
        Ok(buf)
    }

    /// Prune snapshots older than the given offset.
    ///
    /// Keeps the most recent snapshot regardless of cutoff; only intermediate
    /// snapshots are removed. Useful for keeping disk usage bounded.
    pub fn prune_older_than(&self, cutoff_offset: LogOffset) -> Result<u32, LogError> {
        let mut removed = 0;
        let mut latest_offset = 0;
        for entry in std::fs::read_dir(&self.dir)? {
            let entry = entry?;
            if let Some(off) = parse_offset_from_filename(&entry.file_name()) {
                if off > latest_offset {
                    latest_offset = off;
                }
            }
        }
        for entry in std::fs::read_dir(&self.dir)? {
            let entry = entry?;
            if let Some(off) = parse_offset_from_filename(&entry.file_name()) {
                if off < cutoff_offset && off != latest_offset {
                    std::fs::remove_file(entry.path())?;
                    removed += 1;
                }
            }
        }
        Ok(removed)
    }
}

fn parse_offset_from_filename(name: &std::ffi::OsStr) -> Option<LogOffset> {
    let s = name.to_str()?;
    let stripped = s
        .strip_prefix("manifest_")
        .or_else(|| s.strip_prefix("views_"))?;
    let offset_str = stripped.trim_end_matches(".json").trim_end_matches(".bin");
    offset_str.parse().ok()
}

fn current_timestamp_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn create_and_retrieve_snapshot() {
        let dir = tempdir().unwrap();
        let mgr = SnapshotManager::new(dir.path()).unwrap();
        let manifest = mgr
            .create(100, b"view-state-bytes", SnapshotTrigger::Manual, 5)
            .unwrap();
        assert_eq!(manifest.log_offset, 100);

        let latest = mgr.latest().unwrap().unwrap();
        assert_eq!(latest.log_offset, 100);
        assert_eq!(latest.view_count, 5);

        let payload = mgr.load_payload(100).unwrap();
        assert_eq!(payload, b"view-state-bytes");
    }

    #[test]
    fn latest_picks_highest_offset() {
        let dir = tempdir().unwrap();
        let mgr = SnapshotManager::new(dir.path()).unwrap();
        mgr.create(50, b"a", SnapshotTrigger::Time, 1).unwrap();
        mgr.create(100, b"b", SnapshotTrigger::Time, 1).unwrap();
        mgr.create(75, b"c", SnapshotTrigger::Time, 1).unwrap();
        assert_eq!(mgr.latest().unwrap().unwrap().log_offset, 100);
    }
}
