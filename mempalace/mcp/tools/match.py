"""
Federation match-request and findings MCP tools.

Per Part 11.1: tools to request layered matching against a peer
palace and to enumerate the findings emitted by completed matches.

Tools:
  - mempalace_match_request — request a sandbox-bound match against a peer
  - mempalace_findings      — list (or filter) recent findings

Spec ref: Part 11.1, R3 §6 / §7 (federation).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ...federate.findings import (
    FindingTopology,
    build_finding,
)
from ...federate.transport import get_transport


# In-memory store of recent findings (production wiring pulls from log views)
_FINDINGS: list[dict[str, Any]] = []


def _match_request(params: dict[str, Any]) -> dict[str, Any]:
    """Initiate a match request against a peer palace."""
    peer_id = params.get("peer_palace_id", "")
    if not peer_id:
        return {"requested": False, "error": "peer_palace_id required"}
    layer = int(params.get("layer", 1))
    if layer not in (1, 2, 3):
        return {"requested": False, "error": "layer must be 1, 2, or 3"}

    match_id = f"match_{uuid.uuid4().hex[:12]}"
    transport = get_transport()
    peers = transport.known_peers()
    peer_known = any(p.peer_id == peer_id for p in peers)

    return {
        "requested": True,
        "match_id": match_id,
        "peer_palace_id": peer_id,
        "layer": layer,
        "peer_known": peer_known,
        "requested_at_ms": int(time.time() * 1000),
    }


def _findings(params: dict[str, Any]) -> dict[str, Any]:
    """Return recent findings, optionally filtered by topology / match."""
    topology = params.get("topology")
    match_id = params.get("match_id")
    limit = int(params.get("limit", 100))

    out: list[dict[str, Any]] = []
    for f in _FINDINGS:
        if topology and f.get("topology") != topology:
            continue
        if match_id and f.get("match_id") != match_id:
            continue
        out.append(f)
        if len(out) >= limit:
            break
    return {"findings": out, "count": len(out)}


def add_finding_for_test(finding: dict[str, Any]) -> None:
    """Test helper: seed the in-memory finding list."""
    _FINDINGS.append(finding)


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_match_request",
        description=(
            "Request a sandbox-bound match against a peer palace at the "
            "specified layer (1=structural, 2=derivation, 3=substrate)."
        ),
        handler=_match_request,
        input_schema={
            "type": "object",
            "required": ["peer_palace_id"],
            "properties": {
                "peer_palace_id": {"type": "string"},
                "layer": {"type": "integer", "enum": [1, 2, 3]},
                "themes": {"type": "array", "items": {"type": "string"}},
                "privacy_mode": {"type": "string"},
            },
        },
    ))
    server.register(ToolSpec(
        name="mempalace_findings",
        description=(
            "List recent findings emitted by completed matches. Optional "
            "filters: topology, match_id."
        ),
        handler=_findings,
        input_schema={
            "type": "object",
            "properties": {
                "topology": {
                    "type": "string",
                    "enum": [t.value for t in FindingTopology],
                },
                "match_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
    ))


__all__ = ["add_finding_for_test", "register"]
