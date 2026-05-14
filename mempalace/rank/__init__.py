"""
mempalace.rank — rankers, dispatch, isolation, signed loading.

Per Part 7 of the spec: rankers consume features and stance to produce
ordered scored candidates. Multiple rankers exist; dispatch selects one
per ConsumerKind. All rankers run inside isolation primitives.

Per R3 §1: ranker stacking is a specialization of the unified stacking
framework (mempalace.stack).
"""

from .dispatch import (
    DispatchTable,
    configure_dispatch,
    dispatch_ranker,
    get_dispatch_table,
    rank_candidates,
)
from .factored import FactoredConfig, FactoredMultiplicativeRanker
from .isolation import (
    BehaviorMonitor,
    InProcessFenceRanker,
    IsolatedRankerProxy,
    IsolatedRankerSpec,
    RankerStats,
    get_behavior_monitor,
)
from .neural_stub import CrossTermTable, NeuralRankerStub
from .protocol import (
    Ranker,
    RankerManifest,
    ScoredCandidate,
    empty_score,
    normalize_scores,
)
from .registry import RankerRegistry, get_ranker_registry
from .signed_loader import (
    RankerBundle,
    TrustStore,
    VerificationResult,
    load_signed_ranker,
    verify_bundle,
)

__all__ = [
    "BehaviorMonitor",
    "CrossTermTable",
    "DispatchTable",
    "FactoredConfig",
    "FactoredMultiplicativeRanker",
    "InProcessFenceRanker",
    "IsolatedRankerProxy",
    "IsolatedRankerSpec",
    "NeuralRankerStub",
    "Ranker",
    "RankerBundle",
    "RankerManifest",
    "RankerRegistry",
    "RankerStats",
    "ScoredCandidate",
    "TrustStore",
    "VerificationResult",
    "configure_dispatch",
    "dispatch_ranker",
    "empty_score",
    "get_behavior_monitor",
    "get_dispatch_table",
    "get_ranker_registry",
    "load_signed_ranker",
    "normalize_scores",
    "rank_candidates",
    "verify_bundle",
]
