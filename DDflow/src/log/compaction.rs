//! Compaction policy.
//!
//! Decides *when* snapshots are taken. The actual snapshot write happens via
//! `snapshot::SnapshotManager`. Compaction does not delete events.
//!
//! Three triggers (Part 2.3):
//!   - Time-based
//!   - Size-based (events since last snapshot)
//!   - Event-based (specific event kinds, e.g., schema_induced)

use std::time::{Duration, Instant};

use crate::log::snapshot::{SnapshotManager, SnapshotTrigger};
use crate::LogOffset;

/// Configuration for compaction policy.
#[derive(Clone, Debug)]
pub struct CompactionConfig {
    /// Snapshot every N events since last snapshot.
    pub size_threshold: u64,
    /// Snapshot at most this often even if size threshold is hit.
    pub min_interval: Duration,
    /// Snapshot at least this often regardless of size.
    pub max_interval: Duration,
    /// Event kinds that trigger an immediate snapshot.
    pub event_triggers: Vec<String>,
    /// Prune snapshots older than this many offsets behind head.
    pub prune_older_than_offsets: u64,
}

impl Default for CompactionConfig {
    fn default() -> Self {
        Self {
            size_threshold: 10_000,
            min_interval: Duration::from_secs(60 * 60),    // 1 hour
            max_interval: Duration::from_secs(24 * 3600),  // 24 hours
            event_triggers: vec!["schema_induced".to_string()],
            prune_older_than_offsets: 100_000,
        }
    }
}

/// Tracks compaction state and decides when to snapshot.
pub struct Compactor {
    config: CompactionConfig,
    manager: SnapshotManager,
    last_snapshot_offset: LogOffset,
    last_snapshot_at: Instant,
}

impl Compactor {
    pub fn new(manager: SnapshotManager, config: CompactionConfig) -> Self {
        let last_offset = manager
            .latest()
            .ok()
            .flatten()
            .map(|m| m.log_offset)
            .unwrap_or(0);
        Self {
            config,
            manager,
            last_snapshot_offset: last_offset,
            last_snapshot_at: Instant::now(),
        }
    }

    /// Should we take a snapshot now, given the current head and recent events?
    ///
    /// Called periodically by the multiplexer. Returns Some(trigger) if so.
    pub fn should_snapshot(
        &self,
        current_head: LogOffset,
        recent_event_kinds: &[String],
    ) -> Option<SnapshotTrigger> {
        let elapsed = self.last_snapshot_at.elapsed();

        // Hard cap: max interval exceeded.
        if elapsed >= self.config.max_interval {
            return Some(SnapshotTrigger::Time);
        }

        // Don't snapshot too frequently.
        if elapsed < self.config.min_interval {
            return None;
        }

        // Event-triggered snapshot.
        for kind in recent_event_kinds {
            if self.config.event_triggers.iter().any(|t| t == kind) {
                return Some(SnapshotTrigger::Event(kind.clone()));
            }
        }

        // Size-triggered snapshot.
        let events_since = current_head.saturating_sub(self.last_snapshot_offset);
        if events_since >= self.config.size_threshold {
            return Some(SnapshotTrigger::Size);
        }

        None
    }

    /// Record that a snapshot was taken at the given offset.
    pub fn record_snapshot(&mut self, offset: LogOffset) {
        self.last_snapshot_offset = offset;
        self.last_snapshot_at = Instant::now();
    }

    /// Apply the configured retention policy: prune older snapshots.
    pub fn prune(&self, current_head: LogOffset) -> Result<u32, crate::log::append::LogError> {
        let cutoff = current_head.saturating_sub(self.config.prune_older_than_offsets);
        self.manager.prune_older_than(cutoff)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn fresh_compactor(cfg: CompactionConfig) -> (Compactor, tempfile::TempDir) {
        let dir = tempdir().unwrap();
        let mgr = SnapshotManager::new(dir.path()).unwrap();
        let mut comp = Compactor::new(mgr, cfg);
        // Pretend the last snapshot was a long time ago for testing decisions
        // that depend on min_interval.
        comp.last_snapshot_at = Instant::now() - Duration::from_secs(7200);
        (comp, dir)
    }

    #[test]
    fn size_threshold_triggers() {
        let cfg = CompactionConfig {
            size_threshold: 100,
            min_interval: Duration::from_secs(0),
            ..Default::default()
        };
        let (comp, _dir) = fresh_compactor(cfg);
        assert!(matches!(
            comp.should_snapshot(150, &[]),
            Some(SnapshotTrigger::Size)
        ));
    }

    #[test]
    fn event_trigger_fires() {
        let cfg = CompactionConfig {
            size_threshold: 1_000_000,
            min_interval: Duration::from_secs(0),
            event_triggers: vec!["schema_induced".to_string()],
            ..Default::default()
        };
        let (comp, _dir) = fresh_compactor(cfg);
        let kinds = vec!["node_created".to_string(), "schema_induced".to_string()];
        assert!(matches!(
            comp.should_snapshot(50, &kinds),
            Some(SnapshotTrigger::Event(_))
        ));
    }

    #[test]
    fn min_interval_prevents_too_frequent() {
        let cfg = CompactionConfig {
            size_threshold: 1,
            min_interval: Duration::from_secs(3600),
            ..Default::default()
        };
        let dir = tempdir().unwrap();
        let mgr = SnapshotManager::new(dir.path()).unwrap();
        let comp = Compactor::new(mgr, cfg);
        // Just constructed: last_snapshot_at is now; under min_interval.
        assert!(comp.should_snapshot(1000, &[]).is_none());
    }
}
