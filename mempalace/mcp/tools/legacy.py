"""
Legacy compatibility shim for old MCP tool names.

Per Part 11.3: the previous MempalaceServer (palace.py / palace_graph.py
based) exposed a different surface — `mempalace_search`, `mempalace_kg_query`,
`mempalace_add_drawer`, etc. To smooth the migration, we register
deprecation-marked aliases that route to the new tool surface.

Old → new mapping:
  mempalace_search        → mempalace_pending_review (closest equivalent)
  mempalace_kg_query      → no direct equivalent; suggest views API
  mempalace_kg_add        → mempalace_assert
  mempalace_add_drawer    → no direct equivalent; suggest log.append
  mempalace_diary_write   → no direct equivalent; suggest log.append
  mempalace_get_taxonomy  → no direct equivalent; suggest mempalace_pending_review

The shim returns deprecation messages along with results so callers
notice and migrate.

Spec ref: Part 11.3.
"""

from __future__ import annotations

from typing import Any


def _deprecated_kg_add(params: dict[str, Any]) -> dict[str, Any]:
    """Old `mempalace_kg_add` → new `mempalace_assert`."""
    # Translate old fields (subject/predicate/object) to new (subject_id/object_id).
    new_params = {
        "subject_id": params.get("subject", "") or params.get("subject_id", ""),
        "predicate": params.get("predicate", ""),
        "object_id": params.get("object", "") or params.get("object_id", ""),
        "confidence": params.get("confidence", 1.0),
    }
    if "started_at_ms" in params:
        new_params["valid_from_ms"] = params["started_at_ms"]
    if "ended_at_ms" in params:
        new_params["valid_to_ms"] = params["ended_at_ms"]

    # Directly invoke the new tool's handler to avoid nested asyncio.run.
    from .assert_ import _assert
    return _assert(new_params)


def _deprecated_search(params: dict[str, Any]) -> dict[str, Any]:
    """Old `mempalace_search` → no direct equivalent; surface review queue."""
    return {
        "deprecated": True,
        "deprecation_message": (
            "mempalace_search has no direct equivalent. Use mempalace_pending_review "
            "for review-mode browsing or build a stack with retrieve.handle for typed "
            "retrieval."
        ),
        "suggested_replacement": "mempalace_pending_review",
    }


def _deprecated_kg_query(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "deprecated": True,
        "deprecation_message": (
            "mempalace_kg_query has no direct equivalent. Use mempalace.views.current "
            "(outgoing_edges / incoming_edges / current_node) directly, or build a "
            "retrieval stack."
        ),
        "suggested_replacement": "mempalace.views.current",
    }


def _deprecated_add_drawer(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "deprecated": True,
        "deprecation_message": (
            "mempalace_add_drawer has been removed; drawer creation now goes through "
            "the log directly via DrawerCreated events. Use log_client.append() with "
            "a DrawerCreated event from mempalace.schema.events."
        ),
        "suggested_replacement": "mempalace.log.client + DrawerCreated event",
    }


def _deprecated_diary_write(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "deprecated": True,
        "deprecation_message": (
            "mempalace_diary_write has been removed; agent diary entries are now "
            "ordinary drawers tagged with the diary theme. Emit a DrawerCreated event "
            "and link it via add_drawer_to_event."
        ),
        "suggested_replacement": "mempalace.log.client + DrawerCreated event",
    }


def _deprecated_get_taxonomy(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "deprecated": True,
        "deprecation_message": (
            "mempalace_get_taxonomy has been removed. Theme/region structure is now "
            "queryable via views.current.canonical_nodes() and "
            "views.current.nodes_of_kind(NodeKind.THEME)."
        ),
        "suggested_replacement": "mempalace.views.current.nodes_of_kind",
    }


def register(server: Any) -> None:
    from ..server import ToolSpec

    legacy_specs = [
        ToolSpec(
            name="mempalace_kg_add",
            description="(deprecated) Old kg-add tool. Routes to mempalace_assert.",
            handler=_deprecated_kg_add,
            deprecated=True,
            deprecation_message="Use mempalace_assert instead.",
            input_schema={
                "type": "object",
                "required": ["subject", "predicate", "object"],
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "confidence": {"type": "number"},
                    "started_at_ms": {"type": "integer"},
                    "ended_at_ms": {"type": "integer"},
                },
            },
        ),
        ToolSpec(
            name="mempalace_search",
            description="(deprecated) Old search tool — no direct equivalent.",
            handler=_deprecated_search,
            deprecated=True,
            deprecation_message="Use mempalace_pending_review or build a retrieval stack.",
        ),
        ToolSpec(
            name="mempalace_kg_query",
            description="(deprecated) Old kg-query tool — use views.current directly.",
            handler=_deprecated_kg_query,
            deprecated=True,
            deprecation_message="Use mempalace.views.current directly.",
        ),
        ToolSpec(
            name="mempalace_add_drawer",
            description="(deprecated) Use log_client + DrawerCreated.",
            handler=_deprecated_add_drawer,
            deprecated=True,
            deprecation_message="Use the log client directly.",
        ),
        ToolSpec(
            name="mempalace_diary_write",
            description="(deprecated) Diary entries are ordinary drawers now.",
            handler=_deprecated_diary_write,
            deprecated=True,
            deprecation_message="Emit a DrawerCreated event in a diary theme.",
        ),
        ToolSpec(
            name="mempalace_get_taxonomy",
            description="(deprecated) Use views.current.nodes_of_kind.",
            handler=_deprecated_get_taxonomy,
            deprecated=True,
            deprecation_message="Use mempalace.views.current.nodes_of_kind.",
        ),
    ]
    for s in legacy_specs:
        server.register(s)


__all__ = ["register"]
