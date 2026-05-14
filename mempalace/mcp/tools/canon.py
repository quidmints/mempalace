"""
Canonicalization MCP tools.

Per Part 11.1: tools to promote provisional canonicals and amend
existing ones. Backed by `mempalace.canonicalizer`.

Tools:
  - mempalace_promote_to_canon
  - mempalace_canon_amend

Spec ref: Part 11.1.
"""

from __future__ import annotations

from typing import Any

from ...canonicalizer import (
    CanonDomain,
    get_canonicalizer,
)


def _domain_or_error(d: str) -> CanonDomain | None:
    try:
        return CanonDomain(d.lower())
    except ValueError:
        return None


def _promote_to_canon(params: dict[str, Any]) -> dict[str, Any]:
    domain_str = params.get("domain", "")
    domain = _domain_or_error(str(domain_str))
    if domain is None:
        return {"promoted": False, "error": f"unknown domain: {domain_str}"}

    can = get_canonicalizer()
    promoted = can.check_promotions(domain)
    return {
        "promoted": True,
        "count": len(promoted),
        "canonicals": [
            {
                "canonical_id": c.canonical_id,
                "surface": c.surface,
                "member_count": c.member_count,
                "aliases": list(c.aliases),
            }
            for c in promoted
        ],
    }


def _canon_amend(params: dict[str, Any]) -> dict[str, Any]:
    """Mark a canonical as reverted; optionally redirect to a new canonical."""
    canonical_id = params.get("canonical_id", "")
    new_canonical = params.get("new_canonical")
    reason = params.get("reason", "")
    if not canonical_id:
        return {"amended": False, "error": "canonical_id required"}

    can = get_canonicalizer()
    ok = can.revert(
        canonical_id,
        reason=str(reason),
        new_canonical=str(new_canonical) if new_canonical else None,
    )
    return {"amended": ok}


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_promote_to_canon",
        description="Run canonical promotion for a domain.",
        handler=_promote_to_canon,
        input_schema={
            "type": "object",
            "required": ["domain"],
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": [d.value for d in CanonDomain],
                },
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_canon_amend",
        description=(
            "Revert a canonical mapping, optionally redirecting to a new "
            "canonical. Reversibility per R3 §4.5."
        ),
        handler=_canon_amend,
        input_schema={
            "type": "object",
            "required": ["canonical_id"],
            "properties": {
                "canonical_id": {"type": "string"},
                "new_canonical": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    ))


__all__ = ["register"]
