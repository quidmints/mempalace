"""
Python-facing API for master views.

Wraps the Rust-side PyO3 bindings (`mempalace_core.PyLogClient`) and exposes
Pythonic typed accessors. Consumers in this codebase reach views through this
module, never through PyO3 directly.

The Rust crate may not be built in dev environments; in that case views fall
back to a pure-Python implementation that consumes events from the same
`LogClient` via the SubscriberRegistry. The fallback is functionally
equivalent for moderate scale; the Rust version is what production uses.

Spec ref: Part 2.2 (consumer-side), Part 3
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from ..log.client import LogClient, get_default_client
from ..log.subscriber import SubscriberRegistry, get_default_registry
from ..schema.kinds import EdgeKind, NodeKind


# =============================================================================
# Python fallback view state
#
# When the Rust crate isn't available (dev/test), these classes maintain
# equivalent state by subscribing to the log via the Python subscriber.
# =============================================================================

@dataclass
class NodeState:
    node_id: str
    node_kind: str
    properties: dict[str, Any] = field(default_factory=dict)
    canonical: bool = False
    canon_path: str | None = None
    importance: float = 0.5
    created_at_offset: int = 0
    last_modified_at_offset: int = 0
    invalidated_at: int | None = None
    """Set when a NodeInvalidated event is observed; cleared when a
    NodeRevalidated arrives. Track 6C — user-tier invalidation. Views
    that filter to "active" nodes should check `invalidated_at is None`."""

    def is_active(self) -> bool:
        return self.invalidated_at is None


@dataclass
class EdgeState:
    edge_id: str
    edge_kind: str
    source_node_id: str
    target_node_id: str
    valid_from: int | None = None
    valid_to: int | None = None
    recorded_at: int = 0
    invalidated_at: int | None = None
    weight: float = 1.0
    confidence: float = 1.0
    derivation: str = "OBSERVATION"
    properties: dict[str, Any] = field(default_factory=dict)
    created_at_offset: int = 0

    def is_active(self) -> bool:
        return self.invalidated_at is None

    def is_valid_at(self, world_time_ms: int) -> bool:
        if self.invalidated_at is not None:
            return False
        if self.valid_from is not None and world_time_ms < self.valid_from:
            return False
        if self.valid_to is not None and world_time_ms >= self.valid_to:
            return False
        return True


@dataclass
class InterpretationState:
    node_id: str
    field_name: str
    value: Any
    miner_pass_version: str = ""
    confidence: float = 1.0
    assigned_at_offset: int = 0


# =============================================================================
# View store
#
# In-memory state accumulated by subscribing to the log. Thread-safe.
# =============================================================================

class _ViewStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.nodes: dict[str, NodeState] = {}
        self.edges: dict[str, EdgeState] = {}
        self.interpretations: dict[str, InterpretationState] = {}  # key: "node_id::field"
        self.canonicals: set[str] = set()

        # Adjacency indexes for fast traversal
        self.outgoing: dict[str, list[str]] = defaultdict(list)  # source → [edge_ids]
        self.incoming: dict[str, list[str]] = defaultdict(list)  # target → [edge_ids]

        # Heat field (continuous; periodically decayed)
        self.heat: dict[str, float] = defaultdict(lambda: 0.5)
        self.heat_last_bumped: dict[str, int] = {}

        # Track 6C — drawer-level user invalidation. Drawers don't have
        # NodeState entries (they live in the log + content_hash registry),
        # so we track invalidation here. Maps drawer_id → invalidation
        # event recorded_at. Cleared on revalidation.
        self.invalidated_drawers: dict[str, int] = {}

        # R3 §9.3 — substrate verification. Drawer verbatim text needs
        # to be queryable for the faithfulness scorer. We materialize
        # plaintext drawers (v0 encryption schema) here; encrypted
        # drawers (v2+) stay opaque until phone-decrypted via the
        # secure_read path.
        self.drawer_texts: dict[str, str] = {}

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        with self._lock:
            self._apply_locked(offset, kind, payload)

    def _apply_locked(self, offset: int, kind: str, payload: dict) -> None:
        if kind == "node_created":
            node_id = payload.get("node_id", "")
            if not node_id:
                return
            self.nodes[node_id] = NodeState(
                node_id=node_id,
                node_kind=payload.get("node_kind", ""),
                properties=payload.get("properties") or {},
                canonical=payload.get("canonical", False),
                canon_path=payload.get("canon_path"),
                importance=payload.get("importance", 0.5),
                created_at_offset=offset,
                last_modified_at_offset=offset,
            )
            if payload.get("canonical"):
                self.canonicals.add(node_id)

        elif kind == "node_property_set":
            node_id = payload.get("node_id", "")
            field_name = payload.get("field_name", "")
            value = payload.get("new_value")
            node = self.nodes.get(node_id)
            if node is None:
                return
            node.properties[field_name] = value
            if field_name == "canonical":
                if value is True:
                    node.canonical = True
                    self.canonicals.add(node_id)
                elif value is False:
                    node.canonical = False
                    self.canonicals.discard(node_id)
            elif field_name == "canon_path":
                node.canon_path = value if isinstance(value, str) else None
            elif field_name == "importance":
                if isinstance(value, (int, float)):
                    node.importance = float(value)
            node.last_modified_at_offset = offset

        elif kind == "edge_created":
            edge_id = payload.get("edge_id", "")
            if not edge_id:
                return
            edge = EdgeState(
                edge_id=edge_id,
                edge_kind=payload.get("edge_kind", ""),
                source_node_id=payload.get("source_node_id", ""),
                target_node_id=payload.get("target_node_id", ""),
                valid_from=payload.get("valid_from"),
                valid_to=payload.get("valid_to"),
                recorded_at=payload.get("recorded_at", 0),
                weight=payload.get("weight", 1.0),
                confidence=payload.get("confidence", 1.0),
                derivation=payload.get("derivation", "OBSERVATION"),
                properties=payload.get("properties") or {},
                created_at_offset=offset,
            )
            self.edges[edge_id] = edge
            if edge.source_node_id:
                self.outgoing[edge.source_node_id].append(edge_id)
            if edge.target_node_id:
                self.incoming[edge.target_node_id].append(edge_id)

        elif kind == "edge_invalidated":
            edge_id = payload.get("edge_id", "")
            edge = self.edges.get(edge_id)
            if edge is not None:
                edge.invalidated_at = payload.get("recorded_at") or 0

        elif kind == "edge_revalidated":
            edge_id = payload.get("edge_id", "")
            edge = self.edges.get(edge_id)
            if edge is not None:
                edge.invalidated_at = None

        elif kind == "node_invalidated":
            node_id = payload.get("node_id", "")
            node = self.nodes.get(node_id)
            if node is not None:
                node.invalidated_at = (
                    payload.get("invalidated_at_ms")
                    or payload.get("recorded_at")
                    or 0
                )

        elif kind == "node_revalidated":
            node_id = payload.get("node_id", "")
            node = self.nodes.get(node_id)
            if node is not None:
                node.invalidated_at = None

        elif kind == "drawer_captured":
            # R3 §9.3 — store plaintext (v0) drawer text for substrate
            # verification. Encrypted drawers (v2+) leave the field
            # empty here; production reads via the phone-decrypt path
            # (mempalace.drawer.secure_read).
            drawer_id = payload.get("drawer_id", "")
            schema_version = payload.get("encryption_schema_version", "")
            if drawer_id and schema_version in ("", "v0"):
                vtext = payload.get("verbatim_text", "")
                if vtext:
                    self.drawer_texts[drawer_id] = vtext

        elif kind == "drawer_invalidated":
            drawer_id = payload.get("drawer_id", "")
            if drawer_id:
                self.invalidated_drawers[drawer_id] = (
                    payload.get("invalidated_at_ms")
                    or payload.get("recorded_at")
                    or 0
                )

        elif kind == "drawer_revalidated":
            drawer_id = payload.get("drawer_id", "")
            self.invalidated_drawers.pop(drawer_id, None)

        elif kind == "interpretation_assigned":
            node_id = payload.get("node_id", "")
            field_name = payload.get("field_name", "")
            key = f"{node_id}::{field_name}"
            self.interpretations[key] = InterpretationState(
                node_id=node_id,
                field_name=field_name,
                value=payload.get("new_value"),
                miner_pass_version=payload.get("miner_pass_version", ""),
                confidence=payload.get("confidence", 1.0),
                assigned_at_offset=offset,
            )

    # --- Thread-safe accessors ---
    def get_node(self, node_id: str) -> NodeState | None:
        with self._lock:
            return self.nodes.get(node_id)

    def get_edge(self, edge_id: str) -> EdgeState | None:
        with self._lock:
            return self.edges.get(edge_id)

    def get_interpretation(self, node_id: str, field: str) -> InterpretationState | None:
        with self._lock:
            return self.interpretations.get(f"{node_id}::{field}")

    def outgoing_edges(self, source: str, kind: str | None = None) -> list[EdgeState]:
        with self._lock:
            return [
                self.edges[eid]
                for eid in self.outgoing.get(source, [])
                if eid in self.edges
                and self.edges[eid].is_active()
                and (kind is None or self.edges[eid].edge_kind == kind)
            ]

    def incoming_edges(self, target: str, kind: str | None = None) -> list[EdgeState]:
        with self._lock:
            return [
                self.edges[eid]
                for eid in self.incoming.get(target, [])
                if eid in self.edges
                and self.edges[eid].is_active()
                and (kind is None or self.edges[eid].edge_kind == kind)
            ]

    def all_nodes_of_kind(self, kind: NodeKind) -> list[NodeState]:
        with self._lock:
            return [n for n in self.nodes.values() if n.node_kind == kind.value]

    def canonical_nodes(self) -> list[NodeState]:
        with self._lock:
            return [self.nodes[n] for n in self.canonicals if n in self.nodes]


# =============================================================================
# View accessor singleton
# =============================================================================

_VIEW_STORE: _ViewStore | None = None
_VIEW_LOCK = threading.Lock()


def _get_store(client: LogClient | None = None) -> _ViewStore:
    """Return the process-wide view store, initializing if needed.

    Subscribes to the log via the default subscriber registry. The first
    caller initializes; subsequent callers reuse the same store.
    """
    global _VIEW_STORE
    with _VIEW_LOCK:
        if _VIEW_STORE is not None:
            return _VIEW_STORE
        store = _ViewStore()
        registry = get_default_registry()
        registry.register(
            consumer_id="views.current",
            kinds=[
                "node_created",
                "node_property_set",
                "edge_created",
                "edge_invalidated",
                "edge_revalidated",
                "node_invalidated",
                "node_revalidated",
                "drawer_captured",
                "drawer_invalidated",
                "drawer_revalidated",
                "interpretation_assigned",
            ],
            handler=lambda offset, kind, payload: store.apply(offset, kind, payload),
            max_batch_size=1024,
        )
        _VIEW_STORE = store
        return store


def reset_views() -> None:
    """Reset the view store (test helper)."""
    global _VIEW_STORE
    with _VIEW_LOCK:
        _VIEW_STORE = None


def tick_views() -> int:
    """Advance the view subscriber, returning the number of events processed."""
    _get_store()  # ensure registered
    registry = get_default_registry()
    return registry.tick_one("views.current")


# =============================================================================
# Public typed accessors
# =============================================================================

def current_node(node_id: str) -> NodeState | None:
    """Return the current state of a node, or None if not found."""
    return _get_store().get_node(node_id)


def current_edge(edge_id: str) -> EdgeState | None:
    return _get_store().get_edge(edge_id)


def current_interpretation(node_id: str, field: str) -> InterpretationState | None:
    return _get_store().get_interpretation(node_id, field)


def outgoing_edges(source: str, kind: EdgeKind | None = None) -> list[EdgeState]:
    return _get_store().outgoing_edges(source, kind.value if kind else None)


def incoming_edges(target: str, kind: EdgeKind | None = None) -> list[EdgeState]:
    return _get_store().incoming_edges(target, kind.value if kind else None)


def nodes_of_kind(kind: NodeKind) -> list[NodeState]:
    return _get_store().all_nodes_of_kind(kind)


def canonical_nodes() -> list[NodeState]:
    return _get_store().canonical_nodes()


# =============================================================================
# Track 6C — drawer invalidation accessors
# =============================================================================


def is_drawer_invalidated(drawer_id: str) -> bool:
    """True if the drawer is currently user-invalidated (Tier-1 deletion).

    Reads from the in-memory store; tick_views() may need to be called first
    if events haven't been processed yet.
    """
    store = _get_store()
    with store._lock:
        return drawer_id in store.invalidated_drawers


def drawer_invalidated_at(drawer_id: str) -> int | None:
    """Return the millisecond timestamp the drawer was invalidated, or None
    if it's currently active."""
    store = _get_store()
    with store._lock:
        return store.invalidated_drawers.get(drawer_id)


def drawer_text(drawer_id: str) -> str:
    """Return the verbatim plaintext for a drawer, or empty string if
    not available.

    R3 §9.3 — surface the substrate text for faithfulness scoring.
    Available only for plaintext (v0) drawers; encrypted drawers
    (v2+) return empty here, and the caller must use
    `mempalace.drawer.secure_read` for those.
    """
    store = _get_store()
    with store._lock:
        return store.drawer_texts.get(drawer_id, "")


def invalidated_drawer_ids() -> list[str]:
    """All currently-invalidated drawer IDs.

    Useful for the topology browser's "show invalidated" toggle and for
    the user's "what have I hidden" view."""
    store = _get_store()
    with store._lock:
        return list(store.invalidated_drawers.keys())
