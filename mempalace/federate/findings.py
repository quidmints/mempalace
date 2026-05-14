"""
Findings emission.

Per R3 §7.5 / R3 §10 / Part 9.2: when a sandboxed match completes,
we emit a structured Finding describing the outcome:

  - topology  ∈ { peer | mentor | complementary | divergent }
  - strength_per_dimension: per-axis score in [0, 1]
  - target: where the finding goes (currently "switchboard"; will be
    "mempalace_federation" once Switchboard is replaced)

Findings are signed by the session key (so the on-chain side can prove
they came from a specific sandbox session) and emitted both to the
local log (FindingEmitted event) and over /mempalace/findings/1.0.0
to the switchboard / federation oracle.

Spec ref: R3 §7.5, R3 §10.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import FindingEmitted
from ..schema.identifiers import make_event_id_log
from .session_keys import SessionKeyManager, get_session_key_manager


# =============================================================================
# Topology enum
# =============================================================================


class FindingTopology(str, Enum):
    """The four match topologies per R3 §10."""

    PEER = "peer"
    MENTOR = "mentor"
    COMPLEMENTARY = "complementary"
    DIVERGENT = "divergent"


# =============================================================================
# Finding payload
# =============================================================================


@dataclass
class Finding:
    """A structured finding from a completed match."""

    schema_version: str = "finding.v1"
    match_id: str = ""
    topology: FindingTopology = FindingTopology.PEER
    strength_per_dimension: dict[str, float] = field(default_factory=dict)
    layer_breakdowns: dict[str, dict[str, float]] = field(default_factory=dict)
    target: str = "mempalace_federation"  # was "switchboard"; renamed
                                            # per Switchboard-replacement plan
                                            # (Batch 13). Old value still
                                            # accepted by callers passing it
                                            # explicitly during transition.
    emitted_at_ms: int = 0
    emitter_palace_id: str = ""

    # signature
    session_pubkey_hex: str = ""
    signature_hex: str = ""

    # provenance hash for re-derivability auditing
    provenance_hash_hex: str = ""

    def content_bytes(self) -> bytes:
        d = asdict(self)
        d.pop("signature_hex", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


# =============================================================================
# Builder
# =============================================================================


def _provenance_hash(
    *,
    sandbox_id: str,
    layer1_breakdown: dict[str, float] | None,
    layer2_breakdown: dict[str, float] | None,
    layer3_breakdown: dict[str, float] | None,
) -> str:
    """Hash that downstream verifiers can use to confirm the finding
    actually came from a real layered run, not synthesized."""
    payload = {
        "sandbox_id": sandbox_id,
        "layer1": layer1_breakdown or {},
        "layer2": layer2_breakdown or {},
        "layer3": layer3_breakdown or {},
    }
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        digest_size=32,
    ).hexdigest()


def build_finding(
    *,
    match_id: str,
    sandbox_id: str,
    topology: FindingTopology,
    strength_per_dimension: dict[str, float],
    layer1_breakdown: dict[str, float] | None,
    layer2_breakdown: dict[str, float] | None,
    layer3_breakdown: dict[str, float] | None,
    emitter_palace_id: str,
    session_key_id: str,
    target: str = "mempalace_federation",
    session_mgr: SessionKeyManager | None = None,
    now_ms: int | None = None,
) -> Finding:
    """Assemble and sign a Finding."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    mgr = session_mgr or get_session_key_manager()

    f = Finding(
        match_id=match_id,
        topology=topology,
        strength_per_dimension=dict(strength_per_dimension),
        layer_breakdowns={
            "layer1": dict(layer1_breakdown or {}),
            "layer2": dict(layer2_breakdown or {}),
            "layer3": dict(layer3_breakdown or {}),
        },
        target=target,
        emitted_at_ms=now_ms,
        emitter_palace_id=emitter_palace_id,
        session_pubkey_hex=mgr.get_pubkey(session_key_id) or "",
        provenance_hash_hex=_provenance_hash(
            sandbox_id=sandbox_id,
            layer1_breakdown=layer1_breakdown,
            layer2_breakdown=layer2_breakdown,
            layer3_breakdown=layer3_breakdown,
        ),
    )
    sig = mgr.sign(session_key_id, f.content_bytes())
    f.signature_hex = sig.hex() if sig is not None else ""
    return f


# =============================================================================
# Emission
# =============================================================================


def emit_finding(
    finding: Finding,
    *,
    log: LogClient | None = None,
) -> None:
    """Emit a Finding: log it locally and (in production) ship it over
    /mempalace/findings/1.0.0.

    Shipping is delegated to the transport layer; this function only
    appends the local audit event. The transport-side shipper subscribes
    to FindingEmitted via the subscriber pattern.
    """
    log_client = log or get_default_client()
    now = finding.emitted_at_ms or int(time.time() * 1000)

    # Schema event uses string topology + flattened strength dict
    log_client.append(
        FindingEmitted(
            event_id=make_event_id_log(now),
            recorded_at=now,
            actor="federate.findings",
            match_id=finding.match_id,
            topology=finding.topology.value,
            strength_per_dimension=dict(finding.strength_per_dimension),
            target=finding.target,
        )
    )


# =============================================================================
# Topology classifier
# =============================================================================


def classify_topology(
    *,
    peer_score: float,
    asymmetry: float,
    overlap: float,
    divergence: float,
) -> FindingTopology:
    """Map per-axis scores to a topology label.

    Heuristic per R3 §10:

      peer          — high peer_score, low asymmetry
      mentor        — high asymmetry (one side is "ahead" on shared region)
      complementary — moderate overlap, both sides have unique strengths
      divergent     — high divergence, low overlap
    """
    if divergence > 0.6 and overlap < 0.4:
        return FindingTopology.DIVERGENT
    if asymmetry > 0.55:
        return FindingTopology.MENTOR
    if peer_score > 0.6 and asymmetry < 0.3:
        return FindingTopology.PEER
    return FindingTopology.COMPLEMENTARY


__all__ = [
    "Finding",
    "FindingTopology",
    "build_finding",
    "classify_topology",
    "emit_finding",
]
