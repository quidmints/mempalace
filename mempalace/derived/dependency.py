"""
Dependency tracking over versioned artifacts (Phase 4).

Phase 2 gave artifacts a `VersionStamp`; Phase 3 gave readers
consistent-frontier semantics. Phase 4 wires the actual dependency
graph: which artifact depends on which substrate fields and upstream
artifacts, and how a substrate change invalidates dependents.

Three concepts:

  - **DependencyKey**: a typed identifier that names *what* an artifact
    depends on. Either a substrate leaf (`Substrate(node_id, field)`)
    or an upstream interpretation artifact (`Feature(...)`,
    `Embedding(...)`, `Signature(...)`, `Canonical(...)`,
    `Proposal(...)`, `RankerOutput(...)`, `FoyerRender(...)`).

  - **DependencyTracker**: a process-wide singleton that records
    `record_dependency(artifact, dep)` edges and walks them on
    `invalidate(substrate_change)` to mark dependent artifacts dirty.
    Provides `closure(artifact)` for forensic readers.

  - **RecordingContext**: a context manager that wraps a computation
    and records every read against a `KGAccessor`-style proxy. The
    final stamp on the artifact uses the recorded dependencies.

This module ships the tracker + key taxonomy. The recording-context
hook into `views.current` is exposed via `record_dependency` calls
that producers can place in their compute paths. A future refactor
to make recording fully automatic (via a proxied accessor) is
deferred — the explicit-call API is sufficient for now and avoids
a deeper refactor of every read path.

Spec ref: from the long discussion about dependency tracking that
turned into this 5-phase plan.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# Dependency key taxonomy
# =============================================================================


class DependencyKind(str, Enum):
    """Kinds of things an artifact can depend on.

    Substrate leaves are the only sources of truth; everything else is
    an interpretation artifact and depends transitively on substrate.
    """

    # Substrate leaves
    SUBSTRATE_NODE_FIELD = "substrate.node_field"
    SUBSTRATE_EDGE_FIELD = "substrate.edge_field"
    SUBSTRATE_NODE_KIND_SET = "substrate.node_kind_set"  # coarse: any node of a kind
    SUBSTRATE_DRAWER = "substrate.drawer"
    """Track 6C+4A bridge — drawer-level dependency. A consumer that
    reads a drawer's content (verbatim, audio blob, paralinguistic
    features) records a SUBSTRATE_DRAWER dep with the drawer_id.
    DrawerInvalidated events propagate through this kind."""

    # Interpretation nodes
    FEATURE = "feature"
    EMBEDDING = "embedding"
    CANONICAL = "canonical"
    SIGNATURE = "signature"
    PROPOSAL = "proposal"
    FINDING = "finding"
    RANKER_OUTPUT = "ranker_output"
    FOYER_RENDER = "foyer_render"


@dataclass(frozen=True)
class DependencyKey:
    """Typed identifier for a dependency.

    Two keys are equal if they have the same `kind` and `identity`.
    `identity` is a tuple so it's hashable and comparable. The shapes
    by kind:

      SUBSTRATE_NODE_FIELD: (node_id, field_name)
      SUBSTRATE_EDGE_FIELD: (edge_id, field_name)
      SUBSTRATE_NODE_KIND_SET: (node_kind,)
      FEATURE:               (node_id, feature_name)
      EMBEDDING:             (node_id, embedder_version)
      CANONICAL:             (domain, canonical_id)
      SIGNATURE:             (period_id, snapshot_id)
      PROPOSAL:              (proposal_id,)
      FINDING:               (match_id,)
      RANKER_OUTPUT:         (query_hash, ranker_name)
      FOYER_RENDER:          (canon_node_id, inputs_hash)
    """

    kind: DependencyKind
    identity: tuple[Any, ...]

    def to_str(self) -> str:
        """Stable string form for hashing in version snapshots."""
        ident = ":".join(str(p) for p in self.identity)
        return f"{self.kind.value}:{ident}"


# Convenience constructors
def substrate_field(node_id: str, field_name: str) -> DependencyKey:
    return DependencyKey(DependencyKind.SUBSTRATE_NODE_FIELD, (node_id, field_name))


def edge_field(edge_id: str, field_name: str) -> DependencyKey:
    return DependencyKey(DependencyKind.SUBSTRATE_EDGE_FIELD, (edge_id, field_name))


def node_kind_set(node_kind: str) -> DependencyKey:
    return DependencyKey(DependencyKind.SUBSTRATE_NODE_KIND_SET, (node_kind,))


def substrate_drawer(drawer_id: str) -> DependencyKey:
    """Dependency on a drawer's substrate content.

    A consumer that reads a drawer's verbatim/audio_blob/paralinguistic
    features records this dep with the drawer_id. DrawerInvalidated
    events propagate through it; cached artifacts that depended on the
    drawer evict.

    Track 6C added user-facing drawer invalidation; Track 4A added the
    cache that needs eviction. This dep kind bridges them.
    """
    return DependencyKey(DependencyKind.SUBSTRATE_DRAWER, (drawer_id,))


def feature_key(node_id: str, feature_name: str) -> DependencyKey:
    return DependencyKey(DependencyKind.FEATURE, (node_id, feature_name))


def embedding_key(node_id: str, embedder_version: str) -> DependencyKey:
    return DependencyKey(DependencyKind.EMBEDDING, (node_id, embedder_version))


def canonical_key(domain: str, canonical_id: str) -> DependencyKey:
    return DependencyKey(DependencyKind.CANONICAL, (domain, canonical_id))


def signature_key(period_id: str, snapshot_id: str) -> DependencyKey:
    return DependencyKey(DependencyKind.SIGNATURE, (period_id, snapshot_id))


def proposal_key(proposal_id: str) -> DependencyKey:
    return DependencyKey(DependencyKind.PROPOSAL, (proposal_id,))


def finding_key(match_id: str) -> DependencyKey:
    return DependencyKey(DependencyKind.FINDING, (match_id,))


def ranker_output_key(query_hash: str, ranker_name: str) -> DependencyKey:
    return DependencyKey(DependencyKind.RANKER_OUTPUT, (query_hash, ranker_name))


def ranker_output_pattern_key(
    query_hash: str,
    ranker_name: str,
    cluster_signature: str,
) -> DependencyKey:
    """Track 4A — distinct ranker-output cache key per cluster pattern.

    Per HANDLES_DESIGN.md v2 §"Cluster-pattern caching": the same
    (query, ranker) pair under different cluster traversal patterns
    can produce different correct answers. Default-distinct keys
    prevent collision; the projection cache (Track 4B) layers on top
    to merge keys after observed equivalence.

    `cluster_signature` is the stable hash from
    `ClusterTraversalPattern.cluster_signature()`. The empty string
    has no special meaning in this constructor — pass a real
    signature.

    Note: keys produced by this function are DISTINCT from those
    produced by `ranker_output_key(query_hash, ranker_name)`. The
    same (query_hash, ranker_name) pair has at most one entry in the
    pattern-free cache and N entries in the patterned cache (one per
    observed cluster_signature). Callers choose which cache they
    want by which key constructor they use.
    """
    return DependencyKey(
        DependencyKind.RANKER_OUTPUT,
        (query_hash, ranker_name, cluster_signature),
    )


def foyer_render_key(canon_node_id: str, inputs_hash: str) -> DependencyKey:
    return DependencyKey(DependencyKind.FOYER_RENDER, (canon_node_id, inputs_hash))


# =============================================================================
# DependencyTracker
# =============================================================================


@dataclass
class InvalidationReport:
    """Result of a `DependencyTracker.invalidate` call."""

    invalidated_keys: list[DependencyKey] = field(default_factory=list)
    closure_size: int = 0

    @property
    def count(self) -> int:
        return len(self.invalidated_keys)


class DependencyTracker:
    """Process-wide tracker of artifact-dependency edges.

    Two indices:
      - `_deps_of[artifact]`: forward map; artifact → set of deps
      - `_deps_dependents[dep]`: inverse map; dep → set of artifacts

    Plus a `_dirty` set for artifacts known to be stale.

    Thread-safe. All operations take an internal lock; expected to be
    called from many concurrent reader/writer threads.
    """

    def __init__(self) -> None:
        self._deps_of: dict[DependencyKey, set[DependencyKey]] = {}
        self._deps_dependents: dict[DependencyKey, set[DependencyKey]] = {}
        self._dirty: set[DependencyKey] = set()
        self._lock = threading.Lock()

    # ---- recording ---------------------------------------------------------

    def record_dependency(
        self,
        artifact: DependencyKey,
        depends_on: DependencyKey,
    ) -> None:
        """Record that `artifact` reads `depends_on` during compute.

        Idempotent: re-recording the same edge is a no-op.
        Calling this for an artifact replaces nothing — to remove old
        deps when an artifact is recomputed, call
        `clear_dependencies(artifact)` first.
        """
        with self._lock:
            self._deps_of.setdefault(artifact, set()).add(depends_on)
            self._deps_dependents.setdefault(depends_on, set()).add(artifact)

    def record_dependencies(
        self,
        artifact: DependencyKey,
        deps: Iterable[DependencyKey],
    ) -> None:
        """Bulk-record. Useful at the end of a computation context."""
        with self._lock:
            forward = self._deps_of.setdefault(artifact, set())
            for d in deps:
                forward.add(d)
                self._deps_dependents.setdefault(d, set()).add(artifact)

    def clear_dependencies(self, artifact: DependencyKey) -> None:
        """Remove all edges recorded for an artifact (because it's
        being recomputed). Both forward and inverse indices are
        updated."""
        with self._lock:
            old_deps = self._deps_of.pop(artifact, set())
            for d in old_deps:
                dependents = self._deps_dependents.get(d)
                if dependents is not None:
                    dependents.discard(artifact)
                    if not dependents:
                        del self._deps_dependents[d]

    # ---- invalidation ------------------------------------------------------

    def invalidate(
        self,
        changed: DependencyKey,
        *,
        propagate: bool = True,
    ) -> InvalidationReport:
        """Mark every artifact that depends on `changed` as dirty.

        `propagate=True` walks the *transitive* closure: if an artifact
        is invalidated and has its own dependents, those are
        invalidated too. This is the default — a substrate change
        propagates through the full interpretation graph.

        `propagate=False` invalidates only the immediate dependents.
        Useful for diagnostic purposes ("who reads this directly?").

        Returns the InvalidationReport with the full list of dirtied
        keys.
        """
        invalidated: list[DependencyKey] = []
        seen: set[DependencyKey] = set()

        with self._lock:
            stack: list[DependencyKey] = [changed]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                dependents = self._deps_dependents.get(cur, set())
                for d in dependents:
                    if d not in self._dirty:
                        self._dirty.add(d)
                        invalidated.append(d)
                    if propagate:
                        stack.append(d)
        return InvalidationReport(
            invalidated_keys=invalidated,
            closure_size=len(seen) - 1,  # exclude `changed` itself
        )

    def is_dirty(self, artifact: DependencyKey) -> bool:
        with self._lock:
            return artifact in self._dirty

    def mark_clean(self, artifact: DependencyKey) -> None:
        """Mark an artifact as clean (typically called after recompute).
        The recompute should also re-record dependencies if they
        changed."""
        with self._lock:
            self._dirty.discard(artifact)

    # ---- introspection -----------------------------------------------------

    def dependencies_of(self, artifact: DependencyKey) -> set[DependencyKey]:
        """Direct dependencies of an artifact."""
        with self._lock:
            return set(self._deps_of.get(artifact, set()))

    def dependents_of(self, dep: DependencyKey) -> set[DependencyKey]:
        """Direct dependents (artifacts that read this dep)."""
        with self._lock:
            return set(self._deps_dependents.get(dep, set()))

    def closure(
        self, artifact: DependencyKey, *, max_depth: int = 1024,
    ) -> set[DependencyKey]:
        """Return the full transitive set of things this artifact
        depends on (recursively).

        Used for forensic readers that want to answer "what produced
        this artifact?" and for cycle detection in tests.
        """
        with self._lock:
            seen: set[DependencyKey] = set()
            stack: list[DependencyKey] = [artifact]
            depth = 0
            while stack and depth < max_depth:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(self._deps_of.get(cur, set()))
                depth += 1
        seen.discard(artifact)
        return seen

    def reverse_closure(
        self, dep: DependencyKey, *, max_depth: int = 1024,
    ) -> set[DependencyKey]:
        """Return the full transitive set of things that depend on
        this dep (recursively).

        Symmetric to `closure` but walks the inverse direction.
        Equivalent to what `invalidate(dep, propagate=True)` would
        dirty, but read-only.
        """
        with self._lock:
            seen: set[DependencyKey] = set()
            stack: list[DependencyKey] = [dep]
            depth = 0
            while stack and depth < max_depth:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(self._deps_dependents.get(cur, set()))
                depth += 1
        seen.discard(dep)
        return seen

    def stats(self) -> dict[str, int]:
        """Return basic counts (for diagnostics)."""
        with self._lock:
            return {
                "artifacts_tracked": len(self._deps_of),
                "deps_tracked": len(self._deps_dependents),
                "dirty_count": len(self._dirty),
                "total_edges": sum(len(v) for v in self._deps_of.values()),
            }


# =============================================================================
# Process-wide singleton
# =============================================================================


_TRACKER: DependencyTracker | None = None
_TRACKER_LOCK = threading.Lock()


def get_dependency_tracker() -> DependencyTracker:
    """Return the process-wide DependencyTracker, initializing if needed."""
    global _TRACKER
    with _TRACKER_LOCK:
        if _TRACKER is None:
            _TRACKER = DependencyTracker()
        return _TRACKER


def set_dependency_tracker(tracker: DependencyTracker | None) -> None:
    """Replace the process-wide tracker (test hook)."""
    global _TRACKER
    with _TRACKER_LOCK:
        _TRACKER = tracker


# =============================================================================
# RecordingContext — explicit context for collecting deps during compute
# =============================================================================


class RecordingContext:
    """Context manager that collects dependencies as a producer reads.

    Usage:

        with RecordingContext() as rc:
            x = read_substrate_field(node_id, "weight")
            rc.read(substrate_field(node_id, "weight"))
            ...
        tracker.record_dependencies(artifact_key, rc.collected)

    The context just collects keys into a list. A future refactor
    could replace the explicit `rc.read()` calls with a proxied
    accessor that records automatically — but the explicit API is
    sufficient for the current spec.
    """

    def __init__(self) -> None:
        self.collected: list[DependencyKey] = []

    def read(self, key: DependencyKey) -> None:
        """Record a read. Idempotent within a single context."""
        if key not in self.collected:
            self.collected.append(key)

    def reads(self, keys: Iterable[DependencyKey]) -> None:
        for k in keys:
            self.read(k)

    def __enter__(self) -> "RecordingContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # No teardown — caller decides what to do with `collected`.
        return None


__all__ = [
    "DependencyKey",
    "DependencyKind",
    "DependencyTracker",
    "InvalidationReport",
    "RecordingContext",
    "canonical_key",
    "edge_field",
    "embedding_key",
    "feature_key",
    "finding_key",
    "foyer_render_key",
    "get_dependency_tracker",
    "node_kind_set",
    "proposal_key",
    "ranker_output_key",
    "ranker_output_pattern_key",
    "set_dependency_tracker",
    "signature_key",
    "substrate_drawer",
    "substrate_field",
]
