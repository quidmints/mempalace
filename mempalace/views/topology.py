"""
Topology browsing API — Track 6A.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Layer 1 — Topology view (always
available)": the cloud-box exposes a structural view of the DAG that
the phone can browse without decrypting any drawers.

# What's in the topology view

  - All node IDs, kinds, and creation timestamps.
  - All edges and their kinds.
  - Per-drawer metadata (drawer_id, content_hash, duration_ms,
    interactional kind, capture timestamp).
  - Heat / canonical / invalidation flags on nodes.
  - Segment boundaries.

# What's NOT in the topology view

  - Verbatim drawer content (encrypted; only the phone can decrypt).
  - Audio blobs.
  - Decrypted assertion property values.
  - Per-token paralinguistic features.

# Why a class, not a set of free functions

The phone's topology UI paginates — "give me 100 nodes starting at
cursor X." A TopologyBrowser holds a reference to the log + view
store and exposes paginated reads with stable cursor semantics.
Free functions would need to re-resolve the store on every call.

# Endpoint vs library

This module is a Python library. Production wires it through whatever
RPC the daemon shell exposes (TLS + JSON-RPC, gRPC, or similar). The
library returns serializable dicts so the RPC layer is just a
JSON-encode-and-ship.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Layer 1"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    DrawerCaptured,
    DrawerInvalidated,
    DrawerRevalidated,
    EdgeCreated,
    NodeCreated,
    SegmentCreated,
)
from ..schema.kinds import EdgeKind, NodeKind
from . import current as current_views

logger = logging.getLogger(__name__)


# =============================================================================
# Page result types
# =============================================================================


@dataclass
class NodePage:
    """One page of nodes for the phone-side browser."""
    nodes: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    """Pass back as `cursor` to fetch the next page. None when exhausted."""
    total_count: int = 0
    """Best-effort estimate of total nodes matching the filter. Phone
    UI uses for progress bars."""


@dataclass
class EdgePage:
    edges: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    total_count: int = 0


@dataclass
class DrawerPage:
    drawers: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    total_count: int = 0


@dataclass
class SegmentPage:
    segments: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    total_count: int = 0


# =============================================================================
# TopologyBrowser
# =============================================================================


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


class TopologyBrowser:
    """Paginated, plaintext-metadata-only view of the DAG.

    Construction:
      browser = TopologyBrowser()                 # uses default log + views
      browser = TopologyBrowser(log_client=...)   # specific log

    Reads are read-only; nothing here mutates substrate.
    """

    def __init__(self, log_client: LogClient | None = None) -> None:
        self._log = log_client or get_default_client()

    # -------- Nodes ----------------------------------------------------------

    def list_nodes(
        self,
        *,
        node_kind: NodeKind | None = None,
        include_invalidated: bool = False,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> NodePage:
        """List nodes, paginated.

        cursor: opaque string; pass back from the prior page's
                `next_cursor`. None on first call.
        """
        # First, ensure views are caught up to the log
        current_views.tick_views()

        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        store = current_views._get_store()

        with store._lock:
            # Stable iteration order: by creation offset (already
            # tracked on NodeState).
            all_nodes = list(store.nodes.values())
        all_nodes.sort(key=lambda n: n.created_at_offset)

        if node_kind is not None:
            all_nodes = [n for n in all_nodes if n.node_kind == node_kind.value]

        if not include_invalidated:
            all_nodes = [n for n in all_nodes if n.is_active()]

        start_idx = self._cursor_to_index(cursor)
        end_idx = start_idx + page_size
        page = all_nodes[start_idx:end_idx]
        next_cursor = (
            self._index_to_cursor(end_idx) if end_idx < len(all_nodes) else None
        )

        return NodePage(
            nodes=[self._node_to_dict(n) for n in page],
            next_cursor=next_cursor,
            total_count=len(all_nodes),
        )

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get one node by ID. None if not found."""
        current_views.tick_views()
        node = current_views.current_node(node_id)
        if node is None:
            return None
        return self._node_to_dict(node)

    # -------- Edges ----------------------------------------------------------

    def list_edges(
        self,
        *,
        edge_kind: EdgeKind | None = None,
        source_node_id: str | None = None,
        target_node_id: str | None = None,
        include_invalidated: bool = False,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> EdgePage:
        """List edges, paginated. Multiple filter dimensions can combine."""
        current_views.tick_views()
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        store = current_views._get_store()

        with store._lock:
            all_edges = list(store.edges.values())
        all_edges.sort(key=lambda e: e.created_at_offset)

        if edge_kind is not None:
            all_edges = [e for e in all_edges if e.edge_kind == edge_kind.value]
        if source_node_id is not None:
            all_edges = [e for e in all_edges if e.source_node_id == source_node_id]
        if target_node_id is not None:
            all_edges = [e for e in all_edges if e.target_node_id == target_node_id]
        if not include_invalidated:
            all_edges = [e for e in all_edges if e.is_active()]

        start_idx = self._cursor_to_index(cursor)
        end_idx = start_idx + page_size
        page = all_edges[start_idx:end_idx]
        next_cursor = (
            self._index_to_cursor(end_idx) if end_idx < len(all_edges) else None
        )

        return EdgePage(
            edges=[self._edge_to_dict(e) for e in page],
            next_cursor=next_cursor,
            total_count=len(all_edges),
        )

    # -------- Drawers --------------------------------------------------------

    def list_drawers(
        self,
        *,
        include_invalidated: bool = False,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DrawerPage:
        """List drawers, paginated.

        Drawers don't have NodeState entries; they live in the log.
        We scan the log for DrawerCaptured events. For large logs this
        scales; production would maintain a drawer-index view but the
        Track 6A scope just reads from the log directly.
        """
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        drawers = self._scan_drawers()

        if not include_invalidated:
            current_views.tick_views()
            drawers = [
                d for d in drawers
                if not current_views.is_drawer_invalidated(d["drawer_id"])
            ]

        start_idx = self._cursor_to_index(cursor)
        end_idx = start_idx + page_size
        page = drawers[start_idx:end_idx]
        next_cursor = (
            self._index_to_cursor(end_idx) if end_idx < len(drawers) else None
        )

        return DrawerPage(
            drawers=page,
            next_cursor=next_cursor,
            total_count=len(drawers),
        )

    def get_drawer(self, drawer_id: str) -> dict[str, Any] | None:
        """Get one drawer's structural metadata (no plaintext, no
        decryption). None if not found."""
        for d in self._scan_drawers():
            if d["drawer_id"] == drawer_id:
                return d
        return None

    # -------- Segments -------------------------------------------------------

    def list_segments(
        self,
        *,
        drawer_id: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SegmentPage:
        """List drawer segments. Optionally filtered to one drawer."""
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        segments = self._scan_segments(drawer_id_filter=drawer_id)

        start_idx = self._cursor_to_index(cursor)
        end_idx = start_idx + page_size
        page = segments[start_idx:end_idx]
        next_cursor = (
            self._index_to_cursor(end_idx) if end_idx < len(segments) else None
        )

        return SegmentPage(
            segments=page,
            next_cursor=next_cursor,
            total_count=len(segments),
        )

    # -------- Stats ----------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """High-level palette stats for the topology landing page."""
        current_views.tick_views()
        store = current_views._get_store()
        with store._lock:
            node_count = sum(1 for n in store.nodes.values() if n.is_active())
            edge_count = sum(1 for e in store.edges.values() if e.is_active())
            invalidated_node_count = sum(
                1 for n in store.nodes.values() if not n.is_active()
            )
            invalidated_drawer_count = len(store.invalidated_drawers)
        drawer_count = len(self._scan_drawers())
        active_drawer_count = drawer_count - invalidated_drawer_count
        segment_count = len(self._scan_segments())
        return {
            "active_nodes": node_count,
            "invalidated_nodes": invalidated_node_count,
            "active_edges": edge_count,
            "all_drawers": drawer_count,
            "active_drawers": active_drawer_count,
            "invalidated_drawers": invalidated_drawer_count,
            "segments": segment_count,
        }

    # -------- Internals ------------------------------------------------------

    def _scan_drawers(self) -> list[dict[str, Any]]:
        """Scan the log for DrawerCaptured events.

        Idempotent — produces the same result for the same log state.
        Returns drawers in capture order (oldest first).

        Performance note: for production, a drawer-index DD view would
        replace this. Track 6A's scope is to expose the topology view;
        building the drawer-index DD is its own work.
        """
        end = self._log.current_offset() + 1
        rows = list(self._log.read_range(0, end))
        drawers: list[dict[str, Any]] = []
        for offset, kind, payload in rows:
            if kind != DrawerCaptured.EVENT_KIND:
                continue
            drawers.append({
                "drawer_id": payload.get("drawer_id", ""),
                "content_hash": payload.get("content_hash", ""),
                "captured_at_ms": payload.get("capture_recorded_at", 0),
                "duration_ms": payload.get("duration_ms", 0),
                "interactional": payload.get("interactional", ""),
                "self_other_world": payload.get("self_other_world", ""),
                "encryption_schema_version": payload.get(
                    "encryption_schema_version", "v0"
                ),
                "is_encrypted": payload.get(
                    "encryption_schema_version", "v0"
                ) not in ("", "v0"),
                "log_offset": offset,
                # Carry direct_participants etc. — these are structural,
                # plaintext per the encryption boundary
                "direct_participants": payload.get("direct_participants", []),
                "subjects_of_discussion": payload.get(
                    "subjects_of_discussion", []
                ),
                "audience": payload.get("audience", []),
            })
        return drawers

    def _scan_segments(
        self,
        *,
        drawer_id_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Scan the log for SegmentCreated events."""
        end = self._log.current_offset() + 1
        rows = list(self._log.read_range(0, end))
        segments: list[dict[str, Any]] = []
        for offset, kind, payload in rows:
            if kind != SegmentCreated.EVENT_KIND:
                continue
            if (
                drawer_id_filter is not None
                and payload.get("drawer_id") != drawer_id_filter
            ):
                continue
            segments.append({
                "segment_id": payload.get("segment_id", ""),
                "drawer_id": payload.get("drawer_id", ""),
                "start_ms": payload.get("start_ms", 0),
                "end_ms": payload.get("end_ms", 0),
                "created_at_ms": payload.get("created_at_ms", 0),
                "log_offset": offset,
            })
        return segments

    @staticmethod
    def _node_to_dict(node) -> dict[str, Any]:
        """Serialize NodeState for the wire. Includes only structural
        metadata; properties may include semantic content that's
        ciphertext at rest in a future encryption pass — for now,
        node properties stay plaintext per ENCRYPTION_AT_EDGE_DESIGN.md
        v2 (assertion properties get encryption later in Track 5)."""
        return {
            "node_id": node.node_id,
            "node_kind": node.node_kind,
            "canonical": node.canonical,
            "canon_path": node.canon_path,
            "importance": node.importance,
            "created_at_offset": node.created_at_offset,
            "last_modified_at_offset": node.last_modified_at_offset,
            "is_active": node.is_active(),
            "invalidated_at": node.invalidated_at,
            # Properties: leave as-is for now. When NodePropertySet
            # encryption ships (Track 5 extension), the ciphertext
            # form gets surfaced here too.
            "property_keys": list(node.properties.keys()),
        }

    @staticmethod
    def _edge_to_dict(edge) -> dict[str, Any]:
        return {
            "edge_id": edge.edge_id,
            "edge_kind": edge.edge_kind,
            "source_node_id": edge.source_node_id,
            "target_node_id": edge.target_node_id,
            "valid_from": edge.valid_from,
            "valid_to": edge.valid_to,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "derivation": edge.derivation,
            "is_active": edge.is_active(),
            "invalidated_at": edge.invalidated_at,
            "created_at_offset": edge.created_at_offset,
        }

    @staticmethod
    def _cursor_to_index(cursor: str | None) -> int:
        """Cursors are just the integer offset, base-10 string."""
        if cursor is None or cursor == "":
            return 0
        try:
            idx = int(cursor)
            return max(0, idx)
        except ValueError:
            logger.warning("Invalid cursor %r; treating as start", cursor)
            return 0

    @staticmethod
    def _index_to_cursor(idx: int) -> str:
        return str(idx)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DrawerPage",
    "EdgePage",
    "MAX_PAGE_SIZE",
    "NodePage",
    "SegmentPage",
    "TopologyBrowser",
]
