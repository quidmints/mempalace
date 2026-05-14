"""
Chunked export — Track 6F.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Layer 3 — Full plaintext export":

  "Show me everything" streams the log to the phone in chunks. Each
  chunk is a batch of `(ciphertext, dek_handle, attestation_sig,
  content_hash, metadata)` envelopes per drawer. The phone decrypts
  each chunk locally via PhoneSecureElement; the cloud-box never
  sees plaintext during export.

  Defaults (configurable):
    - Chunk size: 1000 drawers per chunk OR ~50 MB of plaintext-
      equivalent data, whichever smaller.
    - Pause/resume supported via cursor.
    - Abort: client closes the iterator; partial export = whatever
      the phone cached.
    - Time estimate: 100k drawers takes minutes-to-tens of minutes.

# What this module ships

A cloud-box-side streaming endpoint:

  - `ChunkedExporter` — stateful iterator. Holds a cursor and emits
    `ExportChunk` objects via `next_chunk()`.
  - `ExportChunk` — one chunk's worth of envelopes plus cursor state.
  - `start_export(...)` — convenience entry point.

# What this module does NOT ship

  - HTTP transport. Production wraps this in a streaming endpoint
    (e.g. SSE / chunked-transfer or a phone-pull RPC). The shape
    here is iterator-based; the transport adapts.
  - Phone-side decryption / rendering. That's the phone client.
  - On-disk persistence of the cursor. Tests + simple production
    can hold the cursor in process state. Robust production
    persists per-export so the phone can resume after
    cloud-box restarts.

# Why a separate module from phone_decrypt.py

`phone_decrypt.py` is the per-drawer fetch endpoint (Layer 2 view).
Chunked export (Layer 3) builds on the same DrawerCiphertextEnvelope
shape but adds:
  - Cursor-based iteration over the log (not a single fetch).
  - Chunk size accounting (drawer count + bytes).
  - Pause/resume/abort semantics.

Keeping them separate means the per-drawer endpoint stays simple
(it doesn't need to know about cursors) and the export endpoint
can evolve its chunking heuristics independently.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Layer 3 — Full plaintext
export", IMPLEMENTATION_ROADMAP.md §"Track 6F".
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Iterator

from ..log.client import LogClient, get_default_client
from ..secure.burn import IntegrityLockoutGate, get_default_gate
from .phone_decrypt import (
    DrawerCiphertextEnvelope,
    DrawerInvalidatedError,
    DrawerNotEncryptedError,
    DrawerNotFoundError,
    PhoneDecryptEndpoint,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Defaults
# =============================================================================


DEFAULT_CHUNK_DRAWER_COUNT = 1000
"""Per design: 1000 drawers per chunk, or ~50 MB, whichever smaller."""

DEFAULT_CHUNK_BYTE_BUDGET = 50 * 1024 * 1024
"""50 MB approximate ciphertext budget per chunk. The exporter
sums envelope ciphertext sizes; once over budget, closes the chunk."""


# =============================================================================
# Cursor + chunk shapes
# =============================================================================


@dataclass(frozen=True)
class ExportCursor:
    """Resume token for chunked export.

    The cursor is opaque to the phone — it ships back the cursor it
    received, asks for the next chunk. The cloud-box uses the cursor
    to find where to continue.
    """

    last_drawer_offset: int = 0
    """Log offset of the last DrawerCaptured event included in the
    previous chunk. Next chunk starts AFTER this offset."""

    drawers_emitted: int = 0
    """Total drawers shipped so far across all chunks for this
    export. For progress reporting."""

    bytes_emitted: int = 0
    """Total ciphertext bytes shipped so far."""


@dataclass
class ExportChunk:
    """One chunk of an export. Returned by `ChunkedExporter.next_chunk()`."""

    envelopes: list[DrawerCiphertextEnvelope] = field(default_factory=list)
    cursor: ExportCursor = field(default_factory=ExportCursor)
    """Where the export is now. Ship back to get the next chunk."""

    is_final: bool = False
    """True if there are no more chunks. Phone uses this to stop
    asking. After is_final=True, calling next_chunk() returns an
    empty chunk with is_final=True (idempotent)."""

    skipped_drawers: int = 0
    """How many drawers in this chunk's offset range were skipped
    (invalidated, not encrypted, etc.). For diagnostics; the phone
    typically ignores this."""


# =============================================================================
# ChunkedExporter
# =============================================================================


@dataclass
class ChunkedExporter:
    """Cloud-box-side streaming export.

    Construction:
      exp = ChunkedExporter(log_client=...)
      exp = ChunkedExporter()   # uses default log

    Usage (one-shot):
      for chunk in exp.iter_chunks():
          send_to_phone(chunk)

    Usage (resumable):
      cursor = ExportCursor()
      while True:
          chunk = exp.next_chunk(cursor)
          send_to_phone(chunk)
          if chunk.is_final:
              break
          cursor = chunk.cursor

    The exporter is stateless across calls — all state lives in the
    cursor. Two phones can run two concurrent exports without
    interference.
    """

    log_client: LogClient | None = None
    chunk_drawer_count: int = DEFAULT_CHUNK_DRAWER_COUNT
    chunk_byte_budget: int = DEFAULT_CHUNK_BYTE_BUDGET
    integrity_gate: IntegrityLockoutGate | None = None
    """Optional gate. If provided, every chunk request checks the
    gate first; raises LockoutError if tripped (mid-export burn)."""

    skip_invalidated: bool = True
    """True (default): silently skip invalidated drawers from the
    export. False: include them (phone can still render with a
    "[invalidated]" marker)."""

    skip_unencrypted: bool = True
    """True (default): silently skip v0 unencrypted drawers. False:
    raise — most callers expect encrypted-only export."""

    def __post_init__(self) -> None:
        if self.log_client is None:
            self.log_client = get_default_client()

    def next_chunk(
        self,
        cursor: ExportCursor | None = None,
    ) -> ExportChunk:
        """Return the next chunk after the cursor.

        Cursor is None on first call; subsequent calls pass the
        cursor from the previous chunk.
        """
        if self.integrity_gate is not None:
            self.integrity_gate.check()

        cursor = cursor or ExportCursor()
        endpoint = PhoneDecryptEndpoint(log_client=self.log_client)

        # Find drawer-captured events after the cursor's offset
        log_end = self.log_client.current_offset()
        envelopes: list[DrawerCiphertextEnvelope] = []
        skipped = 0
        bytes_in_chunk = 0
        last_offset = cursor.last_drawer_offset

        for offset, kind, payload in self.log_client.read_range(
            cursor.last_drawer_offset + 1, log_end + 1,
        ):
            if kind != "drawer_captured":
                continue
            drawer_id = payload.get("drawer_id", "")
            if not drawer_id:
                continue
            last_offset = offset

            # Skip tombstoned drawers (Track 6D erased) — their event
            # is in the log but ciphertext is gone.
            if payload.get("_erased"):
                skipped += 1
                continue

            try:
                env = endpoint.fetch_drawer_ciphertext(
                    drawer_id,
                    allow_invalidated=not self.skip_invalidated,
                )
            except DrawerInvalidatedError:
                if self.skip_invalidated:
                    skipped += 1
                    continue
                raise
            except DrawerNotEncryptedError:
                if self.skip_unencrypted:
                    skipped += 1
                    continue
                raise
            except DrawerNotFoundError:
                # Tombstoned via Track 6D; skip
                skipped += 1
                continue

            envelopes.append(env)
            bytes_in_chunk += len(env.ciphertext)

            # Chunk size limits
            if (
                len(envelopes) >= self.chunk_drawer_count
                or bytes_in_chunk >= self.chunk_byte_budget
            ):
                break

        # is_final: we made it to (or past) the end of the log without
        # filling a chunk
        is_final = last_offset >= log_end

        new_cursor = ExportCursor(
            last_drawer_offset=last_offset,
            drawers_emitted=cursor.drawers_emitted + len(envelopes),
            bytes_emitted=cursor.bytes_emitted + bytes_in_chunk,
        )

        return ExportChunk(
            envelopes=envelopes,
            cursor=new_cursor,
            is_final=is_final,
            skipped_drawers=skipped,
        )

    def iter_chunks(
        self,
        starting_cursor: ExportCursor | None = None,
    ) -> Iterator[ExportChunk]:
        """One-shot iteration. Yields chunks until is_final.

        Production code that wants pause/resume should use
        `next_chunk()` directly with cursor management.
        """
        cursor = starting_cursor or ExportCursor()
        while True:
            chunk = self.next_chunk(cursor)
            yield chunk
            if chunk.is_final:
                return
            cursor = chunk.cursor


# =============================================================================
# Convenience: estimate export size
# =============================================================================


@dataclass
class ExportEstimate:
    """Rough estimate of an export's size before starting it.

    Useful for the phone to display "this will take ~N minutes /
    will transfer ~N MB" before the user confirms.
    """

    drawer_count: int
    estimated_bytes: int
    """Approximate ciphertext bytes the cloud-box would ship."""

    estimated_chunks: int
    """At default chunk size."""


def estimate_export(
    *,
    log_client: LogClient | None = None,
    chunk_drawer_count: int = DEFAULT_CHUNK_DRAWER_COUNT,
) -> ExportEstimate:
    """Walk the log counting drawer events; sum their ciphertext sizes.

    Cheap; doesn't actually decrypt or stream anything.
    """
    log = log_client or get_default_client()
    drawer_count = 0
    total_bytes = 0

    for _o, kind, payload in log.read_range(0, log.current_offset() + 1):
        if kind != "drawer_captured":
            continue
        if payload.get("_erased"):
            continue
        drawer_count += 1
        ct = payload.get("verbatim_ciphertext")
        if isinstance(ct, bytes):
            total_bytes += len(ct)
        elif isinstance(payload.get("verbatim_text"), str):
            # v0 plaintext path — approximate ciphertext size as
            # plaintext + overhead
            total_bytes += len(payload["verbatim_text"]) + 64

    estimated_chunks = max(
        1,
        (drawer_count + chunk_drawer_count - 1) // chunk_drawer_count,
    )

    return ExportEstimate(
        drawer_count=drawer_count,
        estimated_bytes=total_bytes,
        estimated_chunks=estimated_chunks,
    )


__all__ = [
    "DEFAULT_CHUNK_BYTE_BUDGET",
    "DEFAULT_CHUNK_DRAWER_COUNT",
    "ChunkedExporter",
    "ExportChunk",
    "ExportCursor",
    "ExportEstimate",
    "estimate_export",
]
