"""
Layer 1: structural triangulation.

Per R3 §9.5 / Part 9.2 (Layer 1): the cheapest, broadest match. Operates
purely on structural sketches that don't expose substrate:

  - MinHash KG sketches (theme/period-level shape)
  - Schema fingerprint overlap (do we share assertion schemas?)
  - Velocity coupling tensor (similar bursting patterns)

This layer runs FIRST on every match request. If structural similarity
is below a threshold, we abort here and never expose deeper data.

Output: a similarity score in [0, 1] + per-axis breakdown. Layer 2/3
proceed only if score crosses the gate (default 0.4).

Spec ref: R3 §9.5, Part 9.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...stack.context import StackContext
from ...stack.step import BaseStep, StepManifest, StepResult


# =============================================================================
# Sketch comparison primitives
# =============================================================================


def _minhash_jaccard(a: list[int], b: list[int]) -> float:
    """Estimate Jaccard similarity from two equal-length MinHash sketches."""
    if not a or not b or len(a) != len(b):
        return 0.0
    matches = sum(1 for x, y in zip(a, b, strict=True) if x == y)
    return matches / len(a)


def _set_jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _velocity_correlation(va: dict[str, float], vb: dict[str, float]) -> float:
    """Pearson-ish correlation between two velocity dicts."""
    common_keys = set(va.keys()) & set(vb.keys())
    if not common_keys:
        return 0.0
    pa = [va[k] for k in common_keys]
    pb = [vb[k] for k in common_keys]
    n = len(common_keys)
    mean_a = sum(pa) / n
    mean_b = sum(pb) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(pa, pb))
    var_a = sum((a - mean_a) ** 2 for a in pa)
    var_b = sum((b - mean_b) ** 2 for b in pb)
    if var_a == 0 or var_b == 0:
        return 0.0
    return cov / (var_a * var_b) ** 0.5


# =============================================================================
# Layer 1 step
# =============================================================================


@dataclass
class StructuralLayerOutputs:
    score: float
    minhash_jaccard: float
    schema_jaccard: float
    velocity_corr: float
    gate_passed: bool


class StructuralMatchingLayer(BaseStep):
    """Layer 1: structural-only triangulation."""

    name = "matching.layer1.structural"

    def __init__(self, *, gate_threshold: float = 0.4) -> None:
        self._gate = gate_threshold

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            version="0.1.0",
            inputs_required=(
                "local_minhash_sketch",
                "remote_minhash_sketch",
                "local_schema_fingerprints",
                "remote_schema_fingerprints",
                "local_velocity_summary",
                "remote_velocity_summary",
            ),
            outputs=("layer1_score", "layer1_breakdown", "layer1_gate_passed"),
            requires_attestation=False,  # this layer doesn't see substrate
            description="Structural triangulation: minhash + schema + velocity",
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        local_minhash = ctx.get_input("local_minhash_sketch", [])
        remote_minhash = ctx.get_input("remote_minhash_sketch", [])
        local_schemas = set(ctx.get_input("local_schema_fingerprints", []))
        remote_schemas = set(ctx.get_input("remote_schema_fingerprints", []))
        local_velocity = ctx.get_input("local_velocity_summary", {})
        remote_velocity = ctx.get_input("remote_velocity_summary", {})

        mh = _minhash_jaccard(local_minhash, remote_minhash)
        sj = _set_jaccard(local_schemas, remote_schemas)
        vc = _velocity_correlation(local_velocity, remote_velocity)

        # Weighted combination: minhash dominates, schemas and velocity refine
        score = 0.6 * mh + 0.25 * sj + 0.15 * max(0.0, vc)
        gate_passed = score >= self._gate

        outputs = StructuralLayerOutputs(
            score=score,
            minhash_jaccard=mh,
            schema_jaccard=sj,
            velocity_corr=vc,
            gate_passed=gate_passed,
        )

        return StepResult(
            success=True,
            outputs={
                "layer1_score": score,
                "layer1_breakdown": {
                    "minhash_jaccard": mh,
                    "schema_jaccard": sj,
                    "velocity_corr": vc,
                },
                "layer1_gate_passed": gate_passed,
            },
        )


__all__ = ["StructuralLayerOutputs", "StructuralMatchingLayer"]
