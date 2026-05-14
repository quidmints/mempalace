"""
I-am role-set MCP tool.

Per Part 11.1: `mempalace_iams` returns the current self-entity's
active I-am bindings (roles + their target nodes). Backed by
`mempalace.views.self_entity`.

Spec ref: Part 11.1.
"""

from __future__ import annotations

from typing import Any

from ...views.self_entity import (
    current_iams,
    iam_roles,
    self_entity_id,
)


def _iams(params: dict[str, Any]) -> dict[str, Any]:
    period_id = params.get("period_id") or None
    bindings = current_iams(period_id=period_id)
    return {
        "self_entity_id": self_entity_id(),
        "iam_roles": iam_roles(),
        "bindings": [
            {
                "role": b.role,
                "target_node_id": b.target_node_id,
                "valid_from_ms": b.valid_from_ms,
                "valid_to_ms": b.valid_to_ms,
                "weight": b.weight,
                "period_id": b.period_id,
            }
            for b in bindings
        ],
    }


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_iams",
        description=(
            "Return the current self-entity's active I-am role bindings. "
            "Optionally filter by period_id."
        ),
        handler=_iams,
        input_schema={
            "type": "object",
            "properties": {"period_id": {"type": "string"}},
        },
    ))


__all__ = ["register"]
