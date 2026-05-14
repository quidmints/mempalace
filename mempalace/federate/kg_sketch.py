"""
Knowledge-graph MinHash sketch.

Per R3 §9.5 (Layer 1) and Part 9.2: cheap structural fingerprint of a
palace's KG. Used by StructuralMatchingLayer to estimate Jaccard-similar
"reasoning shape" without exposing substrate.

Design:

  - Read current_nodes + current_edges views.
  - Build a multiset of typed walks (length-2 paths) of the form
    (src_kind, edge_kind, dst_kind), weighted by edge kind.
  - Hash each walk with k independent hash functions; keep the per-
    function min-hash.

Length-2 captures local connection patterns; longer walks blow up
combinatorially without much gain at sketch resolution. Edge-kind
weighting lets meaningful edges (DERIVED_FROM, ASSERTS, GOAL_OF)
dominate the noisier ones (TIMELINE_NEAR).

Spec ref: R3 §9.5, Part 9.2.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Edge-kind weights
# =============================================================================
#
# Higher weight → repeated more times in the multiset → more likely to
# survive the min-hash. The values aren't arbitrary: DERIVED_FROM is
# the structural backbone of the assertion graph (R3 §6.2).

EDGE_KIND_WEIGHTS: dict[str, int] = {
    "derived_from": 8,
    "asserts": 5,
    "goal_of": 5,
    "schema_grounded_in": 4,
    "recurrence_member": 4,
    "interpretation_of": 3,
    "context_in": 3,
    "timeline_near": 1,
    "associated_with": 2,
}

DEFAULT_EDGE_WEIGHT = 2


# =============================================================================
# Hash functions
# =============================================================================


def _h(seed: int, data: bytes) -> int:
    """64-bit hash of (seed, data) via blake2b."""
    h = hashlib.blake2b(digest_size=8)
    h.update(struct.pack("<Q", seed))
    h.update(data)
    return int.from_bytes(h.digest(), "little", signed=False)


def _walk_bytes(walk: tuple[str, str, str]) -> bytes:
    src_kind, edge_kind, dst_kind = walk
    return f"{src_kind}|{edge_kind}|{dst_kind}".encode("utf-8")


# =============================================================================
# Sketch
# =============================================================================


@dataclass
class KGSketch:
    """A length-K MinHash signature of a KG."""

    signature: list[int]                       # length = num_hashes
    num_walks: int = 0
    edge_kind_distribution: dict[str, int] = field(default_factory=dict)
    schema_version: str = "kg_sketch.v1"

    def __len__(self) -> int:
        return len(self.signature)


def build_kg_sketch(
    *,
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    num_hashes: int = 128,
) -> KGSketch:
    """Build a KG sketch from current_nodes + current_edges views.

    `nodes` items must have {"node_id", "kind"}.
    `edges` items must have {"src_id", "dst_id", "kind"}.

    Returns a KGSketch with `num_hashes`-length signature.
    """
    # Index node kinds for fast lookup
    kind_by_id: dict[str, str] = {}
    for n in nodes:
        nid = n.get("node_id")
        nk = n.get("kind")
        if isinstance(nid, str) and isinstance(nk, str):
            kind_by_id[nid] = nk

    # Build typed-walk multiset
    walk_counts: dict[tuple[str, str, str], int] = {}
    edge_kind_dist: dict[str, int] = {}
    for e in edges:
        src = e.get("src_id")
        dst = e.get("dst_id")
        ek = e.get("kind")
        if not isinstance(src, str) or not isinstance(dst, str) or not isinstance(ek, str):
            continue
        sk = kind_by_id.get(src)
        dk = kind_by_id.get(dst)
        if sk is None or dk is None:
            continue
        walk = (sk, ek, dk)
        weight = EDGE_KIND_WEIGHTS.get(ek, DEFAULT_EDGE_WEIGHT)
        walk_counts[walk] = walk_counts.get(walk, 0) + weight
        edge_kind_dist[ek] = edge_kind_dist.get(ek, 0) + 1

    # Compute MinHash signature
    INF = (1 << 64) - 1
    signature = [INF] * num_hashes
    total_walks = 0
    for walk, count in walk_counts.items():
        wb = _walk_bytes(walk)
        # repeat `count` times by salting the hash with the repetition index;
        # equivalent to inserting `count` copies into the multiset
        for rep in range(count):
            for i in range(num_hashes):
                hv = _h(i * 1_000_003 + rep, wb)
                if hv < signature[i]:
                    signature[i] = hv
        total_walks += count

    return KGSketch(
        signature=signature,
        num_walks=total_walks,
        edge_kind_distribution=edge_kind_dist,
    )


def sketch_jaccard(a: KGSketch, b: KGSketch) -> float:
    """Estimated Jaccard similarity between two sketches."""
    if not a.signature or not b.signature:
        return 0.0
    if len(a.signature) != len(b.signature):
        # Sketches must be same length to compare
        n = min(len(a.signature), len(b.signature))
        if n == 0:
            return 0.0
        sa = a.signature[:n]
        sb = b.signature[:n]
    else:
        sa = a.signature
        sb = b.signature
    matches = sum(1 for x, y in zip(sa, sb, strict=True) if x == y)
    return matches / len(sa)


# =============================================================================
# Schema fingerprints (companion to sketch, used by Layer 1)
# =============================================================================


def schema_fingerprint(schema_id: str, predicate: str, arity: int) -> str:
    """Stable short fingerprint for an assertion schema.

    Used by Layer 1 to compute schema_jaccard. Two palaces share a
    fingerprint iff their canonicalizers landed on the same schema.

    Returns a 16-hex-char fingerprint.
    """
    h = hashlib.blake2b(digest_size=8)
    h.update(schema_id.encode("utf-8"))
    h.update(b"|")
    h.update(predicate.encode("utf-8"))
    h.update(b"|")
    h.update(struct.pack("<I", arity))
    return h.hexdigest()


__all__ = [
    "DEFAULT_EDGE_WEIGHT",
    "EDGE_KIND_WEIGHTS",
    "KGSketch",
    "build_kg_sketch",
    "schema_fingerprint",
    "sketch_jaccard",
]
