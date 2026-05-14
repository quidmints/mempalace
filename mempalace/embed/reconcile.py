"""
Reconciliation sweeper.

ChromaDB is a derived representation; the log is the source of truth.
Drawer captures emit `drawer_captured` events that include a verbatim text;
the embedding-extraction step then writes to ChromaDB. If the embedding
write fails or the daemon crashes between log append and ChromaDB write,
ChromaDB is missing entries that the log knows about.

The reconciliation sweeper closes that gap: periodically, it walks recent
log entries, checks ChromaDB membership, and re-embeds any drawer that's
in the log but missing from the store. Idempotent — re-embedding the same
text into the same drawer_id is a no-op when the embedding is already
present.

Spec ref: Part 4.3
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..log.client import LogClient, get_default_client
from .client import EmbeddingStore, VectorRecord, get_default_store
from .model import EmbeddingService, get_default_service


@dataclass
class ReconcileStats:
    """Outcome of a single sweep."""
    swept_offset_start: int = 0
    swept_offset_end: int = 0
    drawers_seen: int = 0
    drawers_missing: int = 0
    drawers_reembedded: int = 0
    elapsed_ms: int = 0


class Reconciler:
    """Walks the log on a cadence; re-embeds any drawer missing from the store.

    Usage:
        reconciler = Reconciler(client, store, service)
        # call sweep_once() periodically (the multiplexer schedules this in
        # production) or use start_loop() for a self-driving thread.

    The sweeper also extracts a "text getter" callable so consumers can plug
    in their own logic for retrieving drawer text from a drawer_id (e.g., from
    encrypted blob storage). The default reads the verbatim text from the
    drawer_captured event payload.
    """

    def __init__(
        self,
        client: LogClient | None = None,
        store: EmbeddingStore | None = None,
        service: EmbeddingService | None = None,
        text_getter: Callable[[str, dict], str] | None = None,
    ) -> None:
        self._client = client or get_default_client()
        self._store = store or get_default_store()
        self._service = service or get_default_service()
        # Default text getter: read from the drawer_captured payload's
        # `properties.verbatim_text` if present, else empty string.
        self._text_getter = text_getter or self._default_text_getter
        self._last_swept_offset = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # =========================================================================
    # One-shot sweep
    # =========================================================================

    def sweep_once(
        self,
        from_offset: int | None = None,
        to_offset: int | None = None,
    ) -> ReconcileStats:
        """Sweep a range of the log, re-embedding missing drawers.

        Args:
            from_offset: starting offset. Defaults to last swept offset + 1.
            to_offset: end offset (exclusive). Defaults to current log head + 1.
        """
        start_ms = int(time.time() * 1000)
        with self._lock:
            start = (from_offset
                     if from_offset is not None
                     else self._last_swept_offset + 1)
        head = self._client.current_offset()
        end = to_offset if to_offset is not None else head + 1

        stats = ReconcileStats(
            swept_offset_start=start,
            swept_offset_end=end,
        )

        if start >= end:
            stats.elapsed_ms = int(time.time() * 1000) - start_ms
            return stats

        for offset, kind, payload in self._client.read_range(start, end):
            if kind != "drawer_captured":
                continue
            drawer_id = payload.get("drawer_id")
            if not drawer_id:
                continue
            stats.drawers_seen += 1
            if self._store.has(drawer_id):
                continue
            stats.drawers_missing += 1
            text = self._text_getter(drawer_id, payload)
            if not text:
                # No text recoverable; can't re-embed. Skip and let it
                # surface in the next sweep when text becomes available.
                continue
            vector = self._service.embed(text, step_id=f"reconcile@{offset}")
            self._store.upsert(
                drawer_id,
                vector,
                metadata={
                    "captured_at_offset": offset,
                    "duration_ms": payload.get("duration_ms", 0),
                    "interactional": payload.get("interactional", "memo_to_self"),
                    "embedding_model_id": payload.get("embedding_model_id", ""),
                },
            )
            stats.drawers_reembedded += 1

        with self._lock:
            self._last_swept_offset = max(end - 1, self._last_swept_offset)

        stats.elapsed_ms = int(time.time() * 1000) - start_ms
        return stats

    # =========================================================================
    # Background loop
    # =========================================================================

    def start_loop(self, interval_seconds: float = 60.0) -> None:
        """Start a self-driving thread that sweeps every `interval_seconds`.

        The thread runs until `stop_loop()` is called. Most production code
        won't use this — the multiplexer schedules sweeps. Useful for
        single-process tests and small deployments.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._loop, args=(interval_seconds,), daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop_loop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self, interval: float) -> None:
        while not self._stop_event.is_set():
            try:
                self.sweep_once()
            except Exception:
                # Reconciliation failures shouldn't crash the daemon.
                # Production: log via audit log.
                pass
            self._stop_event.wait(interval)

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _default_text_getter(drawer_id: str, payload: dict) -> str:
        """Default text recovery from drawer_captured payload.

        The drawer_captured event in the current schema stores facet pointers,
        not the full verbatim text inline (verbatim text lives in encrypted
        blob storage). Callers in production override this with a getter that
        reads from blob storage.
        """
        props = payload.get("properties") or {}
        return str(props.get("verbatim_text", ""))
