"""
Public manifest publishing.

Per R3 §7.5 and Part 9.2: each palace publishes a public manifest that
peers can pull to decide whether a match request is worth sending. The
manifest is intentionally a thin, non-revealing summary:

  - Theme list (canonicalized theme names that the palace owns)
  - Theme embeddings (centroid vector per theme; only theme-level)
  - Velocity-field summary (small structural snapshot; see manifold_index)
  - Schema fingerprints (set of canonicalizer schema fingerprints)
  - Enrolled key (signature root for everything)
  - Generated_at + cache TTL

What the manifest does NOT contain:

  - Drawer text, paralinguistic facets, social facets
  - Goal contents, period substrate, event substrate
  - Per-drawer embeddings (only theme centroids)

Spec ref: R3 §7.5, Part 9.2.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from .kg_sketch import KGSketch
from .manifold_index import VelocityFieldSummary


# =============================================================================
# Manifest dataclass
# =============================================================================


# 7-day cache TTL to match the federation match-cache TTL (R3 default).
DEFAULT_MANIFEST_TTL_MS = 7 * 24 * 3600 * 1000


@dataclass
class ThemeEntry:
    """One published theme entry."""

    theme_id: str                       # canonicalized theme id (or anonymized handle)
    name_fingerprint: str               # 16-hex digest of canonicalized theme name
    embedding: list[float] = field(default_factory=list)  # centroid vector
    activity_weight: float = 0.0        # current EMA from manifold_index
    drawer_count: int = 0


@dataclass
class PublicManifest:
    """The payload returned over the /mempalace/manifest/1.0.0 protocol."""

    schema_version: str = "manifest.v1"
    palace_id: str = ""
    enrolled_pubkey_hex: str = ""

    generated_at_ms: int = 0
    ttl_ms: int = DEFAULT_MANIFEST_TTL_MS

    themes: list[ThemeEntry] = field(default_factory=list)
    schema_fingerprints: list[str] = field(default_factory=list)
    velocity_field: VelocityFieldSummary | None = None
    minhash_sketch: list[int] = field(default_factory=list)

    # Self-attestation: integrity hash of the rest of the fields, signed
    # by the enrolled key. The signature itself is held adjacent (not
    # inside this dataclass) so signing/verifying stays explicit.
    content_hash_hex: str = ""

    def expired(self, *, now_ms: int | None = None) -> bool:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return now_ms - self.generated_at_ms > self.ttl_ms

    def compute_content_hash(self) -> str:
        """Compute the canonical content hash (excluding the hash field)."""
        d = asdict(self)
        d.pop("content_hash_hex", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()


# =============================================================================
# Builder
# =============================================================================


def build_manifest(
    *,
    palace_id: str,
    enrolled_pubkey_hex: str,
    themes: Iterable[ThemeEntry],
    schema_fingerprints: Iterable[str],
    velocity_field: VelocityFieldSummary | None,
    kg_sketch: KGSketch | None,
    ttl_ms: int = DEFAULT_MANIFEST_TTL_MS,
    now_ms: int | None = None,
) -> PublicManifest:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    m = PublicManifest(
        palace_id=palace_id,
        enrolled_pubkey_hex=enrolled_pubkey_hex,
        generated_at_ms=now_ms,
        ttl_ms=ttl_ms,
        themes=list(themes),
        schema_fingerprints=list(schema_fingerprints),
        velocity_field=velocity_field,
        minhash_sketch=(list(kg_sketch.signature) if kg_sketch is not None else []),
    )
    m.content_hash_hex = m.compute_content_hash()
    return m


# =============================================================================
# Manifest store — in-process publishing + cache of foreign manifests
# =============================================================================


class ManifestStore:
    """Holds the local manifest and cached foreign manifests."""

    def __init__(self) -> None:
        self._local: PublicManifest | None = None
        self._foreign: dict[str, PublicManifest] = {}  # palace_id -> manifest
        self._lock = threading.Lock()

    # ---- local --------------------------------------------------------------

    def set_local(self, manifest: PublicManifest) -> None:
        with self._lock:
            self._local = manifest

    def get_local(self) -> PublicManifest | None:
        with self._lock:
            return self._local

    # ---- foreign ------------------------------------------------------------

    def cache_foreign(self, manifest: PublicManifest) -> None:
        with self._lock:
            self._foreign[manifest.palace_id] = manifest

    def get_foreign(self, palace_id: str) -> PublicManifest | None:
        with self._lock:
            m = self._foreign.get(palace_id)
            if m is None:
                return None
            if m.expired():
                del self._foreign[palace_id]
                return None
            return m

    def evict_expired(self) -> int:
        with self._lock:
            stale = [pid for pid, m in self._foreign.items() if m.expired()]
            for pid in stale:
                del self._foreign[pid]
            return len(stale)


# =============================================================================
# Module-level singleton
# =============================================================================


_STORE: ManifestStore | None = None
_STORE_LOCK = threading.Lock()


def get_manifest_store() -> ManifestStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ManifestStore()
        return _STORE


def set_manifest_store(store: ManifestStore) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store


__all__ = [
    "DEFAULT_MANIFEST_TTL_MS",
    "ManifestStore",
    "PublicManifest",
    "ThemeEntry",
    "build_manifest",
    "get_manifest_store",
    "set_manifest_store",
]
