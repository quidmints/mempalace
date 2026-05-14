"""
Assertion-write MCP tool.

Per Part 11.1: `mempalace_assert` writes an assertion (8-part frame:
subject, predicate, object, time, source, stance, confidence,
provenance). Routes through `mempalace.views.graph.Graph.add_assertion`.

Note: filename is `assert_.py` because `assert` is a Python keyword.

Spec ref: Part 11.1.
"""

from __future__ import annotations

from typing import Any

from ...views.graph import get_default_graph


def _assert(params: dict[str, Any]) -> dict[str, Any]:
    from ...schema.events import DerivationType

    subject = params.get("subject_id", "") or params.get("subject", "")
    predicate = params.get("predicate", "")
    obj = params.get("object_id", "") or params.get("object", "")
    if not (subject and predicate and obj):
        return {"asserted": False, "error": "subject, predicate, object required"}

    derivation_str = str(params.get("derivation", "observation")).lower()
    try:
        derivation = DerivationType(derivation_str)
    except ValueError:
        derivation = DerivationType.OBSERVATION

    graph = get_default_graph()
    try:
        assertion_id = graph.add_assertion(
            subject_id=subject,
            predicate=predicate,
            object_id=obj,
            derived_from_drawers=list(params.get("derived_from_drawers", []) or []),
            valid_from_ms=params.get("valid_from_ms"),
            valid_to_ms=params.get("valid_to_ms"),
            derivation=derivation,
            confidence=float(params.get("confidence", 1.0)),
        )
    except TypeError as e:
        return {"asserted": False, "error": f"invalid assertion: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"asserted": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "asserted": True,
        "assertion_id": assertion_id,
        "frame": {
            "subject_id": subject,
            "predicate": predicate,
            "object_id": obj,
            "confidence": float(params.get("confidence", 1.0)),
            "derivation": derivation.value,
            "derived_from_drawers": list(params.get("derived_from_drawers", []) or []),
        },
    }


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_assert",
        description=(
            "Assert a triple with the 8-part frame (subject, predicate, "
            "object, time, source, stance, confidence, provenance)."
        ),
        handler=_assert,
        input_schema={
            "type": "object",
            "required": ["subject_id", "predicate", "object_id"],
            "properties": {
                "subject_id": {"type": "string"},
                "predicate": {"type": "string"},
                "object_id": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "source": {"type": "string"},
                "stance": {"type": "string"},
                "valid_from_ms": {"type": "integer"},
                "valid_to_ms": {"type": "integer"},
            },
        },
    ))


__all__ = ["register"]
