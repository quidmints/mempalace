"""
Self-entity accessor.

The self-entity is a designated, reserved entity node in every palace. It is
the subject of "I am" bindings (Moulin/Rathbone — semantic self-knowledge as
distinct from episodic memory) and the implicit subject of self-referential
assertions.

Spec ref: R3 §6 (I-am bindings), Part 3.1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import current as views
from ..schema.identifiers import SELF_ENTITY_ID, is_reserved
from ..schema.kinds import EdgeKind


@dataclass
class IamBinding:
    """A single I-am role-edge: self → target with role context.

    Conway/Moulin/Rathbone: "I am someone who [X]" — the X is realized as
    a `role_in_period` edge from the self-entity to a schema (trait,
    relational, possible-self, self-guide, value) or to another entity
    where role describes the relation ("I am Anna's mentor").

    The `period_id` scopes when the binding holds; bindings without a
    period_id are unscoped (lifetime/durable).
    """
    edge_id: str
    target_node_id: str
    role: str
    period_id: str | None = None
    properties: dict[str, Any] | None = None


def self_entity_id() -> str:
    """The designated self-entity ID. Always the same value within a palace."""
    return SELF_ENTITY_ID


def is_self_entity(node_id: str) -> bool:
    """Return True if node_id refers to the self-entity."""
    return node_id == SELF_ENTITY_ID and is_reserved(node_id)


def current_iams(period_id: str | None = None) -> list[IamBinding]:
    """Return current I-am bindings on the self-entity.

    Args:
        period_id: if provided, only bindings scoped to this period are
            returned (plus unscoped/lifetime bindings).

    Returns:
        List of IamBinding records for currently-active role edges.
    """
    edges = views.outgoing_edges(SELF_ENTITY_ID, kind=EdgeKind.ROLE_IN_PERIOD)
    out: list[IamBinding] = []
    for edge in edges:
        props = edge.properties or {}
        edge_period = props.get("period_id")
        if period_id is not None and edge_period not in (None, period_id):
            continue
        out.append(IamBinding(
            edge_id=edge.edge_id,
            target_node_id=edge.target_node_id,
            role=str(props.get("role", "")),
            period_id=edge_period,
            properties=props,
        ))
    return out


def iams_for_target(target_node_id: str) -> list[IamBinding]:
    """All I-am bindings that target a specific node (schema or entity)."""
    return [b for b in current_iams() if b.target_node_id == target_node_id]


def iam_roles() -> list[str]:
    """Distinct role labels from current I-ams.

    Useful for wake-up composition and identity-level summaries.
    """
    seen: dict[str, None] = {}
    for binding in current_iams():
        if binding.role:
            seen.setdefault(binding.role, None)
    return list(seen.keys())
