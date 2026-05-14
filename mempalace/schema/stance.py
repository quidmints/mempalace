"""
Stance vectors.

The stance is what the query carries that tells the ranker how to weight
features. Different cognitive tasks have different stances; same query
against same data produces different rankings under different stances.

Stance dimensions are open: the dataclass has a known set of dimensions plus
an `extras` dict for ranker-specific custom fields. Rankers read what they
care about; unknown dimensions are ignored.

Spec ref: Part 6.2, R3 §6.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kinds import ConsumerKind


@dataclass
class Stance:
    """Stance vector carried by every query.

    Dimensions:
        correspondence_vs_coherence: 0 = pure coherence (self-understanding,
            reflection); 1 = pure correspondence (fact-finding, audit). Default
            0.5 (balanced).

        temporal_sensitivity: 0 = atemporal (don't filter by valid window);
            1 = strict temporal-overlap required. Per R2 §6 / R3: most
            triangulation doesn't need temporal-overlap as a hard precondition;
            only when stance demands it.

        contradiction_weight: 0 = suppress contradictions; 1 = amplify them.
            Stance-conditional rather than global. Correspondence-biased
            queries typically push this up; coherence-biased queries push down.

        recency_bias: 0 = ignore velocity; 1 = velocity dominates over heat-
            integrated. Affects how rankers compose the heat field with
            velocity field for ranking.

        canonicality_floor: 0 = canonical content competes equally with
            ephemeral; 1 = canonical dominates. Used by FOYER and identity-
            level queries.

        exploration_entropy: 0 = commit to top scores; 1 = high entropy at
            near-tie cases (KisMATH-style fork exploration). Matching pathway
            wants high; FOYER wants low.

        consumer_kind: which consumer is asking. Drives ranker dispatch and
            sets reasonable defaults for the other dimensions.

    Plus `extras` for ranker-specific custom fields.
    """
    consumer_kind: ConsumerKind = ConsumerKind.CLAUDE_THREAD
    correspondence_vs_coherence: float = 0.5
    temporal_sensitivity: float = 0.0
    contradiction_weight: float = 0.5
    recency_bias: float = 0.5
    canonicality_floor: float = 0.5
    exploration_entropy: float = 0.3
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "correspondence_vs_coherence",
            "temporal_sensitivity",
            "contradiction_weight",
            "recency_bias",
            "canonicality_floor",
            "exploration_entropy",
        ):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0, 1], got {v}")

    @classmethod
    def for_consumer(cls, consumer: ConsumerKind) -> "Stance":
        """Return the recommended default stance for a given consumer kind."""
        defaults = _CONSUMER_DEFAULTS.get(consumer, {})
        return cls(consumer_kind=consumer, **defaults)


_CONSUMER_DEFAULTS: dict[ConsumerKind, dict[str, float]] = {
    ConsumerKind.CLAUDE_THREAD: {
        "correspondence_vs_coherence": 0.5,
        "recency_bias": 0.5,
        "canonicality_floor": 0.5,
        "exploration_entropy": 0.3,
    },
    ConsumerKind.MONTAGE: {
        # Montage values diversity (acoustic rhyme, conceptual rhyme); push
        # exploration up. Coherence-leaning because we want coherent sequences.
        "correspondence_vs_coherence": 0.3,
        "recency_bias": 0.3,
        "canonicality_floor": 0.3,
        "exploration_entropy": 0.7,
    },
    ConsumerKind.MATCHING: {
        # Matching wants to surface diverse candidates; high exploration.
        # Correspondence-leaning because we want real overlap, not coherence-
        # confabulated overlap.
        "correspondence_vs_coherence": 0.7,
        "recency_bias": 0.5,
        "canonicality_floor": 0.4,
        "exploration_entropy": 0.8,
    },
    ConsumerKind.FOYER: {
        # FOYER surfaces canonical content; canon dominates, exploration low.
        "correspondence_vs_coherence": 0.4,
        "recency_bias": 0.2,
        "canonicality_floor": 0.95,
        "exploration_entropy": 0.05,
    },
    ConsumerKind.AGENT: {
        # Generic agent stance; tunable per agent.
        "correspondence_vs_coherence": 0.6,
        "recency_bias": 0.5,
        "canonicality_floor": 0.5,
        "exploration_entropy": 0.4,
    },
    ConsumerKind.REVIEW_MODE: {
        # Review mode wants to see contradictions and pending issues clearly;
        # correspondence-biased and contradiction-amplifying.
        "correspondence_vs_coherence": 0.85,
        "contradiction_weight": 0.85,
        "recency_bias": 0.3,
        "canonicality_floor": 0.5,
        "exploration_entropy": 0.2,
    },
    ConsumerKind.RESOLVE: {
        # Resolution stack wants definitive answers; correspondence-pure,
        # low exploration, cautious about contradictions.
        "correspondence_vs_coherence": 0.95,
        "contradiction_weight": 0.7,
        "temporal_sensitivity": 0.5,
        "recency_bias": 0.3,
        "canonicality_floor": 0.5,
        "exploration_entropy": 0.05,
    },
}
