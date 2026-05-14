//! Log replay.
//!
//! Replays events from a starting offset. Used by:
//! - View reconstruction at startup (replay from last snapshot offset).
//! - Backpressure recovery (a consumer that fell behind catches up).
//! - Time-travel queries (replay up to a target offset).
//!
//! Spec ref: Part 2.1, 2.3

use crate::log::append::{LogAppender, LogEntry, LogError};
use crate::LogOffset;

/// Replay events from a starting offset, calling the handler for each.
///
/// The handler can stop replay early by returning false. Any error in the
/// handler is propagated.
pub struct LogReplayer<'a> {
    appender: &'a LogAppender,
    batch_size: usize,
}

impl<'a> LogReplayer<'a> {
    pub fn new(appender: &'a LogAppender) -> Self {
        Self {
            appender,
            batch_size: 1024,
        }
    }

    pub fn with_batch_size(mut self, size: usize) -> Self {
        self.batch_size = size.max(1);
        self
    }

    /// Replay from `start_offset` to `end_offset` (exclusive).
    /// Calls handler for each entry. Returns the count of entries replayed.
    pub fn replay<F>(
        &self,
        start_offset: LogOffset,
        end_offset: LogOffset,
        mut handler: F,
    ) -> Result<u64, LogError>
    where
        F: FnMut(&LogEntry) -> Result<bool, LogError>,
    {
        let mut count: u64 = 0;
        let mut cur = start_offset;
        while cur < end_offset {
            let next = (cur + self.batch_size as u64).min(end_offset);
            let batch = self.appender.read_range(cur, next)?;
            for entry in &batch {
                let keep_going = handler(entry)?;
                count += 1;
                if !keep_going {
                    return Ok(count);
                }
            }
            cur = next;
        }
        Ok(count)
    }

    /// Replay from `start_offset` to current head.
    pub fn replay_to_head<F>(
        &self,
        start_offset: LogOffset,
        handler: F,
    ) -> Result<u64, LogError>
    where
        F: FnMut(&LogEntry) -> Result<bool, LogError>,
    {
        let head = self.appender.current_offset();
        self.replay(start_offset, head + 1, handler)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn replay_walks_in_order() {
        let dir = tempdir().unwrap();
        let log = LogAppender::open(dir.path().join("test.log")).unwrap();
        log.append("a", b"1").unwrap();
        log.append("b", b"2").unwrap();
        log.append("c", b"3").unwrap();

        let replayer = LogReplayer::new(&log);
        let mut kinds = Vec::new();
        let count = replayer
            .replay_to_head(1, |entry| {
                kinds.push(entry.kind.clone());
                Ok(true)
            })
            .unwrap();
        assert_eq!(count, 3);
        assert_eq!(kinds, vec!["a", "b", "c"]);
    }

    #[test]
    fn replay_handler_can_stop_early() {
        let dir = tempdir().unwrap();
        let log = LogAppender::open(dir.path().join("test.log")).unwrap();
        for i in 0..5 {
            log.append(&format!("k{i}"), b"x").unwrap();
        }
        let replayer = LogReplayer::new(&log);
        let mut count = 0;
        replayer
            .replay_to_head(1, |_entry| {
                count += 1;
                Ok(count < 2)
            })
            .unwrap();
        assert_eq!(count, 2);
    }
}
