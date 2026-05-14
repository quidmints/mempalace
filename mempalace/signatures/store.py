"""
Signature snapshot store.

Per R3 §8.1 / §8.2: a "signature" is a periodic snapshot of structural
features that can be compared *against the user's own past* (self-baseline
tracking) or used as a *triage indicator* before deeper matching layers.

The "unusual axes alignment as primary match signal" framing from earlier
revisions is dropped — empirical evidence for cross-user signature
alignment is weak. Two legitimate uses:

  1. self-baseline tracking — drift detection, behavior-vs-baseline markets
  2. triage indicator       — Layer 1 pre-filter with feedback loop

What's in a snapshot (§8.2):

  - Mean position in embedding space (per theme, per period)
  - Velocity field (per-theme velocity over recent windows)
  - Schema fingerprint (canonical schemas at canonical-projection level)
  - Contradiction-resolution profile
  - Fork-significance pattern (per-theme distribution over fork scores)

Spec ref: R3 §8.1, §8.2.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..schema.versioning import VersionStamp, make_stamp


# =============================================================================
# Snapshot dataclass
# =============================================================================


@dataclass
class SignatureSnapshot:
    """One snapshot of the signature for a specific period.

    All vectors and dicts are keyed by canonicalized region ids
    (theme_id / period_id) so snapshots from different times are
    directly comparable as long as the canonicalizer is stable.
    """

    schema_version: str = "signature.v1"
    snapshot_id: str = ""                       # sig_<id>
    period_id: str = ""                         # the period this snapshot covers
    captured_at_ms: int = 0
    window_start_ms: int = 0
    window_end_ms: int = 0

    # § 8.2 fields ----------------------------------------------------------

    # theme_id → centroid vector (typically D ~ 256–768)
    mean_position_by_theme: dict[str, list[float]] = field(default_factory=dict)

    # theme_id → velocity scalar (rate of new drawers in this theme)
    velocity_by_theme: dict[str, float] = field(default_factory=dict)

    # set of canonical-schema fingerprints active in this period
    schema_fingerprints: list[str] = field(default_factory=list)

    # contradiction-resolution profile statistics
    contradiction_profile: dict[str, float] = field(default_factory=dict)
    # keys: "contradictions_seen", "contradictions_resolved",
    #       "mean_resolution_latency_days", "resolution_strategy_split"

    # theme_id → bucketed fork-significance distribution (length B = 5)
    fork_distribution_by_theme: dict[str, list[float]] = field(default_factory=dict)

    # totals
    drawer_count: int = 0
    assertion_count: int = 0

    # Phase 2: content-version stamp. Empty by default for backwards
    # compatibility; build_signature_snapshot fills it in.
    version_stamp: "VersionStamp" = field(
        default_factory=lambda: VersionStamp(),
    )


def _digest_inputs(d: dict[str, Any]) -> str:
    return hashlib.blake2b(
        json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        digest_size=16,
    ).hexdigest()


def _make_snapshot_id(period_id: str, captured_at_ms: int) -> str:
    digest = _digest_inputs({"p": period_id, "t": captured_at_ms})
    return f"sig_{digest[:16]}"


# =============================================================================
# Builder
# =============================================================================


def build_signature_snapshot(
    *,
    period_id: str,
    window_start_ms: int,
    window_end_ms: int,
    mean_position_by_theme: dict[str, list[float]] | None = None,
    velocity_by_theme: dict[str, float] | None = None,
    schema_fingerprints: Iterable[str] | None = None,
    contradiction_profile: dict[str, float] | None = None,
    fork_distribution_by_theme: dict[str, list[float]] | None = None,
    drawer_count: int = 0,
    assertion_count: int = 0,
    now_ms: int | None = None,
    log_offset: int = 0,
    dependencies: list[tuple[str, int]] | None = None,
) -> SignatureSnapshot:
    """Build a signature snapshot.

    Phase 2: stamps the snapshot with a VersionStamp derived from the
    canonical-bytes representation of its content + the provided log
    offset and dependency list. `log_offset` and `dependencies` default
    to "unknown / empty" — callers that have those values should pass
    them so downstream readers can reason about staleness.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    snap = SignatureSnapshot(
        snapshot_id=_make_snapshot_id(period_id, now_ms),
        period_id=period_id,
        captured_at_ms=now_ms,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        mean_position_by_theme=dict(mean_position_by_theme or {}),
        velocity_by_theme=dict(velocity_by_theme or {}),
        schema_fingerprints=list(schema_fingerprints or []),
        contradiction_profile=dict(contradiction_profile or {}),
        fork_distribution_by_theme=dict(fork_distribution_by_theme or {}),
        drawer_count=drawer_count,
        assertion_count=assertion_count,
    )

    # Compute content hash from a canonical-bytes serialization. We
    # exclude `version_stamp` itself (it's about-to-be-set) and
    # `snapshot_id` (it's a uuid; identical-content snapshots taken
    # at different timestamps would otherwise hash differently).
    canonical = {
        "period_id": snap.period_id,
        "window_start_ms": snap.window_start_ms,
        "window_end_ms": snap.window_end_ms,
        "mean_position_by_theme": snap.mean_position_by_theme,
        "velocity_by_theme": snap.velocity_by_theme,
        "schema_fingerprints": sorted(snap.schema_fingerprints),
        "contradiction_profile": snap.contradiction_profile,
        "fork_distribution_by_theme": snap.fork_distribution_by_theme,
        "drawer_count": snap.drawer_count,
        "assertion_count": snap.assertion_count,
    }
    content_bytes = json.dumps(canonical, sort_keys=True).encode("utf-8")
    snap.version_stamp = make_stamp(
        content=content_bytes,
        log_offset=log_offset,
        dependencies=dependencies or [],
    )
    return snap


