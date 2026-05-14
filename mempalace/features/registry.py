"""
Feature registry.

Features are typed, named values computed over the master views and
materialized into derived storage. The registry is the single source of
truth for what features exist, what type each is, what inputs each requires,
and how each is computed.

Why a registry rather than ad-hoc functions:

  - Rankers consume features by name; the registry makes the contract
    explicit and the type-correctness checkable.
  - Features are versioned. A feature's value at a given log offset is
    determined by its definition version + the inputs at that offset.
    When a feature's compute logic changes, the version changes, and
    consumers can choose to keep using the old version or migrate.
  - Persistence is uniform: all features go through the same persist path,
    so a feature view subscribes to feature-computed events without
    knowing what each feature means.

A feature definition includes:
  - name (canonical, e.g. "drawer_recency_score")
  - version (semver-ish; bumped when compute logic changes)
  - dtype (FeatureDType — float, int, vector, categorical)
  - dimension (for vectors)
  - subject_kind (what node kind this feature attaches to)
  - depends_on (which view names / event kinds the compute reads)
  - description (human-readable)

Compute functions live in `features/compute.py`; the registry keeps
metadata only.

Spec ref: Part 7.1 (feature catalog), R3 §1 (stacking framework consumes
typed features).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..schema.kinds import NodeKind


# =============================================================================
# Feature dtype taxonomy
# =============================================================================


class FeatureDType(str, Enum):
    """Canonical feature dtypes.

    Used by the registry to type-check feature values at compute time
    and by views to materialize them in the right shape.
    """

    FLOAT = "float"          # single scalar in R
    UNIT_FLOAT = "unit"      # scalar in [0, 1]
    SIGNED_FLOAT = "signed"  # scalar in [-1, 1]
    INT = "int"
    BOOL = "bool"
    VECTOR = "vector"        # list[float] of declared dimension
    CATEGORICAL = "cat"      # one of a declared label set
    DICT = "dict"            # heterogeneous dict; use sparingly


# =============================================================================
# Feature definition
# =============================================================================


@dataclass(frozen=True)
class FeatureDef:
    """Static definition of a feature.

    The compute function is set at registration time but stored separately
    from the FeatureDef so the def itself is hashable / comparable.
    """

    name: str
    version: str
    dtype: FeatureDType
    subject_kind: NodeKind | str  # NodeKind for graph features; "global" for system-wide
    description: str
    dimension: int | None = None       # for VECTOR dtypes
    categories: tuple[str, ...] | None = None  # for CATEGORICAL dtypes
    depends_on: tuple[str, ...] = ()   # view names / event kinds read at compute
    stance_aware: bool = False         # True if value depends on stance
    aggregation: str = "snapshot"      # "snapshot" | "accumulate" | "rolling"

    def validate_value(self, value: Any) -> None:
        """Raise ValueError if value doesn't match dtype."""
        match self.dtype:
            case FeatureDType.FLOAT:
                if not isinstance(value, (int, float)):
                    raise ValueError(f"feature {self.name}: expected float, got {type(value).__name__}")
            case FeatureDType.UNIT_FLOAT:
                if not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
                    raise ValueError(f"feature {self.name}: expected [0,1], got {value!r}")
            case FeatureDType.SIGNED_FLOAT:
                if not isinstance(value, (int, float)) or not (-1.0 <= float(value) <= 1.0):
                    raise ValueError(f"feature {self.name}: expected [-1,1], got {value!r}")
            case FeatureDType.INT:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"feature {self.name}: expected int, got {type(value).__name__}")
            case FeatureDType.BOOL:
                if not isinstance(value, bool):
                    raise ValueError(f"feature {self.name}: expected bool, got {type(value).__name__}")
            case FeatureDType.VECTOR:
                if not isinstance(value, (list, tuple)):
                    raise ValueError(f"feature {self.name}: expected vector, got {type(value).__name__}")
                if self.dimension is not None and len(value) != self.dimension:
                    raise ValueError(
                        f"feature {self.name}: vector dim mismatch "
                        f"(expected {self.dimension}, got {len(value)})"
                    )
            case FeatureDType.CATEGORICAL:
                if self.categories is not None and value not in self.categories:
                    raise ValueError(
                        f"feature {self.name}: value {value!r} not in {self.categories}"
                    )
            case FeatureDType.DICT:
                if not isinstance(value, dict):
                    raise ValueError(f"feature {self.name}: expected dict")


# =============================================================================
# Compute function signature
# =============================================================================

