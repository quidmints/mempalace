"""
Velocity-readout MCP tool.

Per Part 11.1: `mempalace_velocity` returns the access-velocity for a
node (theme, period, schema, or any node with a heat counter). Backed
by `mempalace.features.compute.compute_drawer_velocity_30d`.

Spec ref: Part 11.1.
"""

from __future__ import annotations

from typing import Any

from ...features.compute import (
    compute_drawer_velocity_30d,
    compute_period_velocity_coupling,
)


def _velocity(params: dict[str, Any]) -> dict[str, Any]:
    subject_id = params.get("subject_id", "")
    if not subject_id:
        return {"error": "subject_id required"}
    ctx: dict[str, Any] = dict(params.get("context", {}) or {})
    velocity_30d = compute_drawer_velocity_30d(subject_id, ctx)
    coupling = compute_period_velocity_coupling(subject_id, ctx)
    return {
        "subject_id": subject_id,
        "velocity_30d": velocity_30d,
        "period_velocity_coupling": coupling,
    }


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_velocity",
        description=(
            "Return the 30-day velocity and period-coupling for a node "
            "(theme, period, schema, or any node with a heat counter)."
        ),
        handler=_velocity,
        input_schema={
            "type": "object",
            "required": ["subject_id"],
            "properties": {
                "subject_id": {"type": "string"},
                "context": {"type": "object"},
            },
        },
    ))


__all__ = ["register"]
