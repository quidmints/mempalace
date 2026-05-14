"""
Layer 3: substrate-level matching.

Per R3 §9.5 / Part 9.2: the most expensive and most exposing layer.
Operates over actual drawer substrate:

  - Verbatim text similarity (TF-IDF / BM25 over surfaces)
  - Paralinguistic similarity (the moved-to-interpretation prosodic features)
  - Full geometric similarity (high-dim semantic embeddings)

Runs ONLY if Layer 2 gate has passed AND the user-configured substrate-
verification setting permits. This layer always runs inside the sandbox
(LOCAL_ONLY privacy mode). The only data that exits is the score and
optional pointers (drawer_id, span) for substrate-verification.

Spec ref: R3 §9.5, R3 §9.3, Part 9.2.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ...stack.context import PrivacyMode, StackContext
from ...stack.step import BaseStep, StepManifest, StepResult


# =============================================================================
# Similarity primitives (kept simple here; production uses real BM25 + ANN)
# =============================================================================


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in text.split() if t]


def _bm25_term_frequency_overlap(local_text: str, remote_text: str) -> float:
    """Cheap-and-cheerful overlap: weighted intersection of multiset terms."""
    if not local_text or not remote_text:
        return 0.0
    local_terms = Counter(_tokenize(local_text))
    remote_terms = Counter(_tokenize(remote_text))
    common = local_terms & remote_terms
    if not common:
        return 0.0
    overlap = sum(common.values())
    total_local = sum(local_terms.values())
    total_remote = sum(remote_terms.values())
    return overlap / max(total_local, total_remote)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# =============================================================================
# Verbatim sub-step
# =============================================================================


class VerbatimSimilarity(BaseStep):
    name = "matching.layer3.verbatim"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("local_drawer_texts", "remote_drawer_texts"),
            outputs=("layer3_verbatim_score",),
            requires_attestation=True,  # touches substrate; needs attestation
            requires_sandbox=True,
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        if ctx.privacy_mode != PrivacyMode.LOCAL_ONLY:
            return StepResult(
                success=False,
                error="VerbatimSimilarity requires LOCAL_ONLY privacy mode",
            )
        local_texts: list[str] = ctx.get_input("local_drawer_texts", [])
        remote_texts: list[str] = ctx.get_input("remote_drawer_texts", [])
        if not local_texts or not remote_texts:
            return StepResult(
                success=True,
                outputs={"layer3_verbatim_score": 0.0},
            )
        # Best-pair score: max overlap across local × remote drawers
        max_score = 0.0
        for l in local_texts:
            for r in remote_texts:
                s = _bm25_term_frequency_overlap(l, r)
                if s > max_score:
                    max_score = s
        return StepResult(
            success=True,
            outputs={"layer3_verbatim_score": max_score},
        )


# =============================================================================
# Semantic-embedding sub-step
# =============================================================================


class SemanticSimilarity(BaseStep):
    name = "matching.layer3.semantic"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=("local_drawer_embeddings", "remote_drawer_embeddings"),
            outputs=("layer3_semantic_score",),
            requires_attestation=True,
            requires_sandbox=True,
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        if ctx.privacy_mode != PrivacyMode.LOCAL_ONLY:
            return StepResult(
                success=False,
                error="SemanticSimilarity requires LOCAL_ONLY privacy mode",
            )
        local_embs: list[list[float]] = ctx.get_input("local_drawer_embeddings", [])
        remote_embs: list[list[float]] = ctx.get_input("remote_drawer_embeddings", [])
        if not local_embs or not remote_embs:
            return StepResult(success=True, outputs={"layer3_semantic_score": 0.0})
        max_cos = 0.0
        for l in local_embs:
            for r in remote_embs:
                c = _cosine(l, r)
                if c > max_cos:
                    max_cos = c
        # Map cosine [-1,1] to [0,1]
        score = max(0.0, min(1.0, (max_cos + 1.0) / 2.0))
        return StepResult(success=True, outputs={"layer3_semantic_score": score})


# =============================================================================
# Paralinguistic sub-step (moved to interpretation per R3 §1.3)
# =============================================================================


class ParalinguisticSimilarity(BaseStep):
    """Compares paralinguistic interpretations across palaces.

    Per R3 §1.3, paralinguistic features are interpretation-layer (not
    substrate). Each palace's miner has assigned them; this step
    compares the assignments.
    """

    name = "matching.layer3.paralinguistic"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "local_paralinguistic_features",
                "remote_paralinguistic_features",
            ),
            outputs=("layer3_paralinguistic_score",),
            requires_sandbox=True,
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        local: dict[str, float] = ctx.get_input("local_paralinguistic_features", {})
        remote: dict[str, float] = ctx.get_input("remote_paralinguistic_features", {})
        common = set(local.keys()) & set(remote.keys())
        if not common:
            return StepResult(success=True, outputs={"layer3_paralinguistic_score": 0.0})
        # Cosine over the shared dimensions
        l_vec = [local[k] for k in sorted(common)]
        r_vec = [remote[k] for k in sorted(common)]
        cos = _cosine(l_vec, r_vec)
        score = max(0.0, min(1.0, (cos + 1.0) / 2.0))
        return StepResult(
            success=True,
            outputs={"layer3_paralinguistic_score": score},
        )


# =============================================================================
# Layer 3 composite
# =============================================================================


class SubstrateLayer(BaseStep):
    """Aggregates verbatim, semantic, paralinguistic into layer-3 score."""

    name = "matching.layer3.substrate"

    def declares(self) -> StepManifest:
        return StepManifest(
            name=self.name,
            inputs_required=(
                "layer3_verbatim_score",
                "layer3_semantic_score",
                "layer3_paralinguistic_score",
            ),
            outputs=("layer3_score",),
            requires_sandbox=True,
        )

    def _run_sync(self, ctx: StackContext) -> StepResult:
        v = float(ctx.get_output("layer3_verbatim_score", 0.0))
        s = float(ctx.get_output("layer3_semantic_score", 0.0))
        p = float(ctx.get_output("layer3_paralinguistic_score", 0.0))
        # Weighted: semantic dominates, verbatim and paralinguistic refine
        score = 0.55 * s + 0.30 * v + 0.15 * p
        return StepResult(
            success=True,
            outputs={"layer3_score": score},
        )


__all__ = [
    "ParalinguisticSimilarity",
    "SemanticSimilarity",
    "SubstrateLayer",
    "VerbatimSimilarity",
]