# A compute function takes (subject_id, context_dict) and returns a value
# of the declared dtype. context_dict carries view handles, optional stance,
# and the log offset at which compute is being run (for snapshot
# consistency).
ComputeFn = Callable[[str, dict[str, Any]], Any]


# =============================================================================
# Registry
# =============================================================================


@dataclass
class FeatureRegistry:
    """Global feature registry. Singleton in production."""

    _defs: dict[str, FeatureDef] = field(default_factory=dict)
    _computes: dict[str, ComputeFn] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(
        self,
        feature_def: FeatureDef,
        compute_fn: ComputeFn | None = None,
    ) -> None:
        """Register a feature. compute_fn is optional at registration
        (e.g., features computed by Rust views can be registered without
        a Python compute function)."""
        with self._lock:
            existing = self._defs.get(feature_def.name)
            if existing is not None and existing.version != feature_def.version:
                # Different version of same name — keyed by versioned name
                versioned_name = f"{feature_def.name}@{feature_def.version}"
                self._defs[versioned_name] = feature_def
                if compute_fn is not None:
                    self._computes[versioned_name] = compute_fn
            else:
                self._defs[feature_def.name] = feature_def
                if compute_fn is not None:
                    self._computes[feature_def.name] = compute_fn

    def get(self, name: str) -> FeatureDef | None:
        with self._lock:
            return self._defs.get(name)

    def get_compute(self, name: str) -> ComputeFn | None:
        with self._lock:
            return self._computes.get(name)

    def list_features(self, *, subject_kind: NodeKind | str | None = None) -> list[FeatureDef]:
        with self._lock:
            defs = list(self._defs.values())
        if subject_kind is None:
            return defs
        sk = subject_kind.value if isinstance(subject_kind, NodeKind) else subject_kind
        return [d for d in defs if (
            (d.subject_kind.value if isinstance(d.subject_kind, NodeKind) else d.subject_kind) == sk
        )]

    def reset(self) -> None:
        with self._lock:
            self._defs.clear()
            self._computes.clear()


# =============================================================================
# Module-level singleton + builtin features
# =============================================================================

_REGISTRY = FeatureRegistry()


def get_registry() -> FeatureRegistry:
    return _REGISTRY


# Common builtin features — registered at module load. Compute functions
# are populated by features.compute when that module is imported.

BUILTINS = [
    FeatureDef(
        name="drawer_recency_score",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind=NodeKind.DRAWER_REF,
        description="Exponentially-decayed recency score [0,1].",
        depends_on=("current_nodes",),
    ),
    FeatureDef(
        name="drawer_heat",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind=NodeKind.DRAWER_REF,
        description="Live heat from the heat-field view.",
        depends_on=("heat_field",),
    ),
    FeatureDef(
        name="drawer_velocity_30d",
        version="0.1.0",
        dtype=FeatureDType.FLOAT,
        subject_kind=NodeKind.DRAWER_REF,
        description="Access velocity over the trailing 30-day window.",
        depends_on=("velocity_field",),
    ),
    FeatureDef(
        name="theme_canonicality",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind=NodeKind.THEME,
        description="How canonical a theme is (0 = candidate, 1 = canonical).",
        depends_on=("canon_set",),
    ),
    FeatureDef(
        name="event_fork_significance",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind=NodeKind.EVENT,
        description="KisMATH-inspired fork significance: how much this event is a decision point.",
        depends_on=("current_edges", "schema_induced"),
    ),
    FeatureDef(
        name="assertion_substrate_faithfulness",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind=NodeKind.ASSERTION,
        description="How closely an assertion matches its derived_from substrate spans.",
        depends_on=("current_edges",),
    ),
    FeatureDef(
        name="period_velocity_coupling",
        version="0.1.0",
        dtype=FeatureDType.FLOAT,
        subject_kind=NodeKind.PERIOD,
        description="Cross-theme velocity correlation during this period.",
        depends_on=("velocity_field", "active_periods"),
    ),
    FeatureDef(
        name="stance_alignment_score",
        version="0.1.0",
        dtype=FeatureDType.UNIT_FLOAT,
        subject_kind="global",
        description="How well a candidate aligns with the active stance dimensions.",
        stance_aware=True,
        depends_on=("current_nodes",),
    ),
]


for _bi in BUILTINS:
    _REGISTRY.register(_bi)


__all__ = [
    "BUILTINS",
    "ComputeFn",
    "FeatureDef",
    "FeatureDType",
    "FeatureRegistry",
    "get_registry",
]
