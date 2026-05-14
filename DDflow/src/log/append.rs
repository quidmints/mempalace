//! Append-only log writer.
//!
//! Every change to the palace lands here as a serialized event with an
//! assigned monotonic offset. fsync discipline ensures durability before
//! returning offset to caller.
//!
//! Format on disk (one entry per event, length-prefixed):
//!
//!   [u64 offset][u64 timestamp_ms][u32 kind_len][kind_utf8][u32 payload_len][payload_bincode]
//!
//! The kind is a UTF-8 string (matches Python EVENT_KIND values). The
//! payload is bincode-serialized; we keep it opaque at the Rust layer
//! because Python defines event schemas. The Rust side enforces structural
//! invariants (offset monotonicity, length consistency) but does not parse
//! payloads except where views need to.
//!
//! Spec ref: Part 2.1

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::LogOffset;

#[derive(Error, Debug)]
pub enum LogError {
    #[error("io error: {0}")]
    Io(#[from] io::Error),
    #[error("serialization error: {0}")]
    Bincode(#[from] bincode::Error),
    #[error("log corrupted at offset {offset}: {reason}")]
    Corrupted { offset: u64, reason: String },
    #[error("attempted to append at offset {got}, expected {expected}")]
    OffsetMismatch { got: u64, expected: u64 },
    #[error("validation failed: {0}")]
    Validation(String),
}

/// On-disk entry header. Followed by kind bytes then payload bytes.
#[derive(Serialize, Deserialize, Debug, Clone)]
struct EntryHeader {
    offset: u64,
    timestamp_ms: u64,
    kind_len: u32,
    payload_len: u32,
}

/// One event entry as kept in memory after read.
#[derive(Debug, Clone)]
pub struct LogEntry {
    pub offset: LogOffset,
    pub timestamp_ms: u64,
    pub kind: String,
    pub payload: Vec<u8>,
}

/// The append-only log.
///
/// Opens or creates a file at the given path. Maintains an in-memory index
/// of (offset → file position) for fast random access. Writes are fsync'd
/// before returning offset to the caller; this guarantees durability for
/// crash-safe replay.
pub struct LogAppender {
    inner: Arc<Mutex<LogInner>>,
}

struct LogInner {
    path: PathBuf,
    file: File,
    next_offset: u64,
    /// Index: offset -> byte position in the file. Loaded on open.
    index: Vec<(u64, u64)>,
}

impl LogAppender {
    /// Open or create a log file at the given path.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, LogError> {
        let path = path.as_ref().to_path_buf();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&path)?;

        let mut inner = LogInner {
            path: path.clone(),
            file,
            next_offset: 1,
            index: Vec::new(),
        };
        inner.rebuild_index()?;

        Ok(Self {
            inner: Arc::new(Mutex::new(inner)),
        })
    }

    /// Append a new event. Returns the assigned log offset.
    ///
    /// fsync is performed before returning, so the offset is durable.
    pub fn append(&self, kind: &str, payload: &[u8]) -> Result<LogOffset, LogError> {
        let mut inner = self.inner.lock();
        let offset = inner.next_offset;
        let timestamp_ms = current_timestamp_ms();

        let header = EntryHeader {
            offset,
            timestamp_ms,
            kind_len: kind.len() as u32,
            payload_len: payload.len() as u32,
        };

        // Seek to end and write
        let pos = inner.file.seek(SeekFrom::End(0))?;
        let header_bytes = bincode::serialize(&header)?;

        inner.file.write_all(&header_bytes)?;
        inner.file.write_all(kind.as_bytes())?;
        inner.file.write_all(payload)?;
        inner.file.sync_data()?; // fsync the data; metadata can lag

        inner.index.push((offset, pos));
        inner.next_offset = offset + 1;

        Ok(offset)
    }

    /// Current head offset (the next offset that will be assigned).
    pub fn current_offset(&self) -> LogOffset {
        self.inner.lock().next_offset.saturating_sub(1)
    }

