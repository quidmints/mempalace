"""
Pending-review MCP tool.

Per R3 §4.4: surfaces miner proposals + canonicalizer candidate
clusters in their provisional state for user/agent review. App's
review mode is a UI on top of this; agents can also call it directly.

Categories surfaced:
  - "miner_proposals"  — provisional miner outputs
  - "canon_candidates" — clusters in the candidate pool, per domain
  - "all"              — both

Spec ref: R3 §4.4.
"""

from __future__ import annotations

from typing import Any

from ...canonicalizer import (
    CanonDomain,
    get_canonicalizer,
)
from ...miner.proposals import (
    get_proposal_store,
)
from ...miner.base import ProposalLifecycle


_VALID_CATEGORIES = ("miner_proposals", "canon_candidates", "all")


def _miner_proposals_payload(limit: int) -> list[dict[str, Any]]:
    store = get_proposal_store()
    pending = store.by_lifecycle(ProposalLifecycle.PROVISIONAL)
    out: list[dict[str, Any]] = []
    for entry in pending[:limit]:
        out.append({
            "proposal_id": entry.record.proposal_id,
            "proposal_kind": entry.record.proposal_kind,
            "target_node_id": entry.record.target_node_id,
            "proposed_value": entry.record.proposed_value,
            "confidence": entry.record.confidence,
            "miner_class": entry.record.miner_class,
            "miner_version": entry.record.miner_version,
            "corroboration_count": entry.corroboration_count,
            "rejection_count": entry.rejection_count,
        })
    return out


def _canon_candidates_payload(limit: int) -> list[dict[str, Any]]:
    can = get_canonicalizer()
    out: list[dict[str, Any]] = []
    for d in CanonDomain:
        clusters = can.candidate_clusters(d)
        for cl in clusters:
            distinct_passes = set()
            for m in cl.members:
                distinct_passes.update(m.seen_in_passes)
            out.append({
                "domain": d.value,
                "cluster_id": cl.cluster_id,
                "member_count": len(cl.members),
                "distinct_passes": sorted(distinct_passes),
                "first_seen_ms": cl.first_seen_ms,
                "last_seen_ms": cl.last_seen_ms,
                "members": [
                    {"surface": m.surface, "seen_at_ms": m.seen_at_ms}
                    for m in cl.members
                ],
            })
            if len(out) >= limit:
                return out
    return out


def _pending_review(params: dict[str, Any]) -> dict[str, Any]:
    category = str(params.get("category", "all")).lower()
    if category not in _VALID_CATEGORIES:
        return {"error": f"unknown category: {category}"}
    limit = int(params.get("limit", 100))

    payload: dict[str, Any] = {"category": category}
    if category in ("miner_proposals", "all"):
        payload["miner_proposals"] = _miner_proposals_payload(limit)
    if category in ("canon_candidates", "all"):
        payload["canon_candidates"] = _canon_candidates_payload(limit)
    return payload


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_pending_review",
        description=(
            "Surface items pending user/agent review. category ∈ "
            "{miner_proposals, canon_candidates, all}."
        ),
        handler=_pending_review,
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(_VALID_CATEGORIES),
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
    ))


__all__ = ["register"]
