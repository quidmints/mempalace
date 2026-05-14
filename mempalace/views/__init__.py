"""
mempalace.views — typed accessors over the master views.

This package wraps the Rust DDflow views (mempalace_core) with Pythonic
typed accessors. Consumers in other packages reach views through this
module, never through PyO3 directly.

Submodules:
  - current        — typed accessors for nodes, edges, interpretations
  - graph          — high-level filing-cabinet API for graph mutations
  - walk           — typed traversals with stance-aware weighting
  - timeline       — bitemporal queries (world-time vs system-time)
  - self_entity    — designated self-entity and I-am bindings
  - topology       — Track 6A: paginated structural browsing
  - phone_decrypt  — Track 6B: ciphertext-fetch endpoint
  - invalidate     — Track 6C: user-tier invalidation/revalidation
"""

from . import (
    current,
    graph,
    invalidate,
    phone_decrypt,
    self_entity,
    timeline,
    topology,
    walk,
)

__all__ = [
    "current",
    "graph",
    "invalidate",
    "phone_decrypt",
    "self_entity",
    "timeline",
    "topology",
    "walk",
]
