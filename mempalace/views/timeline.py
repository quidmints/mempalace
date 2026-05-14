"""
Bitemporal queries.

Two distinct query stances:

  - World-time: "what's true at world-time T" — filters by edge.valid_from /
    valid_to.
  - System-time: "what did the system believe at system-time T" — filters by
    edge.recorded_at / invalidated_at.

Per R3 §3.3: temporal-overlap is *not* a hard precondition for triangulation
or most retrieval; it's only enforced when stance demands it. This module
provides the operations; consumers decide when to apply them.

Spec ref: Part 3.3
"""

from __future__ import annotations

from . import current as views
from .current import EdgeState


def edges_valid_at(world_time_ms: int) -> list[EdgeState]:
    """Return all edges that were valid in the world at the given time."""
    snap = list(views._get_store().edges.values())  # noqa: SLF001 — internal access for batch
    return [e for e in snap if e.is_valid_at(world_time_ms)]


def edges_believed_at(system_time_ms: int) -> list[EdgeState]:
    """Return all edges the system believed at the given system time.

    An edge is "believed" if recorded_at <= system_time and either
    invalidated_at is None or invalidated_at > system_time.
    """
    snap = list(views._get_store().edges.values())  # noqa: SLF001
    out = []
    for e in snap:
        if e.recorded_at > system_time_ms:
            continue
        if e.invalidated_at is not None and e.invalidated_at <= system_time_ms:
            continue
        out.append(e)
    return out


def assertion_validity_at(
    assertion_id: str, world_time_ms: int
) -> bool:
    """Return True if the assertion is valid at the given world time.

    An assertion is valid at time T if its asserted_subject and
    asserted_object edges are both valid at T (and not invalidated by then).
    """
    subj_edges = views.outgoing_edges(assertion_id, None)
    if not subj_edges:
        return False
    return all(e.is_valid_at(world_time_ms) for e in subj_edges)
