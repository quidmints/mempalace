"""
mempalace.derived — derived representations.

Per Part 8: master views are consumer-agnostic; derived representations
are consumer-optimized projections built from the master log via the
subscribe-tick-update pattern.
"""

from .attribution import AttributionRecord, AttributionStore
from .base import DerivedRepresentation, DerivedRepStats
from .foyer_cache import FoyerCache, FoyerEntry
from .ranker_cache import (
    PROJECTED_CLUSTER_SIGNATURE,
    RankerOutputCache,
    RankerOutputCacheEntry,
    RankerOutputCacheKey,
    get_default_cache,
    reset_default_cache,
    set_default_cache,
)
from .realtime_index import IndexEntry, RealtimeIndex
from .registry import (
    DerivedRegistry,
    DerivedRepEntry,
    InvalidationPolicy,
    get_derived_registry,
)
from .transition_cache import CoherenceEntry, CoherenceKind, TransitionCache

__all__ = [
    "AttributionRecord",
    "AttributionStore",
    "CoherenceEntry",
    "CoherenceKind",
    "DerivedRegistry",
    "DerivedRepEntry",
    "DerivedRepStats",
    "DerivedRepresentation",
    "FoyerCache",
    "FoyerEntry",
    "IndexEntry",
    "InvalidationPolicy",
    "PROJECTED_CLUSTER_SIGNATURE",
    "RankerOutputCache",
    "RankerOutputCacheEntry",
    "RankerOutputCacheKey",
    "RealtimeIndex",
    "TransitionCache",
    "get_default_cache",
    "get_derived_registry",
    "reset_default_cache",
    "set_default_cache",
]
