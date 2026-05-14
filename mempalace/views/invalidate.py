"""
Invalidation API — Track 6C.

Per USER_VIEW_AND_DELETE_DESIGN.md §"Two-tier deletion / Tier 1 —
Invalidate (cheap, reversible)": user-emitted events that hide content
from retrieval without removing ciphertext from the log.

# What this module exposes

  - `invalidate_drawer(drawer_id, *, reason=None)` — emit a
    `drawer_invalidated` event.
  - `revalidate_drawer(drawer_id)` — emit `drawer_revalidated`.
  - `invalidate_node(node_id, *, reason=None)` — emit `node_invalidated`.
  - `revalidate_node(node_id)` — emit `node_revalidated`.
  - `invalidate_edge(edge_id, *, reason=None)` — emit `edge_invalidated`
    (the existing event kind, exposed here for symmetry with the
    user-tier API).
  - `revalidate_edge(edge_id)` — emit `edge_revalidated`.

All take an optional `log_client` for testability; default is the
process-wide log.

# What this module does NOT do

  - Decide whether the user is authorized to invalidate. The phone-side
    UI is the authentication boundary; by the time the call lands here,
    the daemon already verified the request came from the enrolled
    phone (TLS + heartbeat). This module just emits events.
  - Cascade. Invalidating a node does NOT invalidate its edges
    automatically — per design, edges remain visible in topology
    "flagged invalidated-via-node." If the user wants explicit
    cascading, they invalidate edges separately.
  - Erase ciphertext. That's Tier-2 (`request_erase`); shipped in
    Track 6D.

# Why functions, not a class

Most callers invalidate one or two things. A class would be ceremony
without payoff. The shape mirrors the existing `mempalace.drawer.capture`
function-style API.

Spec ref: USER_VIEW_AND_DELETE_DESIGN.md §"Tier 1 — Invalidate"
"""

from __future__ import annotations

import logging
import time

from ..log.client import LogClient, get_default_client
from ..schema.events import (
    DrawerInvalidated,
    DrawerRevalidated,
    EdgeInvalidated,
    EdgeRevalidated,
    NodeInvalidated,
    NodeRevalidated,
)
from ..schema.identifiers import make_event_id_log

logger = logging.getLogger(__name__)


# =============================================================================
# Drawer
# =============================================================================


def invalidate_drawer(
    drawer_id: str,
    *,
    reason: str | None = None,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    """Invalidate a drawer (Tier-1 deletion).

    Returns: the event_id of the appended event.

    Idempotent at the substrate level: invalidating an already-invalidated
    drawer appends another event but the view sees no state change.
    Callers who want strict idempotency check `is_drawer_invalidated()`
    first.
    """
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = DrawerInvalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        drawer_id=drawer_id,
        invalidated_by_user=True,
        reason=reason,
        invalidated_at_ms=now_ms,
    )
    result = log.append(evt)
    if not result.accepted:
        logger.warning(
            "invalidate_drawer rejected: %s", result.validation
        )
    return evt.event_id


def revalidate_drawer(
    drawer_id: str,
    *,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    """Revalidate a previously-invalidated drawer.

    Idempotent: revalidating an already-valid drawer is a no-op at the
    view level. The event still appends (audit trail).
    """
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = DrawerRevalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        drawer_id=drawer_id,
        revalidated_at_ms=now_ms,
    )
    log.append(evt)
    return evt.event_id


# =============================================================================
# Node
# =============================================================================


def invalidate_node(
    node_id: str,
    *,
    reason: str | None = None,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    """Invalidate a node. Edges incident to the node remain visible
    in topology view but flagged as invalidated-via-node when their
    source/target is checked.
    """
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = NodeInvalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        node_id=node_id,
        invalidated_by_user=True,
        reason=reason,
        invalidated_at_ms=now_ms,
    )
    log.append(evt)
    return evt.event_id


def revalidate_node(
    node_id: str,
    *,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = NodeRevalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        node_id=node_id,
        revalidated_at_ms=now_ms,
    )
    log.append(evt)
    return evt.event_id


# =============================================================================
# Edge
# =============================================================================


def invalidate_edge(
    edge_id: str,
    *,
    reason: str | None = None,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    """Invalidate an edge.

    The substrate already had `edge_invalidated`, but this function
    surfaces the user-tier path so callers get the same shape as
    drawer/node invalidation.
    """
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = EdgeInvalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        edge_id=edge_id,
        reason=reason,
    )
    log.append(evt)
    return evt.event_id


def revalidate_edge(
    edge_id: str,
    *,
    actor: str = "user",
    log_client: LogClient | None = None,
) -> str:
    log = log_client or get_default_client()
    now_ms = int(time.time() * 1000)
    evt = EdgeRevalidated(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        edge_id=edge_id,
        revalidated_at_ms=now_ms,
    )
    log.append(evt)
    return evt.event_id


__all__ = [
    "invalidate_drawer",
    "invalidate_edge",
    "invalidate_node",
    "revalidate_drawer",
    "revalidate_edge",
    "revalidate_node",
]
