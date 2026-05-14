"""
Layer 2: derivation-chain compatibility.

Per R3 §9.5 / Part 9.2: KisMATH-style CCGraph extraction over the
assertion → drawer derivation chains. Only runs if Layer 1 gate passed.

Sub-levels (fine-grained gating per R3 §9.5):

  2a. Assertion-set overlap: how many predicates do we both ground in
      our drawers? Cheap; uses canonicalized predicate fingerprints.

  2b. Derivation-graph similarity: KisMATH CCGraph (cycle-condensation)
      structure of our derivation DAG vs theirs. Measures how similar
      our reasoning structures are.

  2c. R-path overlap: shared inference paths from substrate to assertion.
      Most expensive of the three; runs only if 2a + 2b clear their gates.

The CCGraph extraction here is a structural sketch — we don't share
the actual assertion text, only the graph topology and predicate
fingerprints.

Spec ref: R3 §9.5, Part 9.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...stack.context import StackContext
from ...stack.step import BaseStep, StepManifest, StepResult


# =============================================================================
# CCGraph sketch primitives
# =============================================================================


@dataclass
class CCGraphSketch:
    """A KisMATH-style condensation-graph sketch.

    nodes: list of strongly-connected component IDs (after cycle condensation)
    edges: (src_id, dst_id) tuples between components
    fork_components: components flagged as fork-points (high-significance
                     decision nodes); R3 §9.6.

    The sketch shares only structural information — no assertion content.
    """

    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    fork_components: tuple[str, ...]


def _ccgraph_jaccard(a: CCGraphSketch, b: CCGraphSketch) -> dict[str, float]:
    """Compare two CCGraph sketches structurally.

    Returns dict with:
      - node_jaccard: |A∩B|/|A∪B| over node IDs
      - edge_jaccard: same over edges
      - fork_overlap: same over fork components (R3 §9.6 weighted)
    """
    nodes_a, nodes_b = set(a.nodes), set(b.nodes)
    edges_a, edges_b = set(a.edges), set(b.edges)
    forks_a, forks_b = set(a.fork_components), set(b.fork_components)

    def jaccard(s1, s2):
        if not s1 and not s2:
            return 0.0
        u = s1 | s2
        if not u:
            return 0.0
        return len(s1 & s2) / len(u)

    return {
        "node_jaccard": jaccard(nodes_a, nodes_b),
        "edge_jaccard": jaccard(edges_a, edges_b),
        "fork_overlap": jaccard(forks_a, forks_b),
    }


# =============================================================================
# Sub-level 2a: assertion-set overlap
# =============================================================================


class AssertionOverlap(BaseStep):
    """Layer 2a: predicate-fingerprint overlap."""

    name = "matching.layer2a.assertion_overlap"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "local_predicate_fingerprints",
                "remote_predicate_fingerprints",
                "layer1_gate_passed",
            ),
            outputs=("layer2a_score", "layer2a_gate_passed"),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        if not ctx.get_input("layer1_gate_passed", False) and not ctx.get_output("layer1_gate_passed", False):
            return StepResult(
                success=True,
                outputs={"layer2a_score": 0.0, "layer2a_gate_passed": False},
            )

        local = set(ctx.get_input("local_predicate_fingerprints", []))
        remote = set(ctx.get_input("remote_predicate_fingerprints", []))
        if not local and not remote:
            score = 0.0
        else:
            union = local | remote
            score = len(local & remote) / len(union) if union else 0.0
        return StepResult(
            success=True,
            outputs={
                "layer2a_score": score,
                "layer2a_gate_passed": score >= 0.2,
            },
        )


# =============================================================================
# Sub-level 2b: derivation-graph similarity
# =============================================================================


class DerivationGraphSimilarity(BaseStep):
    """Layer 2b: CCGraph structural comparison + typed-discourse signal.

    Per R3 §9.5, the discourse-structure patterns (refinement chains,
    contradiction-resolution, supports/opposes) carry signature-relevant
    information that generic graph-Jaccard misses. When the inputs
    `local_discourse_pattern` and `remote_discourse_pattern` are
    available (caller-provided), this step blends the typed-discourse
    similarity into the layer 2b score.

    When the discourse patterns aren't provided, falls back to the
    pure CCGraph signal.
    """

    name = "matching.layer2b.derivation_graph"

    # Default blend weights when discourse signal is present:
    # 70% generic CCGraph, 30% typed discourse. Tunable per palace.
    DEFAULT_CCGRAPH_WEIGHT = 0.7
    DEFAULT_DISCOURSE_WEIGHT = 0.3

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "local_ccgraph_sketch",
                "remote_ccgraph_sketch",
                "layer2a_gate_passed",
            ),
            outputs=("layer2b_score", "layer2b_breakdown", "layer2b_gate_passed"),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        if not ctx.get_output("layer2a_gate_passed", False) and not ctx.get_input("layer2a_gate_passed", False):
            return StepResult(
                success=True,
                outputs={
                    "layer2b_score": 0.0,
                    "layer2b_breakdown": {},
                    "layer2b_gate_passed": False,
                },
            )

        local = ctx.get_input("local_ccgraph_sketch")
        remote = ctx.get_input("remote_ccgraph_sketch")
        if local is None or remote is None:
            return StepResult(
                success=False,
                error="missing CCGraph sketches",
            )

        breakdown = _ccgraph_jaccard(local, remote)
        # KisMATH §9.6: forks weight extra (decision-points are signature)
        ccgraph_score = (
            0.4 * breakdown["node_jaccard"]
            + 0.3 * breakdown["edge_jaccard"]
            + 0.3 * breakdown["fork_overlap"]
        )

        # R3 §9.5 — typed-discourse signal (refinement chains,
        # contradiction-resolution patterns, supports/opposes balance).
        # When the caller has populated discourse patterns for the local
        # and remote nodes, blend the signal in.
        local_dp = ctx.get_input("local_discourse_pattern")
        remote_dp = ctx.get_input("remote_discourse_pattern")
        discourse_breakdown: dict = {}
        if local_dp is not None and remote_dp is not None:
            from ..discourse import compare_discourse_patterns
            discourse_breakdown = compare_discourse_patterns(
                local_dp, remote_dp,
            )
            score = (
                self.DEFAULT_CCGRAPH_WEIGHT * ccgraph_score
                + self.DEFAULT_DISCOURSE_WEIGHT
                * discourse_breakdown["aggregate"]
            )
            breakdown["discourse"] = discourse_breakdown
        else:
            # No discourse pattern available — fall back to pure CCGraph
            score = ccgraph_score

        return StepResult(
            success=True,
            outputs={
                "layer2b_score": score,
                "layer2b_breakdown": breakdown,
                "layer2b_gate_passed": score >= 0.25,
            },
        )


# =============================================================================
# Sub-level 2c: R-path overlap
# =============================================================================


class RPathOverlap(BaseStep):
    """Layer 2c: shared inference-path overlap."""

    name = "matching.layer2c.r_path"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "local_r_paths",
                "remote_r_paths",
                "layer2b_gate_passed",
            ),
            outputs=("layer2c_score", "layer2c_gate_passed"),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        if not ctx.get_output("layer2b_gate_passed", False) and not ctx.get_input("layer2b_gate_passed", False):
            return StepResult(
                success=True,
                outputs={"layer2c_score": 0.0, "layer2c_gate_passed": False},
            )

        # R-paths are sequences of typed edges; we sketch them as tuples
        local_paths = {tuple(p) for p in ctx.get_input("local_r_paths", [])}
        remote_paths = {tuple(p) for p in ctx.get_input("remote_r_paths", [])}
        if not local_paths and not remote_paths:
            score = 0.0
        else:
            union = local_paths | remote_paths
            score = len(local_paths & remote_paths) / len(union) if union else 0.0

        return StepResult(
            success=True,
            outputs={
                "layer2c_score": score,
                "layer2c_gate_passed": score >= 0.15,
            },
        )


# =============================================================================
# Layer 2 composite
# =============================================================================


class DerivationLayer(BaseStep):
    """Aggregates 2a, 2b, 2c into a single layer-2 score."""

    name = "matching.layer2.derivation"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "layer2a_score",
                "layer2b_score",
                "layer2c_score",
            ),
            outputs=("layer2_score", "layer2_gate_passed"),
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        s_a = float(ctx.get_output("layer2a_score", 0.0))
        s_b = float(ctx.get_output("layer2b_score", 0.0))
        s_c = float(ctx.get_output("layer2c_score", 0.0))
        # Geometric mean: each sub-level must contribute
        s_clamped = [max(0.05, x) for x in (s_a, s_b, s_c)]
        product = s_clamped[0] * s_clamped[1] * s_clamped[2]
        score = product ** (1.0 / 3.0)
        return StepResult(
            success=True,
            outputs={
                "layer2_score": score,
                "layer2_gate_passed": score >= 0.3,
            },
        )


__all__ = [
    "AssertionOverlap",
    "CCGraphSketch",
    "DerivationGraphSimilarity",
    "DerivationLayer",
    "RPathOverlap",
]
