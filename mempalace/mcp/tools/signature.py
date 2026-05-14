"""
Signature-export MCP tool.

Per Part 11.1: `mempalace_signature` exports a SignatureSnapshot at
the requested level of detail (full / coarse / hash-only). Backed by
`mempalace.signatures.store`.

Spec ref: Part 11.1, R3 §8.2.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ...signatures.store import (
    SignatureSnapshot,
    get_signature_store,
)


def _serialize_snapshot(snap: SignatureSnapshot, level: str) -> dict[str, Any]:
    """Render a snapshot at the requested level.

    - "full"      : all fields
    - "coarse"    : drop per-theme detail, keep aggregates
    - "hash_only" : just snapshot_id + a fingerprint over content
    """
    if level == "hash_only":
        import hashlib
        body = repr(asdict(snap)).encode("utf-8")
        return {
            "snapshot_id": snap.snapshot_id,
            "period_id": snap.period_id,
            "fingerprint": hashlib.blake2b(body, digest_size=16).hexdigest(),
        }
    full = asdict(snap)
    if level == "coarse":
        # drop per-theme detail; keep top-line stats
        for key in (
            "per_theme_velocity",
            "per_theme_mean_position",
        ):
            full.pop(key, None)
    return full


def _signature(params: dict[str, Any]) -> dict[str, Any]:
    period_id = params.get("period_id", "")
    level = str(params.get("level", "full")).lower()
    if level not in ("full", "coarse", "hash_only"):
        return {"error": f"unknown level: {level}"}

    store = get_signature_store()
    if period_id:
        snap = store.get(period_id)
        if snap is None:
            return {"error": f"no snapshot for period_id: {period_id}"}
        return {"snapshot": _serialize_snapshot(snap, level)}
    # No period_id → return the latest
    latest = store.latest()
    if latest is None:
        return {"error": "no snapshots in store"}
    return {"snapshot": _serialize_snapshot(latest, level)}


def register(server: Any) -> None:
    from ..server import ToolSpec

    server.register(ToolSpec(
        name="mempalace_signature",
        description=(
            "Export a signature snapshot at the requested detail level. "
            "level ∈ {full, coarse, hash_only}."
        ),
        handler=_signature,
        input_schema={
            "type": "object",
            "properties": {
                "period_id": {"type": "string"},
                "level": {
                    "type": "string",
                    "enum": ["full", "coarse", "hash_only"],
                },
            },
        },
    ))


__all__ = ["register"]
