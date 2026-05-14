"""
Multi-domain canonicalizer.

Per R3 §4: seed canonicals → fast-path exact match → slow-path
embedding similarity → threshold collapse → open-world novel
acceptance → cache.

Domains and thresholds (R3 §4.2):

  predicates       0.85
  memory_types     0.90
  schema_names     0.78
  entity_aliases   0.92
  period_names     0.80
  theme_names      0.85
  goal_markers     0.75

Novel-canonical promotion (R3 §4.3):
  - below-threshold inputs go to a candidate pool
  - pool clusters at threshold + 0.05
  - cluster promotes when >= 3 members AND stable across >= 2 Class 2 passes
  - promotion is recorded as an event with cluster members

Reversibility (R3 §4.5): all canonicalization decisions are events;
wrong collapses can be reverted via `canonicalization_reverted`.

Spec ref: R3 §4.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .log.client import LogClient, get_default_client
from .schema.events import (
    CanonicalPromoted,
    CanonicalRejected,
    CanonicalizationReverted,
)
from .schema.identifiers import make_event_id_log


# =============================================================================
# Domain configuration
# =============================================================================


class CanonDomain(str, Enum):
    PREDICATES = "predicates"
    MEMORY_TYPES = "memory_types"
    SCHEMA_NAMES = "schema_names"
    ENTITY_ALIASES = "entity_aliases"
    PERIOD_NAMES = "period_names"
    THEME_NAMES = "theme_names"
    GOAL_MARKERS = "goal_markers"


DEFAULT_THRESHOLDS: dict[CanonDomain, float] = {
    CanonDomain.PREDICATES: 0.85,
    CanonDomain.MEMORY_TYPES: 0.90,
    CanonDomain.SCHEMA_NAMES: 0.78,
    CanonDomain.ENTITY_ALIASES: 0.92,
    CanonDomain.PERIOD_NAMES: 0.80,
    CanonDomain.THEME_NAMES: 0.85,
    CanonDomain.GOAL_MARKERS: 0.75,
}

# Cluster-formation threshold sits above the canonicalization threshold
CLUSTER_THRESHOLD_OFFSET = 0.05

# Novel-promotion conditions
DEFAULT_PROMOTION_MIN_MEMBERS = 3
DEFAULT_PROMOTION_MIN_STABLE_PASSES = 2


# =============================================================================
# Embedding callback
# =============================================================================


# Caller injects an embedder; we don't pin a specific model here.
EmbedderFn = Callable[[str], list[float]]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# =============================================================================
# Per-domain registry of canonicals + cache
# =============================================================================


@dataclass
class CanonicalEntry:
    """One accepted canonical."""

    canonical_id: str                          # e.g. "pred_loves"
    surface: str                               # "loves"
    embedding: list[float] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    promoted_at_ms: int = 0
    domain: CanonDomain = CanonDomain.PREDICATES
    member_count: int = 1                      # for cluster-promoted canonicals


@dataclass
class CandidateClusterMember:
    """A surface form being held in the candidate pool."""

    surface: str
    embedding: list[float]
    seen_in_passes: set[str] = field(default_factory=set)
    seen_at_ms: int = 0


@dataclass
class CandidateCluster:
    """A cluster of similar pending surface forms in the candidate pool."""

    cluster_id: str
    domain: CanonDomain
    centroid: list[float]
    members: list[CandidateClusterMember] = field(default_factory=list)
    first_seen_ms: int = 0
    last_seen_ms: int = 0


@dataclass
class CanonResolveResult:
    """Result of resolving an input surface."""

    canonical_id: str | None
    surface: str
    matched_existing: bool
    similarity: float
    queued_in_cluster_id: str | None = None
    novel_proposed_canonical_id: str | None = None


# =============================================================================
# Canonicalizer
# =============================================================================


class Canonicalizer:
    """Multi-domain canonicalizer with novel-canonical promotion."""

    def __init__(
        self,
        *,
        thresholds: dict[CanonDomain, float] | None = None,
        embedder: EmbedderFn | None = None,
        log: LogClient | None = None,
        promotion_min_members: int = DEFAULT_PROMOTION_MIN_MEMBERS,
        promotion_min_passes: int = DEFAULT_PROMOTION_MIN_STABLE_PASSES,
    ) -> None:
        self._thresholds = dict(thresholds or DEFAULT_THRESHOLDS)
        self._embedder = embedder
        self._log = log
        self._min_members = promotion_min_members
        self._min_passes = promotion_min_passes

        # state per domain
        self._canonicals: dict[CanonDomain, list[CanonicalEntry]] = {
            d: [] for d in CanonDomain
        }
        # exact-surface fast-path cache
        self._exact_index: dict[CanonDomain, dict[str, CanonicalEntry]] = {
            d: {} for d in CanonDomain
        }
        # candidate pools
        self._clusters: dict[CanonDomain, list[CandidateCluster]] = {
            d: [] for d in CanonDomain
        }
        # reverted canonical_ids
        self._reverted: set[str] = set()

        self._lock = threading.Lock()

    # ---- seeds ------------------------------------------------------------

    def seed(
        self,
        domain: CanonDomain,
        canonicals: Iterable[tuple[str, str, list[float] | None]],
    ) -> None:
        """Seed initial canonicals: iterable of (canonical_id, surface, embedding)."""
        with self._lock:
            for cid, surface, emb in canonicals:
                entry = CanonicalEntry(
                    canonical_id=cid,
                    surface=surface,
                    embedding=list(emb or []),
                    domain=domain,
                    promoted_at_ms=int(time.time() * 1000),
                )
                self._canonicals[domain].append(entry)
                self._exact_index[domain][surface.lower().strip()] = entry

    # ---- resolve ----------------------------------------------------------

    def resolve(
        self,
        domain: CanonDomain,
        surface: str,
        *,
        pass_id: str = "",
    ) -> CanonResolveResult:
        """Resolve a surface form against the canonical set for a domain.

        Pipeline (R3 §4.1):
          1. fast-path exact match
          2. slow-path embedding similarity
          3. threshold collapse if max similarity ≥ threshold
          4. otherwise queue in candidate pool, return novel-proposed id
        """
        s_norm = surface.lower().strip()
        threshold = self._thresholds.get(domain, 0.85)

        with self._lock:
            # 1. fast path
            existing = self._exact_index[domain].get(s_norm)
            if existing is not None and existing.canonical_id not in self._reverted:
                return CanonResolveResult(
                    canonical_id=existing.canonical_id,
                    surface=surface,
                    matched_existing=True,
                    similarity=1.0,
                )

        # 2. slow path
        emb: list[float] = []
        if self._embedder is not None:
            emb = self._embedder(surface)

        with self._lock:
            best: CanonicalEntry | None = None
            best_sim = 0.0
            if emb:
                for cand in self._canonicals[domain]:
                    if cand.canonical_id in self._reverted:
                        continue
                    if not cand.embedding:
                        continue
                    sim = _cosine(emb, cand.embedding)
                    if sim > best_sim:
                        best_sim = sim
                        best = cand
            if best is not None and best_sim >= threshold:
                # 3. threshold collapse — register alias
                best.aliases.append(surface)
                self._exact_index[domain][s_norm] = best
                return CanonResolveResult(
                    canonical_id=best.canonical_id,
                    surface=surface,
                    matched_existing=True,
                    similarity=best_sim,
                )

            # 4. below threshold → candidate pool
            cluster = self._enqueue_candidate_locked(
                domain, surface, emb, pass_id, threshold,
            )
            return CanonResolveResult(
                canonical_id=None,
                surface=surface,
                matched_existing=False,
                similarity=best_sim,
                queued_in_cluster_id=cluster.cluster_id,
                novel_proposed_canonical_id=cluster.cluster_id,
            )

    # ---- candidate pool ---------------------------------------------------

    def _enqueue_candidate_locked(
        self,
        domain: CanonDomain,
        surface: str,
        embedding: list[float],
        pass_id: str,
        threshold: float,
    ) -> CandidateCluster:
        """Place a below-threshold surface into a candidate cluster.

        Cluster join uses threshold + CLUSTER_THRESHOLD_OFFSET (R3 §4.3).
        """
        now = int(time.time() * 1000)
        cluster_threshold = threshold + CLUSTER_THRESHOLD_OFFSET

        # Look for an existing cluster the surface can join
        chosen: CandidateCluster | None = None
        if embedding:
            best_sim = 0.0
            for cl in self._clusters[domain]:
                sim = _cosine(embedding, cl.centroid)
                if sim >= cluster_threshold and sim > best_sim:
                    best_sim = sim
                    chosen = cl

        member = CandidateClusterMember(
            surface=surface,
            embedding=list(embedding),
            seen_in_passes={pass_id} if pass_id else set(),
            seen_at_ms=now,
        )
        if chosen is None:
            cluster_id = f"cand_{domain.value}_{len(self._clusters[domain]) + 1}"
            chosen = CandidateCluster(
                cluster_id=cluster_id,
                domain=domain,
                centroid=list(embedding) or [],
                members=[member],
                first_seen_ms=now,
                last_seen_ms=now,
            )
            self._clusters[domain].append(chosen)
        else:
            chosen.members.append(member)
            chosen.last_seen_ms = now
            # update centroid as running mean
            if chosen.centroid and embedding and len(chosen.centroid) == len(embedding):
                n = len(chosen.members)
                chosen.centroid = [
                    (c * (n - 1) + e) / n
                    for c, e in zip(chosen.centroid, embedding, strict=True)
                ]
        if pass_id:
            for m in chosen.members:
                if m.surface == surface:
                    m.seen_in_passes.add(pass_id)
        return chosen

    # ---- promotion --------------------------------------------------------

    def check_promotions(
        self,
        domain: CanonDomain | None = None,
    ) -> list[CanonicalEntry]:
        """Promote any cluster meeting the conditions to a canonical.

        Conditions (R3 §4.3):
          - cluster has >= promotion_min_members members
          - cluster has been stable across >= promotion_min_passes
            distinct Class 2 passes (i.e. seen_in_passes union >= N)

        All emitted CanonicalPromoted events are framed as a single
        batch (consumer "canonicalizer.check_promotions") so a torn
        promotion run is recoverable.
        """
        promoted: list[CanonicalEntry] = []
        domains = [domain] if domain is not None else list(CanonDomain)
        log_client = self._log or get_default_client()

        # First pass: figure out what would be promoted (without
        # mutating state). This lets us avoid opening an empty batch
        # in the common case where nothing meets the threshold.
        candidates_to_promote: list[tuple[CanonDomain, CandidateCluster]] = []
        with self._lock:
            for d in domains:
                for cl in self._clusters[d]:
                    distinct_passes = set()
                    for m in cl.members:
                        distinct_passes.update(m.seen_in_passes)
                    if (
                        len(cl.members) >= self._min_members
                        and len(distinct_passes) >= self._min_passes
                    ):
                        candidates_to_promote.append((d, cl))

        if not candidates_to_promote:
            return promoted

        with log_client.batch(
            "canonicalizer.check_promotions",
            expected_count=len(candidates_to_promote),
            input_summary={
                "domains": [d.value for d in domains],
                "candidate_count": len(candidates_to_promote),
            },
            actor="canonicalizer",
        ) as bh:
            with self._lock:
                # Per-domain rebuild of cluster list, only including
                # surviving (non-promoted) clusters.
                domains_touched: set[CanonDomain] = set()
                surviving_by_domain: dict[CanonDomain, list[CandidateCluster]] = {}

                # Identify promoted cluster ids per domain
                promoted_cluster_ids_by_domain: dict[CanonDomain, set[str]] = {}
                for d, cl in candidates_to_promote:
                    domains_touched.add(d)
                    promoted_cluster_ids_by_domain.setdefault(
                        d, set(),
                    ).add(cl.cluster_id)

                for d in domains_touched:
                    surviving_by_domain[d] = [
                        c for c in self._clusters[d]
                        if c.cluster_id not in promoted_cluster_ids_by_domain[d]
                    ]

                # Now do the actual promotion + event emission
                for d, cl in candidates_to_promote:
                    canonical_id = f"{d.value[:4]}_{cl.cluster_id}"
                    from collections import Counter
                    c = Counter(m.surface for m in cl.members)
                    canonical_surface = c.most_common(1)[0][0]
                    entry = CanonicalEntry(
                        canonical_id=canonical_id,
                        surface=canonical_surface,
                        embedding=list(cl.centroid),
                        aliases=[m.surface for m in cl.members],
                        promoted_at_ms=int(time.time() * 1000),
                        domain=d,
                        member_count=len(cl.members),
                    )
                    self._canonicals[d].append(entry)
                    for m in cl.members:
                        self._exact_index[d][m.surface.lower().strip()] = entry
                    promoted.append(entry)
                    bh.append(CanonicalPromoted(
                        event_id=make_event_id_log(entry.promoted_at_ms),
                        recorded_at=entry.promoted_at_ms,
                        actor="canonicalizer",
                        domain=d.value,
                        canonical_form=canonical_surface,
                        cluster_member_surfaces=[m.surface for m in cl.members],
                        promoted_by="miner",
                    ))

                for d, surviving in surviving_by_domain.items():
                    self._clusters[d] = surviving
        return promoted

    def reject_cluster(
        self,
        domain: CanonDomain,
        cluster_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """Reject a cluster (drop it from the candidate pool)."""
        log_client = self._log or get_default_client()
        with self._lock:
            for i, cl in enumerate(self._clusters[domain]):
                if cl.cluster_id == cluster_id:
                    del self._clusters[domain][i]
                    now = int(time.time() * 1000)
                    log_client.append(CanonicalRejected(
                        event_id=make_event_id_log(now),
                        recorded_at=now,
                        actor="canonicalizer",
                        domain=domain.value,
                        candidate_id=cluster_id,
                        rejected_by="user",
                        reason=reason,
                    ))
                    return True
        return False

    # ---- reversibility ----------------------------------------------------

    def revert(
        self,
        canonical_id: str,
        *,
        reason: str = "",
        new_canonical: str | None = None,
    ) -> bool:
        """Revert a canonicalization decision (R3 §4.5).

        Marks the canonical as reverted; subsequent resolve() calls
        won't match against it. Members of the reverted canonical can
        be re-canonicalized into different canonicals.

        `reason` is captured in actor metadata (not on the event itself
        — schema's CanonicalizationReverted carries only the mapping).
        """
        log_client = self._log or get_default_client()
        # Find the surface form for the reverted canonical (so we can
        # populate the schema-required surface_form field). Domain is
        # discoverable from the entry.
        with self._lock:
            self._reverted.add(canonical_id)
            reverted_domain = ""
            reverted_surface = ""
            for d in CanonDomain:
                for c in self._canonicals[d]:
                    if c.canonical_id == canonical_id:
                        reverted_domain = d.value
                        reverted_surface = c.surface
                        break
                # remove from exact-index any entries pointing at the
                # reverted canonical
                stale = [
                    s for s, e in self._exact_index[d].items()
                    if e.canonical_id == canonical_id
                ]
                for s in stale:
                    del self._exact_index[d][s]
            now = int(time.time() * 1000)
            log_client.append(CanonicalizationReverted(
                event_id=make_event_id_log(now),
                recorded_at=now,
                actor=f"canonicalizer:{reason}" if reason else "canonicalizer",
                domain=reverted_domain,
                previous_canonical=canonical_id,
                surface_form=reverted_surface,
                new_canonical=new_canonical,
            ))
        return True

    # ---- introspection ----------------------------------------------------

    def canonicals(self, domain: CanonDomain) -> list[CanonicalEntry]:
        with self._lock:
            return [
                c for c in self._canonicals[domain]
                if c.canonical_id not in self._reverted
            ]

    def candidate_clusters(self, domain: CanonDomain) -> list[CandidateCluster]:
        with self._lock:
            return list(self._clusters[domain])


# =============================================================================
# Module-level singleton
# =============================================================================


_CAN: Canonicalizer | None = None
_CAN_LOCK = threading.Lock()


def get_canonicalizer() -> Canonicalizer:
    global _CAN
    with _CAN_LOCK:
        if _CAN is None:
            _CAN = Canonicalizer()
        return _CAN


def set_canonicalizer(c: Canonicalizer) -> None:
    global _CAN
    with _CAN_LOCK:
        _CAN = c


__all__ = [
    "CLUSTER_THRESHOLD_OFFSET",
    "CandidateCluster",
    "CandidateClusterMember",
    "CanonDomain",
    "CanonResolveResult",
    "Canonicalizer",
    "CanonicalEntry",
    "DEFAULT_PROMOTION_MIN_MEMBERS",
    "DEFAULT_PROMOTION_MIN_STABLE_PASSES",
    "DEFAULT_THRESHOLDS",
    "EmbedderFn",
    "get_canonicalizer",
    "set_canonicalizer",
]
