"""
Discourse-structure extraction and matching — R3 §9.5.

# What this addresses

R3 §9.5 splits matching layer 2 into three sub-levels:

  - 2a structural: assertion graph alone (already in
    `mempalace.federate.layers.derivation.PredicateFingerprint`).
  - 2b discourse: discourse-structure patterns — refinement chains,
    contradiction-resolution patterns, supports/opposes structures.
  - 2c full-derivation: full derivation graph with drawer embeddings.

The existing `DerivationGraphSimilarity` (layer 2b in the federate
pipeline) does generic graph-Jaccard over the CCGraph and treats all
edges as fungible. This module is additive: it extracts a
**typed-discourse-aware** signature and provides a similarity
function that weights different edge types by discourse role.

Three discourse patterns count as load-bearing per R3 §9.5:

  - **Refinement chains** — sequences of REFINES edges. Long chains
    indicate iterative thinking; short ones indicate decisive
    revision.
  - **Contradiction-resolution patterns** — CONTRADICTS edges
    paired with SUPERSEDES edges (the "knew-it-all-along" updating
    Conway describes). The pairing ratio is signature-relevant.
  - **Supports/opposes structures** — SUPPORTS and INHIBITS edges.
    The balance (more support vs more opposition) characterizes
    how someone navigates evidence.

This module produces a `DiscoursePattern` for any node, plus a
`compare_discourse_patterns` function for the matching kernel.

# Where this fits

Layer 2 in the federation pipeline can call
`extract_discourse_pattern(node_id)` for each candidate match and
`compare_discourse_patterns(local, remote)` to get a typed-similarity
score that's added to the existing graph-Jaccard signal.

For now, this module ships as a standalone primitive. Wiring it
into the layer 2b composite step is a follow-on integration.

Spec ref: integration_appendix_r3.md §9.5.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from ..schema.kinds import EdgeKind
from ..views import current as views


# =============================================================================
# Tunables
# =============================================================================


MAX_REFINEMENT_CHAIN_LENGTH = 16
"""Cap chain-walk depth so a cycle doesn't loop forever. Real
chains are typically 1-5 long; 16 is conservative."""

MAX_DISCOURSE_NEIGHBORHOOD = 64
"""Max number of edges to follow when sketching a node's discourse
neighborhood. Past this, the signature stops adding detail (the
extra edges are diminishing-returns for matching purposes)."""


# =============================================================================
# Output dataclass
# =============================================================================


@dataclass(frozen=True)
class DiscoursePattern:
    """Typed-discourse signature of one node's neighborhood.

    Captures the three R3 §9.5 patterns plus a few normalized
    derived metrics used by the comparison kernel.
    """

    node_id: str

    # ---- 1. Refinement chains -------------------------------------------
    # Walks REFINES edges. A "chain length" is the depth from the node
    # to a chain leaf (a node with no outgoing REFINES edge).
    refinement_chain_count: int = 0
    """How many distinct refinement chains this node participates in
    (counts the node itself if it has any REFINES edges out)."""

    refinement_chain_lengths: tuple[int, ...] = ()
    """Per-chain depth. Sorted ascending. Long chains = iterative
    refinement; short = decisive revision."""

    # ---- 2. Contradiction-resolution patterns ---------------------------
    contradiction_count: int = 0
    """CONTRADICTS edges incident to this node (in or out)."""

    supersedes_count: int = 0
    """SUPERSEDES edges (in or out)."""

    contradiction_resolution_ratio: float = 0.0
    """resolution_count / max(contradiction_count, 1).
    A node with many contradictions but few supersessions has
    unresolved tensions; one with high ratio has actively
    consolidated."""

    # ---- 3. Supports/opposes structures ---------------------------------
    supports_count: int = 0
    """SUPPORTS edges (out — this node supports something)."""
    supported_by_count: int = 0
    """SUPPORTS edges (in — this node is supported by something)."""

    inhibits_count: int = 0
    """INHIBITS edges (out)."""
    inhibited_by_count: int = 0
    """INHIBITS edges (in)."""

    support_vs_oppose_balance: float = 0.0
    """(supports_count + supported_by) - (inhibits_count + inhibited_by),
    normalized to [-1, 1] by total. Positive = navigates via
    confirming evidence; negative = navigates via opposition."""

    # ---- aggregate ------------------------------------------------------
    total_discourse_edges: int = 0
    """Sum of all discourse-edge counts. A node with 0 here doesn't
    participate in any discourse structure — pure structural matching
    only at layer 2a."""

    @property
    def has_discourse(self) -> bool:
        return self.total_discourse_edges > 0


# =============================================================================
# Extraction
# =============================================================================


def _outgoing_kind(node_id: str, kind: EdgeKind) -> list:
    edges = views.outgoing_edges(node_id, kind=kind)
    return edges or []


def _incoming_kind(node_id: str, kind: EdgeKind) -> list:
    edges = views.incoming_edges(node_id, kind=kind)
    return edges or []


def _walk_refinement_chain(start_id: str) -> int:
    """Walk REFINES edges out from start_id, returning the chain
    depth. Cycle-safe (depth-cap + visited set)."""
    depth = 0
    seen = {start_id}
    cursor = start_id
    while depth < MAX_REFINEMENT_CHAIN_LENGTH:
        out = _outgoing_kind(cursor, EdgeKind.REFINES)
        if not out:
            break
        nxt = out[0].target_node_id
        if nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
        depth += 1
    return depth


def extract_discourse_pattern(node_id: str) -> DiscoursePattern:
    """Build the discourse signature for one node.

    Walks the immediate edge neighborhood + REFINES chains. Cost is
    O(local edges + chain depth), bounded by the two MAX_* tunables.
    """
    # ---- 1. Refinement chains ------------------------------------------
    out_refines = _outgoing_kind(node_id, EdgeKind.REFINES)
    in_refines = _incoming_kind(node_id, EdgeKind.REFINES)
    chain_lengths: list[int] = []
    if out_refines:
        # This node refines something — walk forward from here
        chain_lengths.append(_walk_refinement_chain(node_id))
    # Each incoming REFINES is a chain of depth ≥1 ending at this node.
    # We don't walk backward to find the chain origin (that doubles
    # the work and rarely matters for matching); we record the count.
    for edge in in_refines:
        chain_lengths.append(1)
    chain_count = len(chain_lengths)

    # ---- 2. Contradiction-resolution -----------------------------------
    contra_out = _outgoing_kind(node_id, EdgeKind.CONTRADICTS)
    contra_in = _incoming_kind(node_id, EdgeKind.CONTRADICTS)
    super_out = _outgoing_kind(node_id, EdgeKind.SUPERSEDES)
    super_in = _incoming_kind(node_id, EdgeKind.SUPERSEDES)

    contradiction_count = len(contra_out) + len(contra_in)
    supersedes_count = len(super_out) + len(super_in)
    if contradiction_count > 0:
        ratio = supersedes_count / contradiction_count
    else:
        ratio = 0.0

    # ---- 3. Supports / inhibits ----------------------------------------
    sup_out = _outgoing_kind(node_id, EdgeKind.SUPPORTS)
    sup_in = _incoming_kind(node_id, EdgeKind.SUPPORTS)
    inh_out = _outgoing_kind(node_id, EdgeKind.INHIBITS)
    inh_in = _incoming_kind(node_id, EdgeKind.INHIBITS)

    supports_count = len(sup_out)
    supported_by_count = len(sup_in)
    inhibits_count = len(inh_out)
    inhibited_by_count = len(inh_in)

    support_total = supports_count + supported_by_count
    inhibit_total = inhibits_count + inhibited_by_count
    total_se = support_total + inhibit_total
    if total_se > 0:
        balance = (support_total - inhibit_total) / total_se
    else:
        balance = 0.0

    total_discourse_edges = (
        len(out_refines) + len(in_refines)
        + contradiction_count + supersedes_count
        + total_se
    )

    return DiscoursePattern(
        node_id=node_id,
        refinement_chain_count=chain_count,
        refinement_chain_lengths=tuple(sorted(chain_lengths)),
        contradiction_count=contradiction_count,
        supersedes_count=supersedes_count,
        contradiction_resolution_ratio=ratio,
        supports_count=supports_count,
        supported_by_count=supported_by_count,
        inhibits_count=inhibits_count,
        inhibited_by_count=inhibited_by_count,
        support_vs_oppose_balance=balance,
        total_discourse_edges=total_discourse_edges,
    )


# =============================================================================
# Comparison
# =============================================================================


def _bucket_chain_lengths(lengths: Iterable[int]) -> dict[str, int]:
    """Bucketize chain lengths into coarse bins for stable comparison.

    Bins: 0 (no chain), 1-2 (short), 3-5 (medium), 6+ (long).
    The exact lengths are noisy across palaces (one person's "chain
    of 3" is another's "chain of 4" depending on segmentation);
    bucket counts are stable.
    """
    buckets = {"none": 0, "short": 0, "medium": 0, "long": 0}
    for L in lengths:
        if L == 0:
            buckets["none"] += 1
        elif L <= 2:
            buckets["short"] += 1
        elif L <= 5:
            buckets["medium"] += 1
        else:
            buckets["long"] += 1
    return buckets


def _normalized_count_diff(a: int, b: int) -> float:
    """Returns 1.0 when counts match, decreasing toward 0 as they
    diverge. Symmetric. Uses the smaller-over-larger ratio."""
    if a == 0 and b == 0:
        return 1.0
    if a == 0 or b == 0:
        return 0.0
    return min(a, b) / max(a, b)


def compare_discourse_patterns(
    local: DiscoursePattern,
    remote: DiscoursePattern,
) -> dict[str, float]:
    """Per-component similarity scores + an aggregate.

    Returns a dict with keys:
      refinement_similarity, contradiction_similarity, support_similarity,
      aggregate. All in [0, 1].

    Aggregate is the equally-weighted mean. Callers can re-weight.
    """
    # Refinement chains: bucket-overlap Jaccard
    local_buckets = _bucket_chain_lengths(local.refinement_chain_lengths)
    remote_buckets = _bucket_chain_lengths(remote.refinement_chain_lengths)
    refine_sim = _bucket_jaccard(local_buckets, remote_buckets)

    # Contradiction resolution: similar resolution-ratio = similar style
    contra_sim = _normalized_count_diff(
        local.contradiction_count, remote.contradiction_count,
    )
    ratio_diff = abs(
        local.contradiction_resolution_ratio
        - remote.contradiction_resolution_ratio,
    )
    contra_ratio_sim = max(0.0, 1.0 - ratio_diff)
    contra_similarity = 0.5 * contra_sim + 0.5 * contra_ratio_sim

    # Support/oppose balance similarity
    bal_diff = abs(
        local.support_vs_oppose_balance - remote.support_vs_oppose_balance,
    )
    # bal_diff is in [0, 2] since each is in [-1, 1]. Normalize.
    support_sim = max(0.0, 1.0 - bal_diff / 2.0)

    # Aggregate
    components = (refine_sim, contra_similarity, support_sim)
    aggregate = sum(components) / len(components)

    return {
        "refinement_similarity": refine_sim,
        "contradiction_similarity": contra_similarity,
        "support_similarity": support_sim,
        "aggregate": aggregate,
    }


def _bucket_jaccard(a: dict[str, int], b: dict[str, int]) -> float:
    """Token-set Jaccard on (bucket_name, count) histograms.

    Treats two histograms as identical when both have zero entries
    (rather than dividing by zero or returning 0)."""
    keys = set(a.keys()) | set(b.keys())
    inter = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    union = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    if union == 0:
        return 1.0
    return inter / union


__all__ = [
    "MAX_DISCOURSE_NEIGHBORHOOD",
    "MAX_REFINEMENT_CHAIN_LENGTH",
    "DiscoursePattern",
    "compare_discourse_patterns",
    "extract_discourse_pattern",
]
