"""
HandleContext — the carrier type the search policy and ranker cache
have been referencing in comments without a concrete definition.

# What HandleContext is

The runtime state of a single handle's traversal: the query that
opened it, the InterpretiveFrames produced so far, the
ClusterTraversalPattern accumulated through hops, and the budget
already consumed.

Per HANDLES_DESIGN.md v2: the handle is a stateful semiotic-routing
session. It carries enough context that:

  - The `SearchPolicy` can decide the next step from frames + pattern
    + budget alone (no global state lookups).
  - The `RankerOutputCache` can derive a stable `cluster_signature()`
    for cache keys (Track 4A).
  - The walk audit (`WalkCompleted` event) can serialize a complete
    record of the traversal.

# Why it exists

Before this module: `search_policy.py` and `ranker_cache.py` had
references to `HandleContext` in their docstrings. The policy
worked around the absence by accepting individual fields
(`frames=...`, `pattern=...`); the cache worked around it by taking
a pre-computed `cluster_signature` string. That decoupling was
deliberate but it left the HandleContext concept undefined as a
type, with no compile-time check that the right data was being
passed in.

`HandleContext` is the missing class. It's a lightweight aggregator;
the policy still pre-extracts what it needs (so it's not coupled
to the full shape), but now there's a typed home for the data.

# What this module does NOT ship

The full `HandleState` machinery from HANDLES_DESIGN.md v2
(retrieval session, ranker chains, cache projection wiring, etc.)
is still in `mempalace/retrieve/handle.py` and hasn't been
promoted to this package. That's a separate piece of work.

Spec ref: HANDLES_DESIGN.md v2, SUBSTRATE_SIGNAL_ANALYSIS.md,
ranker_cache.py docstring, search_policy.py docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cluster_pattern import ClusterTraversalPattern, Hop
from .frame import InterpretiveFrame


@dataclass
class HandleContext:
    """One handle's runtime carrier.

    Construction:
      ctx = HandleContext(handle_id="hdl_x", query_hash="abc...")
      ctx.add_hop(Hop(...))
      ctx.frames.append(InterpretiveFrame(...))

    Snapshotting (for the WalkCompleted audit):
      snapshot = ctx.snapshot()
      log.append(WalkCompleted(..., final_cluster_signature=snapshot["cluster_signature"]))
    """

    handle_id: str
    """The id assigned at handle-open time. Used for log audit
    correlation and cache-namespacing."""

    query_hash: str = ""
    """Stable hash of the input query (text + filters + asks).
    Used as part of `RankerOutputCache` keys; lets the cache
    reuse hits across handles asking the same thing."""

    cluster_pattern: ClusterTraversalPattern = field(
        default_factory=lambda: ClusterTraversalPattern(),
    )
    """The traversal-pattern accumulator. Each step appends a Hop
    (cluster_signature) so the running pattern tracks where the
    walk has been. Track 4A's cache key derives from this."""

    frames: list[InterpretiveFrame] = field(default_factory=list)
    """Interpretive frames produced as the walk advances. The
    SearchPolicy reads frame confidences to decide what to do
    next (dominant frame? close frames? high dispersion?)."""

    total_hops: int = 0
    """Cumulative count across all directives. Diagnostic + budget
    accounting."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Open-ended metadata. Callers can stash session-specific
    state (e.g., recently-visited node ids for cycle avoidance)
    without changing the HandleContext shape."""

    # ---- mutators ----

    def add_hop(self, hop: Hop) -> None:
        """Record one hop in the cluster pattern + bump hop count."""
        self.cluster_pattern.add_hop(hop)
        self.total_hops += 1

    def add_frame(self, frame: InterpretiveFrame) -> None:
        """Append an interpretive frame produced during a step."""
        self.frames.append(frame)

    # ---- read helpers ----

    def cluster_signature(self) -> str:
        """Stable hash of the current cluster pattern.

        Identical to `cluster_pattern.cluster_signature()`; provided
        on the context for the convenience the ranker_cache docstring
        promised."""
        return self.cluster_pattern.cluster_signature()

    def is_pattern_stable(self, *, min_hops: int = 4) -> bool:
        """True iff the cluster pattern has stabilized.

        Wraps `ClusterTraversalPattern.is_stable`."""
        return self.cluster_pattern.is_stable(min_hops=min_hops)

    def snapshot(self) -> dict[str, Any]:
        """Serializable summary for the WalkCompleted audit event."""
        return {
            "handle_id": self.handle_id,
            "query_hash": self.query_hash,
            "total_hops": self.total_hops,
            "cluster_signature": self.cluster_signature(),
            "pattern_stable": self.is_pattern_stable(),
            "frame_count": len(self.frames),
            "frame_confidences": [f.confidence for f in self.frames],
        }


__all__ = [
    "HandleContext",
]
