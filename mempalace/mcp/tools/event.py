"""
Event-assertion MCP tool.

Per Part 11.1: `mempalace_event_assert` creates an event node within
a period. Used when an agent (or the user via agent) needs to mark a
discrete occurrence.

Spec ref: Part 11.1.
"""

from __future__ import annotations

import time
from typing import Any

from ...views.graph import get_default_graph


def _event_assert(params: dict[str, Any]) -> dict[str, Any]:
    period_id = params.get("period_id", "")
    name = params.get("name", "")
    if not (period_id and name):
        return {"asserted": False, "error": "period_id and name required"}

    occurred = params.get("occurred_at_ms") or int(time.time() * 1000)
    graph = get_default_graph()
    try:
        event_id = graph.create_event(
            period_id=period_id,
            name=name,
            gist=str(params.get("gist", "")),
            occurred_at_ms=int(occurred),
            occurred_to_ms=params.get("occurred_to_ms"),
            importance=float(params.get("importance", 0.5)),
        )
    except Exception as e:  # noqa: BLE001
        return {"asserted": False, "error": f"{type(e).__name__}: {e}"}

    # Optionally link drawers
    drawer_ids = list(params.get("drawer_ids", []) or [])
    edges_added: list[str] = []
    for did in drawer_ids:
        try:
            edge_id = graph.add_drawer_to_event(event_id, did)
            edges_added.append(edge_id)
        except Exception:  # noqa: BLE001
            pass

    return {
        "asserted": True,
        "event_id": event_id,
        "drawer_edges": edges_added,
    }


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_event_assert",
        description=(
            "Assert an event within a period. Optionally link existing drawers "
            "to the event."
        ),
        handler=_event_assert,
        input_schema={
            "type": "object",
            "required": ["period_id", "name"],
            "properties": {
                "period_id": {"type": "string"},
                "name": {"type": "string"},
                "gist": {"type": "string"},
                "occurred_at_ms": {"type": "integer"},
                "occurred_to_ms": {"type": "integer"},
                "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "drawer_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    ))


__all__ = ["register"]