    /// Read entries with offset in [start, end). Returns them in order.
    pub fn read_range(
        &self,
        start: LogOffset,
        end: LogOffset,
    ) -> Result<Vec<LogEntry>, LogError> {
        let mut inner = self.inner.lock();
        let mut out = Vec::new();
        // Linear scan of the index — fine for batches, real impl can binary-search.
        for &(off, pos) in inner.index.iter() {
            if off >= start && off < end {
                let entry = read_entry_at(&mut inner.file, pos)?;
                out.push(entry);
            }
        }
        Ok(out)
    }

    /// Path of the underlying file.
    pub fn path(&self) -> PathBuf {
        self.inner.lock().path.clone()
    }
}

impl LogInner {
    /// Walk the file from the start, rebuilding the offset → position index.
    /// Stops if a corrupt entry is encountered (treats it as end-of-log).
    fn rebuild_index(&mut self) -> Result<(), LogError> {
        self.index.clear();
        self.file.seek(SeekFrom::Start(0))?;

        let header_size = bincode::serialized_size(&EntryHeader {
            offset: 0,
            timestamp_ms: 0,
            kind_len: 0,
            payload_len: 0,
        })? as usize;

        let mut pos: u64 = 0;
        loop {
            let mut header_buf = vec![0u8; header_size];
            match self.file.read_exact(&mut header_buf) {
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => break,
                Err(e) => return Err(e.into()),
            }
            let header: EntryHeader = match bincode::deserialize(&header_buf) {
                Ok(h) => h,
                Err(_) => break, // truncated/garbled tail — treat as end
            };

            let body_size = (header.kind_len + header.payload_len) as i64;
            self.file.seek(SeekFrom::Current(body_size))?;

            self.index.push((header.offset, pos));
            self.next_offset = header.offset + 1;

            pos += header_size as u64 + body_size as u64;
        }

        Ok(())
    }
}

fn read_entry_at(file: &mut File, pos: u64) -> Result<LogEntry, LogError> {
    let header_size = bincode::serialized_size(&EntryHeader {
        offset: 0,
        timestamp_ms: 0,
        kind_len: 0,
        payload_len: 0,
    })? as usize;

    file.seek(SeekFrom::Start(pos))?;
    let mut header_buf = vec![0u8; header_size];
    file.read_exact(&mut header_buf)?;
    let header: EntryHeader = bincode::deserialize(&header_buf)?;

    let mut kind_buf = vec![0u8; header.kind_len as usize];
    file.read_exact(&mut kind_buf)?;
    let kind = String::from_utf8(kind_buf).map_err(|_| LogError::Corrupted {
        offset: header.offset,
        reason: "kind not valid utf8".to_string(),
    })?;

    let mut payload_buf = vec![0u8; header.payload_len as usize];
    file.read_exact(&mut payload_buf)?;

    Ok(LogEntry {
        offset: header.offset,
        timestamp_ms: header.timestamp_ms,
        kind,
        payload: payload_buf,
    })
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
    fn append_and_read_back() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.log");
        let log = LogAppender::open(&path).unwrap();

        let off1 = log.append("drawer_captured", b"payload1").unwrap();
        let off2 = log.append("node_created", b"payload2").unwrap();
        assert_eq!(off1, 1);
        assert_eq!(off2, 2);

        let entries = log.read_range(1, 3).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].kind, "drawer_captured");
        assert_eq!(entries[0].payload, b"payload1");
        assert_eq!(entries[1].kind, "node_created");
    }

    #[test]
    fn reopen_recovers_offset() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.log");
        {
            let log = LogAppender::open(&path).unwrap();
            log.append("a", b"1").unwrap();
            log.append("b", b"2").unwrap();
        }
        let log = LogAppender::open(&path).unwrap();
        assert_eq!(log.current_offset(), 2);
        let off = log.append("c", b"3").unwrap();
        assert_eq!(off, 3);
    }
}
