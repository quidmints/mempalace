"""
Handle-protocol MCP tools.

Per Part 11.1: handle is the per-conversation reference to a thread of
context. Tools:

  - mempalace_handle_allocate
  - mempalace_handle_resolve
  - mempalace_handle_refine
  - mempalace_handle_close

Spec ref: Part 11.1.
"""

from __future__ import annotations

import time
import uuid
from typing import Any


# In-memory handle table — production wiring uses a real store.
_HANDLES: dict[str, dict[str, Any]] = {}


def _allocate(params: dict[str, Any]) -> dict[str, Any]:
    handle_id = f"hndl_{uuid.uuid4().hex[:12]}"
    _HANDLES[handle_id] = {
        "owner": params.get("owner", "unknown"),
        "context": params.get("initial_context", ""),
        "themes": list(params.get("themes", []) or []),
        "allocated_at_ms": int(time.time() * 1000),
        "closed": False,
        "refinements": [],
    }
    return {"handle_id": handle_id}


def _resolve(params: dict[str, Any]) -> dict[str, Any]:
    handle_id = params.get("handle_id", "")
    record = _HANDLES.get(handle_id)
    if record is None:
        return {"resolved": False, "error": "unknown handle_id"}
    return {"resolved": True, "handle": dict(record)}


def _refine(params: dict[str, Any]) -> dict[str, Any]:
    handle_id = params.get("handle_id", "")
    record = _HANDLES.get(handle_id)
    if record is None:
        return {"refined": False, "error": "unknown handle_id"}
    if record["closed"]:
        return {"refined": False, "error": "handle already closed"}
    refinement = params.get("refinement", "")
    if refinement:
        record["refinements"].append(
            {"refinement": refinement, "at_ms": int(time.time() * 1000)}
        )
    return {"refined": True, "refinement_count": len(record["refinements"])}


def _close(params: dict[str, Any]) -> dict[str, Any]:
    handle_id = params.get("handle_id", "")
    record = _HANDLES.get(handle_id)
    if record is None:
        return {"closed": False, "error": "unknown handle_id"}
    record["closed"] = True
    record["closed_at_ms"] = int(time.time() * 1000)
    return {"closed": True}


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_handle_allocate",
        description="Allocate a new handle for a conversation thread.",
        handler=_allocate,
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "initial_context": {"type": "string"},
                "themes": {"type": "array", "items": {"type": "string"}},
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_handle_resolve",
        description="Resolve a handle to its context record.",
        handler=_resolve,
        input_schema={
            "type": "object",
            "required": ["handle_id"],
            "properties": {"handle_id": {"type": "string"}},
        },
    ))
    server.register(ToolSpec(
        name="mempalace_handle_refine",
        description="Add a refinement to an existing handle.",
        handler=_refine,
        input_schema={
            "type": "object",
            "required": ["handle_id"],
            "properties": {
                "handle_id": {"type": "string"},
                "refinement": {"type": "string"},
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_handle_close",
        description="Close a handle.",
        handler=_close,
        input_schema={
            "type": "object",
            "required": ["handle_id"],
            "properties": {"handle_id": {"type": "string"}},
        },
    ))


__all__ = ["register"]
