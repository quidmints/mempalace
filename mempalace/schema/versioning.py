"""
Content-version stamps for cached interpretation artifacts (Phase 2).

Every cached interpretation artifact (a feature value, an embedding, a
signature snapshot, a canonical mapping, a foyer render, a finding,
etc.) carries a `VersionStamp`:

  - `content_hash`         : 32-byte BLAKE2b of the artifact's payload.
                              Two artifacts with identical payloads
                              have identical content_hashes regardless
                              of when they were computed. Used for
                              "did this actually change?" memoization.

  - `computed_at_log_offset`: log offset that was the substrate state
                              when the artifact was computed. Two
                              artifacts with the same content_hash but
                              different offsets are equally valid;
                              earlier offset is preferred for
                              deduplication.

  - `dependency_version_snapshot_hash`:
                              32-byte BLAKE2b of the *sorted* list of
                              `(dependency_key, version)` pairs the
                              artifact read during computation. Two
                              artifacts with the same hash share an
                              identical dependency frontier, so the
                              dirty/stale check is a single hash
                              comparison.

The full snapshot (the unhashed list of pairs) is kept in
`dependency_version_snapshot`; the hash is the hot-path field. The
snapshot is what diagnostic / forensic readers consult to ask "exactly
what versions of what fields produced this artifact?"

This module ships only the data structures + utilities. Phase 3 wires
artifacts to populate them; Phase 4 turns the snapshot into a real
dependency tracker.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable


# Hash size and algorithm — single choice locked across the codebase.
HASH_DIGEST_SIZE = 32
EMPTY_CONTENT_HASH = b"\x00" * HASH_DIGEST_SIZE
EMPTY_DEPENDENCY_HASH = b"\x00" * HASH_DIGEST_SIZE


def hash_content(payload: bytes) -> bytes:
    """Single canonical content-hash function."""
    return hashlib.blake2b(payload, digest_size=HASH_DIGEST_SIZE).digest()


def hash_dependency_snapshot(
    snapshot: Iterable[tuple[str, int]],
) -> bytes:
    """Hash a `(dependency_key, version)` list.

    Sorted before hashing so the hash is stable regardless of insertion
    order. `dependency_key` is opaque to this module — it's typically
    `f"{kind}:{node_id}:{field_name}"` for substrate dependencies, or
    `f"{kind}:{artifact_id}"` for upstream-artifact dependencies.

    Empty input → EMPTY_DEPENDENCY_HASH (so artifacts with no recorded
    dependencies have a stable, recognizable hash).
    """
    sorted_pairs = sorted(snapshot)
    if not sorted_pairs:
        return EMPTY_DEPENDENCY_HASH
    h = hashlib.blake2b(digest_size=HASH_DIGEST_SIZE)
    for key, version in sorted_pairs:
        # Pipe separators with explicit length prefixes prevent
        # "ab|c|2" colliding with "a|bc|2"
        h.update(len(key).to_bytes(4, "big"))
        h.update(key.encode("utf-8"))
        h.update(version.to_bytes(8, "big", signed=True))
    return h.digest()


@dataclass
class VersionStamp:
    """Version stamp embedded in every cached interpretation artifact.

    Fields:
      content_hash:
        32-byte BLAKE2b of the artifact payload. Empty bytes (
        `EMPTY_CONTENT_HASH`) means "not yet stamped" — artifacts in
        flight may transiently have empty stamps. Production reads
        should fail-fast if they see empty.

      computed_at_log_offset:
        The log offset that was current when this artifact was computed.
        0 means "unknown / not yet stamped".

      dependency_version_snapshot_hash:
        32-byte BLAKE2b over the sorted dependency_version_snapshot.
        Equality of this hash is the hot-path "are dependencies the
        same?" check.

      dependency_version_snapshot:
        The unhashed list of `(dependency_key, version)` pairs. Kept
        for forensic readers. May be left empty in production hot-path
        artifacts; in that case, the hash is still meaningful as a
        capture-time fingerprint, but the snapshot is unrecoverable
        from the artifact alone.

      stamp_schema_version:
        Allows for future evolution. "v1" is the only valid value
        today; later versions might use SHA3-256 or include richer
        provenance fields.
    """

    content_hash: bytes = EMPTY_CONTENT_HASH
    computed_at_log_offset: int = 0
    dependency_version_snapshot_hash: bytes = EMPTY_DEPENDENCY_HASH
    dependency_version_snapshot: list[tuple[str, int]] = field(default_factory=list)
    stamp_schema_version: str = "v1"

    @property
    def is_stamped(self) -> bool:
        """True if the stamp has a real content_hash."""
        return self.content_hash != EMPTY_CONTENT_HASH

    @property
    def is_dirty_relative_to(self) -> Any:
        """Convenience: a sentinel dirty check is just hash inequality.
        Callers compare stamps directly; this property is documentation."""
        return None

    def matches_content(self, other: "VersionStamp") -> bool:
        """True if both stamps refer to the same artifact payload."""
        return self.content_hash == other.content_hash

    def matches_dependencies(self, other: "VersionStamp") -> bool:
        """True if both stamps were computed against the same
        dependency frontier."""
        return (
            self.dependency_version_snapshot_hash
            == other.dependency_version_snapshot_hash
        )

    def is_stale_against(
        self,
        current_dependency_versions: dict[str, int],
    ) -> bool:
        """Check if this stamp's recorded dependency versions still
        match the current versions of those dependencies.

        Returns True if any recorded dependency has moved to a higher
        version, OR if the snapshot is empty (we can't tell, so
        conservatively return True — stale unless proven otherwise).

        For the hot-path equivalence check, prefer
        `matches_dependencies(other)` — it's O(1) and doesn't need
        the full current-versions map.
        """
        if not self.dependency_version_snapshot:
            return True
        for key, recorded_v in self.dependency_version_snapshot:
            current_v = current_dependency_versions.get(key)
            if current_v is None:
                # Dependency disappeared — that's a change
                return True
            if current_v > recorded_v:
                return True
        return False


def make_stamp(
    *,
    content: bytes,
    log_offset: int,
    dependencies: Iterable[tuple[str, int]] = (),
) -> VersionStamp:
    """Build a stamp from raw inputs.

    `content` is the canonical-bytes representation of the artifact
    payload (json.dumps with sort_keys, msgpack, etc — caller's
    choice; what matters is that two artifacts with the same logical
    payload produce the same bytes).

    `log_offset` is the substrate state at compute time.

    `dependencies` is the iterable of `(dependency_key, version)`
    pairs that were read.
    """
    deps_list = list(dependencies)
    return VersionStamp(
        content_hash=hash_content(content),
        computed_at_log_offset=log_offset,
        dependency_version_snapshot_hash=hash_dependency_snapshot(deps_list),
        dependency_version_snapshot=sorted(deps_list),
        stamp_schema_version="v1",
    )


# =============================================================================
# Helper: substrate-version tracker
#
# This is the minimal in-module tracker — it pairs with the richer
# `DependencyTracker` in `mempalace.derived.dependency` (Phase 4),
# which provides the full forward+inverse dependency graph and
# transitive invalidation. Use this `SubstrateVersionTracker` when
# you only need per-field version numbers (e.g. for stamping) and
# the `DependencyTracker` when you need to know who-depends-on-what.
#
# The tracker is populated by the view subscriber (which sees every
# event); see Phase 3 for the wiring.
# =============================================================================


@dataclass
class SubstrateVersionTracker:
    """Track per-field versions of substrate state.

    A "version" is the log offset of the most recent event that
    mutated that field. Fields that have never been touched return
    version 0.

    Thread-safe via internal lock.
    """

    _versions: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import threading
        self._lock = threading.Lock()

    def record_change(self, dependency_key: str, log_offset: int) -> None:
        with self._lock:
            current = self._versions.get(dependency_key, 0)
            if log_offset > current:
                self._versions[dependency_key] = log_offset

    def version_of(self, dependency_key: str) -> int:
        with self._lock:
            return self._versions.get(dependency_key, 0)

    def snapshot(self, keys: Iterable[str]) -> list[tuple[str, int]]:
        """Take a snapshot of the current versions for the given keys.
        Returns sorted list."""
        with self._lock:
            return sorted(
                (k, self._versions.get(k, 0)) for k in keys
            )

    def all_versions(self) -> dict[str, int]:
        with self._lock:
            return dict(self._versions)


__all__ = [
    "EMPTY_CONTENT_HASH",
    "EMPTY_DEPENDENCY_HASH",
    "HASH_DIGEST_SIZE",
    "SubstrateVersionTracker",
    "VersionStamp",
    "hash_content",
    "hash_dependency_snapshot",
    "make_stamp",
]