# =============================================================================
# Distance metrics
# =============================================================================


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def signature_similarity(a: SignatureSnapshot, b: SignatureSnapshot) -> dict[str, float]:
    """Compute a per-dimension similarity dict between two snapshots.

    Used by both the self-drift detector (a vs an older snapshot) and
    the triage indicator (a vs a foreign snapshot, with all caveats
    about cross-user alignment).

    Returns a dict with per-axis scores in [0, 1]. Caller composes them
    into an overall metric (the triage indicator applies its
    learned-weights here; the drift detector inverts to a distance).
    """
    common_themes = (
        set(a.mean_position_by_theme.keys()) & set(b.mean_position_by_theme.keys())
    )
    if common_themes:
        sim_mean = sum(
            max(0.0, _cosine(a.mean_position_by_theme[t], b.mean_position_by_theme[t]))
            for t in common_themes
        ) / len(common_themes)
    else:
        sim_mean = 0.0

    if a.velocity_by_theme and b.velocity_by_theme:
        keys = set(a.velocity_by_theme) & set(b.velocity_by_theme)
        if keys:
            num = sum(
                min(a.velocity_by_theme[k], b.velocity_by_theme[k]) for k in keys
            )
            den = sum(
                max(a.velocity_by_theme[k], b.velocity_by_theme[k]) for k in keys
            )
            sim_vel = num / den if den > 0 else 0.0
        else:
            sim_vel = 0.0
    else:
        sim_vel = 0.0

    schema_a = set(a.schema_fingerprints)
    schema_b = set(b.schema_fingerprints)
    union = schema_a | schema_b
    sim_schema = (len(schema_a & schema_b) / len(union)) if union else 0.0

    cp_a = a.contradiction_profile
    cp_b = b.contradiction_profile
    common = set(cp_a) & set(cp_b)
    if common:
        # 1 minus mean absolute relative difference, clipped
        diffs = []
        for k in common:
            denom = max(abs(cp_a[k]), abs(cp_b[k]), 1e-9)
            diffs.append(min(1.0, abs(cp_a[k] - cp_b[k]) / denom))
        sim_contra = max(0.0, 1.0 - (sum(diffs) / len(diffs)))
    else:
        sim_contra = 0.0

    common_themes_fork = (
        set(a.fork_distribution_by_theme) & set(b.fork_distribution_by_theme)
    )
    if common_themes_fork:
        sims = []
        for t in common_themes_fork:
            sims.append(
                max(
                    0.0,
                    _cosine(
                        a.fork_distribution_by_theme[t],
                        b.fork_distribution_by_theme[t],
                    ),
                )
            )
        sim_fork = sum(sims) / len(sims)
    else:
        sim_fork = 0.0

    return {
        "mean_position": sim_mean,
        "velocity": sim_vel,
        "schema_fingerprint": sim_schema,
        "contradiction_profile": sim_contra,
        "fork_significance": sim_fork,
    }


