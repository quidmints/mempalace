"""
Temporal-triple proximity retrieval.

# What this addresses

Per the user reframe of "triple": three characteristics — one of the
past, one of the present, one of the future — held in union by a
**traversal** through the substrate. Proximity here isn't a single
distance; it's whether the substrate can draw a coherent line through
all three regions, and how short / how well-grounded that line is.

A query like "should I go to grad school for math" insinuates:

  - past characteristic    → experiences with math
  - present characteristic → current consideration-state
  - future characteristic  → trajectory the query is pursuing

The answer isn't in any single drawer. It's the **path** that
connects something from each region. The path IS the answer
(structural form), AND a synthesized response cites that path
(narrative form) — both, per the user's directive.

# Substrate dispatch

Different temporal characteristics naturally favor different substrates:

  - **Past** biases the DAG. Events are structurally located in time;
    assertions carry valid_from/valid_to. The DAG knows what a drawer
    is *about*.
  - **Present** biases the embedding store. Current-state isn't yet
    structured into the assertion graph (mining lags real-time).
    Embeddings know what a drawer *feels like*.
  - **Future** is mixed. Explicit goal assertions (PURSUES, AIMED_AT)
    live in the DAG; aspirational language sits in the embedding
    cluster. When NO future calibration exists, project — apply the
    present's lens to the past, find analogous trajectories,
    hypothesize a future-node by inference.

# The path as primitive

A traversal is a sequence of typed hops. Each hop is one of:

  - **DAG_EDGE**   — follow a typed edge in the assertion graph.
                     Cheap, high-confidence, structurally anchored.
  - **CHROMA_NN**  — embedding-space nearest-neighbor jump from one
                     drawer to another. Cheap, but unanchored unless
                     scaffolded by surrounding DAG hops.
  - **PROJECTION** — a hypothesized hop into a virtual future-node
                     synthesized from past analogues + present lens.

A "good" path mixes these — DAG anchors keep it honest, Chroma hops
let it cross between structurally-disconnected drawers that actually
relate. A path made of pure Chroma hops gets penalized; one with at
least two DAG anchors is high-confidence.

# What this enables

  1. Reflective queries that aren't single-document answers.
  2. Honest "I don't have evidence for this" responses when no
     coherent path exists.
  3. Substrate-honest cost: queries leaning past don't overpay for
     embedding scans; queries leaning present don't overpay for
     graph walks.
  4. Federation matching with much stronger compatibility signal:
     two palaces with similar past/present/future triples around
     the same topic match more deeply than predicate overlap alone.

Spec ref: this module is the user's "tripled-characteristic" reframe
of triples (post the assertion rename). Sits alongside the existing
TRIPLES_REFRAME.md senses (n-tuple arity, triangulation by witness).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..embed.client import EmbeddingStore, get_default_store
from ..embed.model import EmbeddingService, get_default_service
from ..schema.kinds import EdgeKind, NodeKind
from ..views import current as views


# =============================================================================
# Enums + tunables
# =============================================================================


class TimeAxis(str, Enum):
    PAST = "past"
    PRESENT = "present"
    FUTURE = "future"


class HopKind(str, Enum):
    DAG_EDGE = "dag_edge"
    CHROMA_NN = "chroma_nn"
    PROJECTION = "projection"


# Hop-cost weights for path scoring. Lower cost = preferred.
DAG_HOP_COST = 1.0
"""DAG hops are the cheapest — structurally anchored."""

CHROMA_HOP_COST = 1.6
"""Embedding hops cost more — they jump across structural gaps,
so each one slightly degrades path coherence."""

PROJECTION_HOP_COST = 2.5
"""Projection hops are the most expensive — they're inferential.
A path with a projection hop is still valid (and sometimes the
only available answer), but ranks below paths grounded entirely
in observed substrate."""


DEFAULT_BEAM_WIDTH = 8
DEFAULT_MAX_HOPS = 8
DEFAULT_REGION_SEED_K = 5


# =============================================================================
# Hop / Path
# =============================================================================


@dataclass(frozen=True)
class Hop:
    """One step in a traversal."""

    kind: HopKind
    from_node_id: str
    to_node_id: str
    edge_kind: str | None = None
    """For DAG_EDGE — the EdgeKind value taken."""
    similarity: float | None = None
    """For CHROMA_NN — the cosine similarity."""

    @property
    def cost(self) -> float:
        if self.kind == HopKind.DAG_EDGE:
            return DAG_HOP_COST
        if self.kind == HopKind.CHROMA_NN:
            base = CHROMA_HOP_COST
            # Higher similarity → slightly cheaper hop
            if self.similarity is not None:
                return base * (1.0 - 0.3 * self.similarity)
            return base
        return PROJECTION_HOP_COST


@dataclass
class Path:
    """A traversal that touches one node from each temporal region."""

    nodes: list[str] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)
    region_anchors: dict[TimeAxis, str] = field(default_factory=dict)
    """Which node in `nodes` is the anchor for each axis."""

    @property
    def length(self) -> int:
        return len(self.hops)

    @property
    def total_cost(self) -> float:
        return sum(h.cost for h in self.hops)

    @property
    def has_full_triple(self) -> bool:
        return all(axis in self.region_anchors for axis in TimeAxis)

    @property
    def hop_kinds(self) -> tuple[HopKind, ...]:
        return tuple(h.kind for h in self.hops)

    @property
    def dag_anchor_count(self) -> int:
        return sum(1 for h in self.hops if h.kind == HopKind.DAG_EDGE)

    @property
    def coherence_score(self) -> float:
        """Higher = better-grounded. Penalizes pure-Chroma chains
        and rewards DAG anchoring. In [0, 1]."""
        if not self.hops:
            return 0.0
        # Base: fraction of hops that are DAG-anchored
        dag_frac = self.dag_anchor_count / len(self.hops)
        # Triple completeness bonus
        triple_bonus = 1.0 if self.has_full_triple else 0.5
        # Length penalty: very long paths score lower
        length_factor = max(0.3, 1.0 - 0.05 * len(self.hops))
        return dag_frac * triple_bonus * length_factor


# =============================================================================
# Characteristic + TemporalQuery
# =============================================================================


@dataclass
class Characteristic:
    """One temporal characteristic of a query.

    A characteristic describes WHAT the query is reaching for at one
    time-axis. The walker uses it to resolve a region (set of seed
    nodes) in the substrate.
    """

    axis: TimeAxis
    description: str
    """Human-readable description used as the embedding-search query
    text and as context for the synthesized answer."""

    keyword_seeds: tuple[str, ...] = ()
    """Optional keywords for DAG-side region resolution. The walker
    matches these against entity name properties + assertion
    predicates."""

    explicit_node_ids: tuple[str, ...] = ()
    """Caller can pin specific nodes that anchor this region. Used
    when the caller already knows which assertions / drawers count."""

    dag_weight: float = 0.5
    """0.0 = ignore the DAG, 1.0 = DAG-only. Defaults split 50/50."""

    chroma_weight: float = 0.5

    def with_substrate_bias(
        self, *, dag: float, chroma: float,
    ) -> "Characteristic":
        return Characteristic(
            axis=self.axis,
            description=self.description,
            keyword_seeds=self.keyword_seeds,
            explicit_node_ids=self.explicit_node_ids,
            dag_weight=dag,
            chroma_weight=chroma,
        )


@dataclass
class TemporalQuery:
    """A query expressed as the union of three temporal characteristics.

    `future` may be None — when so, the walker projects a future
    region using the present's lens applied to past trajectories.
    """

    past: Characteristic
    present: Characteristic
    future: Characteristic | None = None
    description: str = ""
    """Optional natural-language framing of the whole query."""


# =============================================================================
# Region resolution
# =============================================================================


@dataclass
class _Region:
    """Resolved seeds for one characteristic — a small set of
    candidate starting nodes."""

    axis: TimeAxis
    seed_nodes: list[str] = field(default_factory=list)
    seed_scores: dict[str, float] = field(default_factory=dict)
    """Per-node confidence in [0, 1]. Higher = better starting point."""

    is_projected: bool = False
    """True when the region was generated by projection rather than
    resolved from observed substrate."""


def _resolve_region(
    char: Characteristic,
    *,
    embedding_store: EmbeddingStore,
    embedder: EmbeddingService,
    seed_k: int = DEFAULT_REGION_SEED_K,
) -> _Region:
    """Resolve one characteristic to a region of seed nodes.

    Honors the characteristic's substrate bias: a high `dag_weight`
    pulls in nodes via the assertion graph (predicate match, name
    match), a high `chroma_weight` pulls in nodes via embedding
    nearest-neighbor.
    """
    region = _Region(axis=char.axis)

    if char.explicit_node_ids:
        for nid in char.explicit_node_ids:
            region.seed_nodes.append(nid)
            region.seed_scores[nid] = 1.0
        return region

    # ---- DAG-side resolution -------------------------------------
    if char.dag_weight > 0:
        dag_hits = _dag_match(char.keyword_seeds + (char.description,))
        for nid, score in dag_hits[:seed_k]:
            weighted = score * char.dag_weight
            if nid in region.seed_scores:
                region.seed_scores[nid] += weighted
            else:
                region.seed_scores[nid] = weighted
                region.seed_nodes.append(nid)

    # ---- Chroma-side resolution ----------------------------------
    if char.chroma_weight > 0 and embedding_store.count() > 0:
        try:
            qvec = embedder.embed(char.description)
        except Exception:
            qvec = None
        if qvec:
            chroma_hits = embedding_store.query(qvec, k=seed_k)
            for hit in chroma_hits:
                weighted = hit.similarity * char.chroma_weight
                if hit.drawer_id in region.seed_scores:
                    region.seed_scores[hit.drawer_id] += weighted
                else:
                    region.seed_scores[hit.drawer_id] = weighted
                    region.seed_nodes.append(hit.drawer_id)

    # Sort seed_nodes by score descending
    region.seed_nodes.sort(
        key=lambda nid: region.seed_scores[nid],
        reverse=True,
    )
    region.seed_nodes = region.seed_nodes[:seed_k]
    return region


def _dag_match(keywords: tuple[str, ...]) -> list[tuple[str, float]]:
    """Find nodes whose properties match any of the keywords.

    Returns (node_id, match_score) pairs. Score is fraction of
    matched keywords. Cheap: scans the in-memory view store.
    """
    keywords_lower = tuple(
        k.lower() for k in keywords if k and isinstance(k, str)
    )
    if not keywords_lower:
        return []

    hits: dict[str, float] = {}
    store = views._get_store()
    with store._lock:
        for nid, ns in store.nodes.items():
            text_blob = " ".join(
                str(v) for v in ns.properties.values()
                if isinstance(v, (str, int, float))
            ).lower()
            if not text_blob:
                continue
            matched = sum(1 for k in keywords_lower if k in text_blob)
            if matched:
                hits[nid] = matched / len(keywords_lower)

    return sorted(hits.items(), key=lambda kv: kv[1], reverse=True)


# =============================================================================
# Future projection — hypothesized region when `future` is None or empty
#
# Per user spec: "possible futures are always projection of that
# present's lens on the past tempered by existing calibrations
# for the future intent."
# =============================================================================


def _project_future_region(
    past: Characteristic,
    present: Characteristic,
    hint: Characteristic | None,
    *,
    embedding_store: EmbeddingStore,
    embedder: EmbeddingService,
) -> _Region:
    """Hypothesize a future region by analogy.

    Algorithm:
      1. Apply the present's lens to the past — find past episodes
         that look like the present's framing.
      2. Walk forward in time from those analogue past episodes —
         where did similar situations lead? (PRECEDES / SUCCEEDS,
         or assertions whose valid_from is later.)
      3. Temper by existing calibrations — if there are explicit
         future-intent assertions (PURSUES / AIMED_AT / INTENDS-style
         predicates), constrain the projection toward them.
      4. If no calibrations exist and no analogue trajectories
         suffice, emit a virtual projected node carrying the
         hypothesis.
    """
    region = _Region(axis=TimeAxis.FUTURE, is_projected=True)

    # Step 1: present's lens on past
    present_vec = None
    if embedding_store.count() > 0:
        try:
            present_vec = embedder.embed(present.description)
        except Exception:
            present_vec = None

    past_analogues: list[str] = []
    if present_vec:
        # Past analogues = drawers similar to the present, filtered to
        # past time-window. Without a time filter on the embedding
        # store, we approximate by querying broadly and trusting the
        # later DAG walk to surface the temporal structure.
        for hit in embedding_store.query(present_vec, k=10):
            past_analogues.append(hit.drawer_id)

    # Step 2: walk forward in time from each analogue
    forward_neighborhood: dict[str, float] = {}
    for analogue in past_analogues:
        for forward_node in _walk_forward(analogue, max_hops=2):
            forward_neighborhood[forward_node] = (
                forward_neighborhood.get(forward_node, 0.0) + 1.0
            )

    # Step 3: temper by existing calibrations
    calibration_nodes = _find_future_intent_assertions()
    if calibration_nodes:
        # Filter forward_neighborhood by overlap with calibrations.
        # When neighborhoods don't overlap calibrations, we keep the
        # calibrations themselves as the future region (they're the
        # best evidence we have of where the trajectory should go).
        constrained = {
            n: s for n, s in forward_neighborhood.items()
            if n in calibration_nodes
        }
        if constrained:
            forward_neighborhood = constrained
        else:
            # Use calibrations directly
            for cn in calibration_nodes:
                forward_neighborhood[cn] = 1.0

    # Step 4: if we have nothing, emit a virtual projected node
    if not forward_neighborhood:
        virtual_id = _make_virtual_projected_node_id(present, hint)
        region.seed_nodes = [virtual_id]
        region.seed_scores = {virtual_id: 0.5}
        return region

    # Otherwise, top-K from the forward neighborhood become the seeds
    ranked = sorted(
        forward_neighborhood.items(), key=lambda kv: kv[1], reverse=True,
    )
    region.seed_nodes = [n for n, _ in ranked[:DEFAULT_REGION_SEED_K]]
    region.seed_scores = {n: s for n, s in ranked[:DEFAULT_REGION_SEED_K]}
    return region


def _walk_forward(start_id: str, *, max_hops: int) -> set[str]:
    """Walk PRECEDES + SUCCEEDS edges forward in time. Returns
    reachable node IDs."""
    forward: set[str] = set()
    frontier: list[str] = [start_id]
    visited: set[str] = {start_id}
    for _ in range(max_hops):
        next_frontier: list[str] = []
        for nid in frontier:
            for edge in views.outgoing_edges(nid, kind=EdgeKind.PRECEDES) or []:
                if edge.target_node_id not in visited:
                    visited.add(edge.target_node_id)
                    forward.add(edge.target_node_id)
                    next_frontier.append(edge.target_node_id)
            for edge in views.incoming_edges(nid, kind=EdgeKind.SUCCEEDS) or []:
                if edge.source_node_id not in visited:
                    visited.add(edge.source_node_id)
                    forward.add(edge.source_node_id)
                    next_frontier.append(edge.source_node_id)
        frontier = next_frontier
    return forward


_FUTURE_INTENT_PREDICATES = frozenset({
    "pursues", "aimed_at", "intends", "wants", "plans",
    "goal", "aspires_to", "will", "expects",
})
"""Predicates that explicitly point AT a future state. Note that
'considering', 'deciding', 'exploring' are NOT in this set —
those are present-deliberation states (the self deliberating now),
not future-state assertions. Including them would collapse the
present axis onto the future axis incorrectly."""


def _find_future_intent_assertions() -> set[str]:
    """Return assertion node IDs whose predicate signals
    forward-looking intent."""
    out: set[str] = set()
    store = views._get_store()
    with store._lock:
        for nid, ns in store.nodes.items():
            if ns.node_kind != NodeKind.ASSERTION.value:
                continue
            pred = str(ns.properties.get("predicate", "")).lower()
            if pred in _FUTURE_INTENT_PREDICATES:
                out.add(nid)
            elif any(p in pred for p in _FUTURE_INTENT_PREDICATES):
                out.add(nid)
    return out


def _make_virtual_projected_node_id(
    present: Characteristic, hint: Characteristic | None,
) -> str:
    """A virtual node ID for a hypothesized future. Not persisted to
    the log — lives only in the result. Stable across re-runs of the
    same query so callers can deduplicate."""
    import hashlib
    seed = f"{present.description}|{hint.description if hint else ''}"
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).hexdigest()
    return f"projected_{digest}"


# =============================================================================
# Beam-search walker
# =============================================================================


@dataclass
class _Frontier:
    """One frontier in the beam search."""

    path: Path
    current_node: str
    regions_touched: set[TimeAxis]
    g_cost: float  # accumulated cost so far

    def __lt__(self, other: "_Frontier") -> bool:
        # heapq is min-heap; lower g_cost is better
        return self.g_cost < other.g_cost


def _expand(
    frontier: _Frontier,
    *,
    embedding_store: EmbeddingStore,
    embedder: EmbeddingService,
    chroma_k: int = 3,
) -> list[_Frontier]:
    """Generate successor frontiers from one current frontier."""
    successors: list[_Frontier] = []
    seen_targets: set[str] = set(frontier.path.nodes)

    # ---- DAG hops ------------------------------------------------
    for kind in (
        EdgeKind.REFINES, EdgeKind.SUPERSEDES,
        EdgeKind.SUPPORTS, EdgeKind.CONTRADICTS,
        EdgeKind.DERIVED_FROM, EdgeKind.ASSERTED_SUBJECT,
        EdgeKind.ASSERTED_OBJECT, EdgeKind.PURSUES,
        EdgeKind.AIMED_AT, EdgeKind.PRECEDES, EdgeKind.SUCCEEDS,
    ):
        for edge in views.outgoing_edges(
            frontier.current_node, kind=kind,
        ) or []:
            if edge.target_node_id in seen_targets:
                continue
            seen_targets.add(edge.target_node_id)
            hop = Hop(
                kind=HopKind.DAG_EDGE,
                from_node_id=frontier.current_node,
                to_node_id=edge.target_node_id,
                edge_kind=kind.value,
            )
            successors.append(_make_successor(frontier, hop))

    # ---- Chroma nearest-neighbor hops ----------------------------
    if embedding_store.count() > 0:
        node = views.current_node(frontier.current_node)
        node_text = ""
        if node is not None:
            node_text = " ".join(
                str(v) for v in node.properties.values()
                if isinstance(v, str)
            )
        if not node_text:
            # Fall back to the drawer text for drawer nodes
            try:
                node_text = views.drawer_text(frontier.current_node)
            except Exception:
                node_text = ""
        if node_text:
            try:
                qvec = embedder.embed(node_text)
                hits = embedding_store.query(qvec, k=chroma_k + 1)
                for hit in hits:
                    if hit.drawer_id in seen_targets:
                        continue
                    if hit.drawer_id == frontier.current_node:
                        continue
                    seen_targets.add(hit.drawer_id)
                    hop = Hop(
                        kind=HopKind.CHROMA_NN,
                        from_node_id=frontier.current_node,
                        to_node_id=hit.drawer_id,
                        similarity=hit.similarity,
                    )
                    successors.append(_make_successor(frontier, hop))
            except Exception:
                pass

    return successors


def _make_successor(parent: _Frontier, hop: Hop) -> _Frontier:
    new_path = Path(
        nodes=parent.path.nodes + [hop.to_node_id],
        hops=parent.path.hops + [hop],
        region_anchors=dict(parent.path.region_anchors),
    )
    return _Frontier(
        path=new_path,
        current_node=hop.to_node_id,
        regions_touched=set(parent.regions_touched),
        g_cost=parent.g_cost + hop.cost,
    )


def _annotate_region_anchors(
    path: Path, regions: dict[TimeAxis, _Region],
) -> Path:
    """Tag each node in path.nodes with which region's anchor it is.
    First match wins per axis."""
    for axis, region in regions.items():
        if axis in path.region_anchors:
            continue
        for nid in path.nodes:
            if nid in region.seed_scores:
                path.region_anchors[axis] = nid
                break
    return path


def traverse(
    query: TemporalQuery,
    *,
    beam_width: int = DEFAULT_BEAM_WIDTH,
    max_hops: int = DEFAULT_MAX_HOPS,
    embedding_store: EmbeddingStore | None = None,
    embedder: EmbeddingService | None = None,
) -> list[Path]:
    """Run the beam search. Returns ranked paths that satisfy the
    temporal triple (touch all three regions when possible).

    Cost is dominated by `beam_width × max_hops × avg_branching`.
    Default settings produce ~64 frontier expansions worst-case;
    most queries terminate well before that because paths satisfying
    the triple get returned early.
    """
    store = embedding_store or get_default_store()
    emb = embedder or get_default_service()

    # ---- Resolve regions ----------------------------------------
    past_region = _resolve_region(
        query.past, embedding_store=store, embedder=emb,
    )
    present_region = _resolve_region(
        query.present, embedding_store=store, embedder=emb,
    )
    if query.future is None or not _resolve_region(
        query.future, embedding_store=store, embedder=emb,
    ).seed_nodes:
        future_region = _project_future_region(
            query.past, query.present, query.future,
            embedding_store=store, embedder=emb,
        )
    else:
        future_region = _resolve_region(
            query.future, embedding_store=store, embedder=emb,
        )

    regions = {
        TimeAxis.PAST: past_region,
        TimeAxis.PRESENT: present_region,
        TimeAxis.FUTURE: future_region,
    }

    # ---- Initialize beam from past seeds ------------------------
    beam: list[_Frontier] = []
    for seed_node in past_region.seed_nodes:
        path = Path(nodes=[seed_node])
        path.region_anchors[TimeAxis.PAST] = seed_node
        # Mark whether seed is also in present/future regions
        regions_touched = {TimeAxis.PAST}
        if seed_node in present_region.seed_scores:
            regions_touched.add(TimeAxis.PRESENT)
            path.region_anchors[TimeAxis.PRESENT] = seed_node
        if seed_node in future_region.seed_scores:
            regions_touched.add(TimeAxis.FUTURE)
            path.region_anchors[TimeAxis.FUTURE] = seed_node
        heapq.heappush(beam, _Frontier(
            path=path,
            current_node=seed_node,
            regions_touched=regions_touched,
            g_cost=0.0,
        ))

    # ---- Beam search --------------------------------------------
    completed: list[Path] = []
    iterations = 0
    max_iterations = beam_width * max_hops * 4
    # Track the best partial frontier we've seen — used for the
    # projection fallback when the beam empties without a full triple.
    best_partial: _Frontier | None = None

    while beam and iterations < max_iterations:
        iterations += 1
        # Pop the best frontier
        if len(beam) > beam_width:
            beam = heapq.nsmallest(beam_width, beam)
            heapq.heapify(beam)
        frontier = heapq.heappop(beam)

        # Check if this frontier completes the triple
        new_touched = set(frontier.regions_touched)
        if frontier.current_node in present_region.seed_scores:
            new_touched.add(TimeAxis.PRESENT)
        if frontier.current_node in future_region.seed_scores:
            new_touched.add(TimeAxis.FUTURE)

        if new_touched != frontier.regions_touched:
            frontier = _Frontier(
                path=frontier.path,
                current_node=frontier.current_node,
                regions_touched=new_touched,
                g_cost=frontier.g_cost,
            )

        # Update best_partial — prefer frontiers that have touched
        # more regions; among ties, prefer cheaper.
        if best_partial is None or (
            len(frontier.regions_touched) > len(best_partial.regions_touched)
            or (
                len(frontier.regions_touched) == len(best_partial.regions_touched)
                and frontier.g_cost < best_partial.g_cost
            )
        ):
            best_partial = frontier

        if frontier.regions_touched == {
            TimeAxis.PAST, TimeAxis.PRESENT, TimeAxis.FUTURE,
        }:
            completed.append(_annotate_region_anchors(
                frontier.path, regions,
            ))
            if len(completed) >= beam_width:
                break
            continue

        if frontier.path.length >= max_hops:
            continue

        for succ in _expand(
            frontier, embedding_store=store, embedder=emb,
        ):
            heapq.heappush(beam, succ)

    # If we completed nothing but have a future projection, emit a
    # path that ends at the projected node via a PROJECTION hop
    # from the best partial path. Works even when the beam emptied
    # before completing, as long as we saw some frontier.
    if not completed and future_region.seed_nodes and best_partial is not None:
        proj_id = future_region.seed_nodes[0]
        proj_hop = Hop(
            kind=HopKind.PROJECTION,
            from_node_id=best_partial.current_node,
            to_node_id=proj_id,
        )
        synth_path = Path(
            nodes=best_partial.path.nodes + [proj_id],
            hops=best_partial.path.hops + [proj_hop],
            region_anchors=dict(best_partial.path.region_anchors),
        )
        synth_path.region_anchors[TimeAxis.FUTURE] = proj_id
        completed.append(_annotate_region_anchors(synth_path, regions))

    # Rank by coherence_score then by total_cost
    completed.sort(
        key=lambda p: (-p.coherence_score, p.total_cost),
    )
    return completed


# =============================================================================
# Result envelope (paths + synthesized answer)
# =============================================================================


@dataclass
class TemporalResult:
    """Both forms per user directive:
      - `paths` is the structural answer (here are the connections,
        you draw the meaning)
      - `synthesized_answer` is the narrative answer (here's the
        synthesis, citing the path as provenance)
    """

    query: TemporalQuery
    paths: list[Path]
    synthesized_answer: str = ""
    projected_future_node_id: str | None = None
    """When the future region was hypothesized rather than resolved
    from observed substrate, this is the virtual node ID. Callers
    can re-run with this pinned via `Characteristic.explicit_node_ids`
    to drill in."""

    @property
    def has_paths(self) -> bool:
        return bool(self.paths)

    @property
    def best_path(self) -> Path | None:
        return self.paths[0] if self.paths else None


def synthesize_answer(query: TemporalQuery, paths: list[Path]) -> str:
    """Generate the narrative answer that cites the path.

    This default implementation is template-based — no LLM call. It
    produces a structured prose summary of the best path, citing
    each hop. Production deployments would override this with an
    LLM-driven synthesizer that takes (query, paths) → response,
    using paths as grounding context.
    """
    if not paths:
        return (
            "I don't have evidence in the substrate to answer this query. "
            "The past, present, and future characteristics couldn't be "
            "connected through any coherent path."
        )

    best = paths[0]
    lines: list[str] = []
    lines.append(
        f"Following the trail of '{query.description or 'this query'}':"
    )
    lines.append("")
    if best.has_full_triple:
        for axis in (TimeAxis.PAST, TimeAxis.PRESENT, TimeAxis.FUTURE):
            anchor = best.region_anchors.get(axis)
            if anchor is None:
                continue
            anchor_text = _summarize_node(anchor)
            lines.append(f"  [{axis.value}] {anchor_text}")
    lines.append("")
    lines.append(
        f"Path: {len(best.hops)} hop(s), "
        f"{best.dag_anchor_count} DAG-anchored, "
        f"coherence={best.coherence_score:.2f}"
    )
    if any(h.kind == HopKind.PROJECTION for h in best.hops):
        lines.append(
            "  (Future characteristic was projected — no explicit "
            "evidence in the substrate; hypothesized by inference "
            "from analogous past trajectories.)"
        )
    return "\n".join(lines)


def _summarize_node(node_id: str) -> str:
    """One-line summary of a node for citation."""
    if node_id.startswith("projected_"):
        return f"<projected future: {node_id}>"
    node = views.current_node(node_id)
    if node is None:
        return f"<unknown node {node_id}>"
    name = node.properties.get("name") or node.properties.get("predicate")
    if name:
        return f"{node.node_kind} '{name}' [{node_id[:24]}]"
    if node.node_kind == "drawer_ref":
        text = views.drawer_text(node_id)
        if text:
            return f"drawer '{text[:40]}...' [{node_id[:24]}]"
    return f"{node.node_kind} [{node_id[:24]}]"


def query_temporal(
    query: TemporalQuery,
    **kwargs: Any,
) -> TemporalResult:
    """End-to-end: resolve regions, traverse, synthesize.

    The single public entry point. Returns both the structural paths
    and a synthesized narrative answer that cites them.
    """
    paths = traverse(query, **kwargs)
    answer = synthesize_answer(query, paths)
    projected_id: str | None = None
    if paths:
        for hop in paths[0].hops:
            if hop.kind == HopKind.PROJECTION:
                projected_id = hop.to_node_id
                break
    return TemporalResult(
        query=query,
        paths=paths,
        synthesized_answer=answer,
        projected_future_node_id=projected_id,
    )


__all__ = [
    "Characteristic",
    "Hop",
    "HopKind",
    "Path",
    "TemporalQuery",
    "TemporalResult",
    "TimeAxis",
    "query_temporal",
    "synthesize_answer",
    "traverse",
]
