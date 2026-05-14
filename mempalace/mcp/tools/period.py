"""
Period-lifecycle MCP tools.

Per Part 11.1: open / close / seal a period.

Tools:
  - mempalace_period_open
  - mempalace_period_close
  - mempalace_period_seal

Spec ref: Part 11.1.
"""

from __future__ import annotations

import time
from typing import Any

from ...views.graph import get_default_graph


def _period_open(params: dict[str, Any]) -> dict[str, Any]:
    theme_id = params.get("theme_id", "")
    name = params.get("name", "")
    if not (theme_id and name):
        return {"opened": False, "error": "theme_id and name required"}
    started = params.get("started_at_ms") or int(time.time() * 1000)
    graph = get_default_graph()
    period_id = graph.create_period(
        theme_id=theme_id,
        name=name,
        started_at_ms=int(started),
        precedence=int(params.get("precedence", 0)),
        summary=str(params.get("summary", "")),
    )
    return {"opened": True, "period_id": period_id}


def _period_close(params: dict[str, Any]) -> dict[str, Any]:
    period_id = params.get("period_id", "")
    if not period_id:
        return {"closed": False, "error": "period_id required"}
    ended = params.get("ended_at_ms") or int(time.time() * 1000)
    graph = get_default_graph()
    try:
        graph.close_period(period_id=period_id, ended_at_ms=int(ended))
    except Exception as e:  # noqa: BLE001
        return {"closed": False, "error": f"{type(e).__name__}: {e}"}
    return {"closed": True, "ended_at_ms": int(ended)}


def _period_seal(params: dict[str, Any]) -> dict[str, Any]:
    period_id = params.get("period_id", "")
    if not period_id:
        return {"sealed": False, "error": "period_id required"}
    graph = get_default_graph()
    try:
        graph.seal_period(period_id=period_id)
    except Exception as e:  # noqa: BLE001
        return {"sealed": False, "error": f"{type(e).__name__}: {e}"}
    return {"sealed": True}


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_period_open",
        description="Open a new period under a theme.",
        handler=_period_open,
        input_schema={
            "type": "object",
            "required": ["theme_id", "name"],
            "properties": {
                "theme_id": {"type": "string"},
                "name": {"type": "string"},
                "started_at_ms": {"type": "integer"},
                "precedence": {"type": "integer"},
                "summary": {"type": "string"},
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_period_close",
        description="Close an open period (sets ended_at_ms; not yet sealed).",
        handler=_period_close,
        input_schema={
            "type": "object",
            "required": ["period_id"],
            "properties": {
                "period_id": {"type": "string"},
                "ended_at_ms": {"type": "integer"},
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_period_seal",
        description="Seal a closed period — final, becomes immutable canon.",
        handler=_period_seal,
        input_schema={
            "type": "object",
            "required": ["period_id"],
            "properties": {"period_id": {"type": "string"}},
        },
    ))


__all__ = ["register"]