# =============================================================================
# SignatureStore — accumulates snapshots over time
# =============================================================================


class SignatureStore:
    """In-memory store for signature snapshots, keyed by period_id.

    Phase 1 sub-slice 3: optional idempotency_key on `put()` for
    retry-safe writes; quarantine support for torn-batch recovery.

    A signature is normally produced once per period per scheduled
    snapshot run. The same `(consumer_id, batch_id, output_index)`
    tuple identifies a snapshot uniquely; a retry with the same key
    is a no-op (the existing snapshot wins).
    """

    def __init__(self) -> None:
        self._by_period: dict[str, SignatureSnapshot] = {}
        self._chronological: list[SignatureSnapshot] = []
        # idempotency key → snapshot_id (for retry dedup)
        self._by_idempotency_key: dict[tuple[str, str, int], str] = {}
        # batch_id → set[snapshot_id] (for quarantine on recovery)
        self._by_batch_id: dict[str, set[str]] = {}
        # Snapshots that came from torn batches — excluded from default reads
        self._torn_snapshot_ids: set[str] = set()
        self._lock = threading.Lock()

    def put(
        self,
        snap: SignatureSnapshot,
        *,
        idempotency_key: tuple[str, str, int] | None = None,
    ) -> bool:
        """Insert a snapshot. Returns True if newly inserted, False if
        deduplicated by idempotency_key."""
        with self._lock:
            if idempotency_key is not None:
                seen_id = self._by_idempotency_key.get(idempotency_key)
                if seen_id is not None:
                    # Retry of an earlier put — no-op
                    return False
                self._by_idempotency_key[idempotency_key] = snap.snapshot_id
                _, batch_id, _ = idempotency_key
                self._by_batch_id.setdefault(batch_id, set()).add(snap.snapshot_id)
            self._by_period[snap.period_id] = snap
            self._chronological.append(snap)
            return True

    def quarantine_torn_batches(self, torn_batch_ids: set[str]) -> int:
        """Mark snapshots from torn batches as torn.
        Default reads exclude them; `chronological_including_torn`
        keeps them visible for diagnostics."""
        n = 0
        with self._lock:
            for bid in torn_batch_ids:
                ids = self._by_batch_id.get(bid, set())
                new_torn = ids - self._torn_snapshot_ids
                self._torn_snapshot_ids.update(new_torn)
                n += len(new_torn)
        return n

    def get(self, period_id: str) -> SignatureSnapshot | None:
        with self._lock:
            snap = self._by_period.get(period_id)
            if snap is None or snap.snapshot_id in self._torn_snapshot_ids:
                return None
            return snap

    def chronological(self) -> list[SignatureSnapshot]:
        """Return all non-torn snapshots ordered by captured_at_ms."""
        with self._lock:
            return sorted(
                (s for s in self._chronological
                 if s.snapshot_id not in self._torn_snapshot_ids),
                key=lambda s: s.captured_at_ms,
            )

    def chronological_including_torn(self) -> list[SignatureSnapshot]:
        """All snapshots including those quarantined from torn batches.
        Diagnostic use only."""
        with self._lock:
            return sorted(self._chronological, key=lambda s: s.captured_at_ms)

    def latest(self) -> SignatureSnapshot | None:
        chrono = self.chronological()
        return chrono[-1] if chrono else None

    def window(self, start_ms: int, end_ms: int) -> list[SignatureSnapshot]:
        return [
            s for s in self.chronological()
            if start_ms <= s.captured_at_ms <= end_ms
        ]

    def size(self) -> int:
        """Count of non-torn snapshots."""
        with self._lock:
            return sum(
                1 for s in self._chronological
                if s.snapshot_id not in self._torn_snapshot_ids
            )

    def size_including_torn(self) -> int:
        with self._lock:
            return len(self._chronological)


_STORE: SignatureStore | None = None
_STORE_LOCK = threading.Lock()


def get_signature_store() -> SignatureStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SignatureStore()
        return _STORE


def set_signature_store(store: SignatureStore) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store


__all__ = [
    "SignatureSnapshot",
    "SignatureStore",
    "build_signature_snapshot",
    "get_signature_store",
    "set_signature_store",
    "signature_similarity",
]
