"""
Cluster traversal pattern + Hop types (Track 3 supporting types).

Per HANDLES_DESIGN.md v2 §"Cluster traversal pattern":

    ClusterTraversalPattern: sliding window of recent hops + derived
    dominance signals. cluster_signature() returns a stable hash used
    by Track 4A's cache key and by Track 4B's projection mechanism.

# Why a sliding window with K=8

The cluster pattern is meant to capture "what the walk has been doing
lately" — recent enough to detect direction change, long enough that
single-hop noise doesn't flip the signature. K=8 was the design's
default; the constant is exposed so callers can tune.

# Stable hash

The signature must be deterministic across processes for cache keys
to match. Uses sorted-tuple inputs to a SHA-256-like digest; no
floating-point fields go into the hash directly (would lose
determinism across platforms). Numeric features get bucketed first.

Spec ref: HANDLES_DESIGN.md v2 §"Cluster traversal pattern", Track
4A's `cluster_signature` requirement.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

DEFAULT_CLUSTER_WINDOW_K = 8
"""How many recent hops the cluster pattern remembers. Per design."""


@dataclass(frozen=True)
class Hop:
    """One step in a walk.

    Frozen so equal hops are hashable; the cluster pattern uses
    `(from_node_id, to_node_id)` and `edge_kind` as identity.
    """

    from_node_id: str
    to_node_id: str
    edge_id: str
    edge_kind: str
    edge_confidence: float = 1.0
    chosen_by: str = ""
    """Identifier of which frame / which ranker chose this hop.
    Empty for unscored hops."""
    chosen_at_step: int = 0


def _confidence_bucket(c: float) -> int:
    """Bucket [0,1] confidence into 10 discrete buckets so the hash
    is stable across platforms / float-rounding differences.
    """
    if c <= 0.0:
        return 0
    if c >= 1.0:
        return 9
    return int(c * 10.0)


@dataclass
class ClusterTraversalPattern:
    """Sliding window of recent hops + derived dominance signals.

    Mutable: callers `add_hop()` as the walk progresses. The
    derived `dominant_edge_kinds` and `dominant_node_transitions` are
    recomputed lazily on access (not on every add — recompute is O(K)
    but cheap; doing it eagerly is wasted work for a window that's
    going to be added to several times before being read).
    """

    recent_hops: deque[Hop] = field(default_factory=deque)
    window_size: int = DEFAULT_CLUSTER_WINDOW_K

    def __post_init__(self) -> None:
        # If a deque was passed in without maxlen, retrofit it
        if self.recent_hops.maxlen is None:
            self.recent_hops = deque(self.recent_hops, maxlen=self.window_size)

    def add_hop(self, hop: Hop) -> None:
        """Append a hop. Oldest hop drops off when window is full."""
        # Reset deque maxlen if needed (defensive)
        if self.recent_hops.maxlen != self.window_size:
            self.recent_hops = deque(
                self.recent_hops, maxlen=self.window_size
            )
        self.recent_hops.append(hop)

    @property
    def dominant_edge_kinds(self) -> list[str]:
        """Edge kinds appearing in the window, sorted by frequency
        descending. Ties broken alphabetically for determinism."""
        if not self.recent_hops:
            return []
        counts: dict[str, int] = {}
        for h in self.recent_hops:
            counts[h.edge_kind] = counts.get(h.edge_kind, 0) + 1
        return [
            ek
            for ek, _ in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]

    @property
    def dominant_node_transitions(self) -> list[tuple[str, str]]:
        """Most common (from_node_id, to_node_id) pairs. Useful when
        the walk keeps revisiting the same edge (potential cycle
        signal) or when paired transitions tell the search policy
        the walk is settling.
        """
        if not self.recent_hops:
            return []
        counts: dict[tuple[str, str], int] = {}
        for h in self.recent_hops:
            pair = (h.from_node_id, h.to_node_id)
            counts[pair] = counts.get(pair, 0) + 1
        return [
            pair
            for pair, _ in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]

    def cluster_signature(self) -> str:
        """Stable hash of the pattern.

        Used by Track 4A's cache key (`ranker_output_pattern_key`).
        Two patterns with the same hops in the same order produce
        the same signature; floating-point confidences are bucketed
        first to keep the hash deterministic across platforms.
        """
        if not self.recent_hops:
            return "cs_empty"
        # Build a deterministic byte string from the window.
        # Don't use Python's hash() — it salts.
        h = hashlib.sha256()
        for hop in self.recent_hops:
            h.update(hop.from_node_id.encode("utf-8"))
            h.update(b"\x00")
            h.update(hop.to_node_id.encode("utf-8"))
            h.update(b"\x00")
            h.update(hop.edge_kind.encode("utf-8"))
            h.update(b"\x00")
            h.update(_confidence_bucket(hop.edge_confidence).to_bytes(1, "big"))
            h.update(b"\xff")
        return f"cs_{h.hexdigest()[:24]}"

    def __len__(self) -> int:
        return len(self.recent_hops)

    def is_stable(self, *, min_hops: int = 4) -> bool:
        """True if the pattern has shown the same dominant edge kind
        for the most recent `min_hops` hops.

        Used by Track 3's search policy to detect "walk is stuck" —
        when the cluster pattern isn't changing, the policy
        terminates rather than continue spending budget on no-op hops.
        """
        if len(self.recent_hops) < min_hops:
            return False
        recent = list(self.recent_hops)[-min_hops:]
        first_kind = recent[0].edge_kind
        return all(h.edge_kind == first_kind for h in recent)


__all__ = [
    "DEFAULT_CLUSTER_WINDOW_K",
    "ClusterTraversalPattern",
    "Hop",
]
