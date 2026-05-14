"""
RHYME — Refraction and resonance for retrieval breadth.

# What RHYME is

A traversal-breadth mechanism. Like a bat's sonar, it locates
similarities in the substrate not by single-best-match but by
sweeping a region and surfacing every echo. The walker / ranker /
matcher gets multiple candidate forms of accuracy and can choose
among them — immediately or later (during a montage / match
session).

The same underlying mechanism handles a related case: near-duplicate
voice memos. If the operator records "I'm meeting Alice tomorrow"
twice within a short window, the second recording isn't a
duplicate to discard — it's a clarification of the first (different
prosody, different framing, possibly different intent). RHYME
treats them as a resonance pair: both surface together, and the
disambiguating signal lives in their difference.

# What this module ships

  - `rhyme_score(a, b)` — a similarity-with-difference score that
    penalizes pure duplicates (no information) AND pure
    independents (no resonance), favoring near-duplicates that
    add disambiguating signal.
  - `RhymeCluster` — a set of candidates that resonate with each
    other; the cluster is the unit of presentation rather than a
    single best match.
  - `cluster_by_rhyme(candidates, threshold)` — the bat-sonar
    sweep: takes a candidate list, returns clusters of resonant
    candidates.
  - `near_duplicate_pair(a, b, window_ms)` — detects the voice-memo
    case: same content, close-in-time, treated as a clarification
    pair.

# How RHYME differs from cosine-similarity retrieval

Cosine similarity returns the SINGLE best match per query. RHYME
returns CLUSTERS of resonant candidates, preserving the
disambiguating differences within each cluster. The downstream
ranker / matcher can pick from the cluster, and the user-facing
montage can show them together (a "match session always carries
options").

Spec: described conversationally by the user (May 6, 2026 thread):

  > RHYME is the mechanism for capturing refraction and resonance,
  > it's like sonar used by bats, it locates similarity which aids
  > traversal and helps build meaning that is more accurate but
  > allows for breadth that can allow multiple forms of accuracy
  > to choose from immediately or later.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


# =============================================================================
# Constants
# =============================================================================


DEFAULT_RHYME_THRESHOLD = 0.45
"""Pairs above this rhyme_score are treated as resonant. Below: too
independent for a cluster."""

DEFAULT_NEAR_DUPLICATE_WINDOW_MS = 5 * 60 * 1000
"""5 minutes. Captures within this window with high content overlap
are treated as a clarification pair (duplicate-as-resonance)."""

DEFAULT_DUPLICATE_CONTENT_THRESHOLD = 0.85
"""Content similarity above this counts as "near-duplicate" for the
voice-memo clarification case."""


# =============================================================================
# Rhyme score
# =============================================================================


def rhyme_score(
    a_text: str,
    b_text: str,
    *,
    a_signature: str | None = None,
    b_signature: str | None = None,
) -> float:
    """Return [0, 1] resonance score for two candidates.

    The score peaks for *near-duplicate* pairs: high content overlap
    plus some difference. Pure duplicates (identical) score lower
    because they add no information; pure independents score lower
    because they don't resonate.

    Concretely: rhyme = similarity * (1 - similarity^2) * 4, which
    has its maximum around similarity ≈ 0.577. (The familiar
    "Gini-like" peak that captures "similar but not the same".)

    Optional `a_signature` / `b_signature` are content-hash-like
    short identifiers; if both are provided and equal, the texts
    are treated as equivalent (e.g., re-captures of the same
    phrase) and we look at non-content difference.
    """
    if a_signature and b_signature and a_signature == b_signature:
        # Identical signatures = same content. Rhyme score depends
        # on how far the texts have actually diverged at the
        # surface (e.g., re-captures with different prosody).
        sim = _cheap_text_similarity(a_text, b_text)
        # Penalize identical text (no clarification signal); reward
        # textually-different-but-same-meaning rephrasings.
        return _rhyme_curve(sim)

    sim = _cheap_text_similarity(a_text, b_text)
    return _rhyme_curve(sim)


def _rhyme_curve(similarity: float) -> float:
    """Map [0, 1] similarity to [0, 1] rhyme score.

    Peaks around similarity = 1/sqrt(3) ≈ 0.577.
    """
    similarity = max(0.0, min(1.0, similarity))
    # Raw: 4 * sim * (1 - sim^2) peaks at sim = 1/sqrt(3).
    # Peak value = 8 / (3 * sqrt(3)) ≈ 1.5396; normalize by it.
    raw = 4.0 * similarity * (1.0 - similarity * similarity)
    peak = 8.0 / (3.0 * math.sqrt(3.0))
    return raw / peak


def _cheap_text_similarity(a: str, b: str) -> float:
    """Token-set Jaccard. Cheap, deterministic, no embedding needed."""
    if not a and not b:
        return 1.0
    a_tokens = set(_tokens(a))
    b_tokens = set(_tokens(b))
    if not a_tokens and not b_tokens:
        return 1.0
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return inter / union if union else 0.0


def _tokens(text: str) -> list[str]:
    return [t for t in text.lower().split() if t]


# =============================================================================
# RhymeCluster
# =============================================================================


@dataclass
class RhymeCluster:
    """A set of candidates that resonate with each other.

    The `representative` is the highest-coherence member (smallest
    average distance to other members). The full cluster is the
    presentation unit; downstream code can pick a single member or
    show them as a montage.
    """

    members: list[Any] = field(default_factory=list)
    """Opaque candidate objects. The cluster doesn't care what
    they are — it's keyed by the text/signature extractors passed
    to `cluster_by_rhyme`."""

    pairwise_scores: dict[tuple[int, int], float] = field(default_factory=dict)
    """Sparse: scores between member indices."""

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def average_resonance(self) -> float:
        if not self.pairwise_scores:
            return 0.0
        return sum(self.pairwise_scores.values()) / len(self.pairwise_scores)

    def representative_index(self) -> int | None:
        """Index of the member with highest mean resonance to others."""
        if not self.members:
            return None
        if len(self.members) == 1:
            return 0
        means = [0.0] * len(self.members)
        counts = [0] * len(self.members)
        for (i, j), s in self.pairwise_scores.items():
            means[i] += s
            means[j] += s
            counts[i] += 1
            counts[j] += 1
        means = [m / max(c, 1) for m, c in zip(means, counts)]
        best = max(range(len(self.members)), key=lambda i: means[i])
        return best


def cluster_by_rhyme(
    candidates: list[Any],
    *,
    text_extractor: Any,  # Callable[[Any], str]
    signature_extractor: Any | None = None,  # Callable[[Any], str | None]
    threshold: float = DEFAULT_RHYME_THRESHOLD,
) -> list[RhymeCluster]:
    """Sweep `candidates` and group them into resonance clusters.

    Two candidates land in the same cluster iff their rhyme_score
    is >= `threshold`. Transitive: if A rhymes with B and B with C,
    A and C end up in the same cluster.

    O(n^2) — fine for the scales the user-facing montage operates at
    (typically tens of candidates). For larger sweeps, gate by
    cluster_signature first, then RHYME within cluster.
    """
    n = len(candidates)
    if n == 0:
        return []

    # Union-find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    pairwise: dict[tuple[int, int], float] = {}
    for i in range(n):
        a_text = text_extractor(candidates[i])
        a_sig = signature_extractor(candidates[i]) if signature_extractor else None
        for j in range(i + 1, n):
            b_text = text_extractor(candidates[j])
            b_sig = (
                signature_extractor(candidates[j])
                if signature_extractor else None
            )
            score = rhyme_score(
                a_text, b_text, a_signature=a_sig, b_signature=b_sig,
            )
            pairwise[(i, j)] = score
            if score >= threshold:
                union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    clusters: list[RhymeCluster] = []
    for indices in groups.values():
        members = [candidates[i] for i in indices]
        sub_scores: dict[tuple[int, int], float] = {}
        index_map = {orig_i: new_i for new_i, orig_i in enumerate(indices)}
        for (i, j), s in pairwise.items():
            if i in index_map and j in index_map:
                sub_scores[(index_map[i], index_map[j])] = s
        clusters.append(RhymeCluster(
            members=members, pairwise_scores=sub_scores,
        ))

    # Sort clusters by size descending so callers see big ones first
    clusters.sort(key=lambda c: -c.size)
    return clusters


# =============================================================================
# Near-duplicate detection (voice-memo clarification)
# =============================================================================


@dataclass
class NearDuplicatePair:
    """Two candidates that are near-duplicates within a time window.

    The user's example: two voice memos that sound the same, recorded
    minutes apart. The second is treated as clarifying the first
    rather than as redundant. Both stay in the substrate; the
    canonicalizer can fold them into one assertion node with two
    derivations.
    """

    a: Any
    b: Any
    content_similarity: float
    time_distance_ms: int


def near_duplicate_pair(
    a: Any,
    b: Any,
    *,
    text_extractor: Any,  # Callable[[Any], str]
    time_extractor: Any,  # Callable[[Any], int]
    content_threshold: float = DEFAULT_DUPLICATE_CONTENT_THRESHOLD,
    window_ms: int = DEFAULT_NEAR_DUPLICATE_WINDOW_MS,
) -> NearDuplicatePair | None:
    """Return a NearDuplicatePair iff a/b qualify; else None."""
    sim = _cheap_text_similarity(text_extractor(a), text_extractor(b))
    if sim < content_threshold:
        return None
    dt = abs(time_extractor(a) - time_extractor(b))
    if dt > window_ms:
        return None
    return NearDuplicatePair(
        a=a, b=b, content_similarity=sim, time_distance_ms=dt,
    )


def find_near_duplicates(
    candidates: list[Any],
    *,
    text_extractor: Any,
    time_extractor: Any,
    content_threshold: float = DEFAULT_DUPLICATE_CONTENT_THRESHOLD,
    window_ms: int = DEFAULT_NEAR_DUPLICATE_WINDOW_MS,
) -> list[NearDuplicatePair]:
    """All near-duplicate pairs in a candidate list."""
    out: list[NearDuplicatePair] = []
    n = len(candidates)
    for i in range(n):
        for j in range(i + 1, n):
            pair = near_duplicate_pair(
                candidates[i], candidates[j],
                text_extractor=text_extractor,
                time_extractor=time_extractor,
                content_threshold=content_threshold,
                window_ms=window_ms,
            )
            if pair is not None:
                out.append(pair)
    return out


__all__ = [
    "DEFAULT_DUPLICATE_CONTENT_THRESHOLD",
    "DEFAULT_NEAR_DUPLICATE_WINDOW_MS",
    "DEFAULT_RHYME_THRESHOLD",
    "NearDuplicatePair",
    "RhymeCluster",
    "cluster_by_rhyme",
    "find_near_duplicates",
    "near_duplicate_pair",
    "rhyme_score",
]
