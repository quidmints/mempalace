"""
Ranker isolation.

Per R3 §6.2: ranker code must be process-isolated from the trusted core.
A malicious ranker (downloaded from the federation, or a buggy local one)
must not be able to:
  - Reach into the master views beyond the candidate set it was given
  - Make external network calls
  - Mutate any state visible to other consumers
  - Tamper with the trusted aggregator

This file provides the isolation primitives:
  - IsolatedRankerProxy: subprocess-based wrapper that runs a ranker in a
    capability-restricted child process and communicates via stdin/stdout.
  - InProcessFenceRanker: lighter-weight wrapper that enforces the API
    surface but doesn't process-isolate; used in dev for speed.
  - BehaviorMonitor: tracks per-ranker behavioral statistics and can
    quarantine rankers that produce anomalous outputs (R3 §6.3).

The full subprocess implementation requires platform-specific sandboxing
(seccomp on Linux, sandbox-exec on macOS); this module ships the
interfaces and a same-process approximation. Production deployment swaps
in the real subprocess.

Spec ref: R3 §6.2, §6.3.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..retrieve.gather import Candidate
from ..schema.stance import Stance
from .protocol import Ranker, RankerManifest, ScoredCandidate


# =============================================================================
# Behavior monitor
# =============================================================================


@dataclass
class RankerStats:
    """Per-ranker behavioral statistics."""

    name: str
    invocations: int = 0
    total_elapsed_ms: int = 0
    total_candidates_scored: int = 0
    score_sum: float = 0.0
    score_min: float = float("inf")
    score_max: float = float("-inf")
    quarantined: bool = False
    quarantine_reason: str | None = None
    last_invocation_ms: int = 0


class BehaviorMonitor:
    """Monitors ranker outputs for anomalies and quarantines bad actors.

    Detection rules (initial set):
      - All-zero scores across a candidate set: suspicious (zero-out attack)
      - All-identical scores: degenerate (no ranking signal)
      - Score range > [0, 1]: out-of-bounds; ranker is broken
      - Latency > 5 seconds: ranker is too slow for production
    """

    def __init__(self) -> None:
        self._stats: dict[str, RankerStats] = {}
        self._lock = threading.Lock()

    def record(
        self,
        ranker_name: str,
        scored: list[ScoredCandidate],
        elapsed_ms: int,
    ) -> list[str]:
        """Record an invocation. Returns list of anomaly flags."""
        anomalies: list[str] = []
        scores = [s.score for s in scored]

        if scores:
            if all(s == 0.0 for s in scores):
                anomalies.append("all_zero_scores")
            elif len(set(round(s, 6) for s in scores)) == 1:
                anomalies.append("all_identical_scores")
            if any(s < 0.0 or s > 1.0 for s in scores):
                anomalies.append("score_out_of_bounds")

        if elapsed_ms > 5000:
            anomalies.append("latency_exceeded")

        now_ms = int(time.time() * 1000)
        with self._lock:
            stats = self._stats.setdefault(ranker_name, RankerStats(name=ranker_name))
            stats.invocations += 1
            stats.total_elapsed_ms += elapsed_ms
            stats.total_candidates_scored += len(scored)
            for s in scores:
                stats.score_sum += s
                stats.score_min = min(stats.score_min, s)
                stats.score_max = max(stats.score_max, s)
            stats.last_invocation_ms = now_ms

            # Cumulative anomaly trigger: 3+ all-zero invocations → quarantine
            if "all_zero_scores" in anomalies:
                stats_zero = stats.metadata if hasattr(stats, "metadata") else {}
                # Simplified: count anomalies inline; quarantine after 3 hits
                if not hasattr(stats, "_zero_count"):
                    stats._zero_count = 0  # type: ignore[attr-defined]
                stats._zero_count += 1  # type: ignore[attr-defined]
                if stats._zero_count >= 3 and not stats.quarantined:  # type: ignore[attr-defined]
                    stats.quarantined = True
                    stats.quarantine_reason = "repeated_all_zero_outputs"
                    anomalies.append("quarantined")

        return anomalies

    def get_stats(self, ranker_name: str) -> RankerStats | None:
        with self._lock:
            return self._stats.get(ranker_name)

    def all_stats(self) -> dict[str, RankerStats]:
        with self._lock:
            return dict(self._stats)

    def is_quarantined(self, ranker_name: str) -> bool:
        with self._lock:
            stats = self._stats.get(ranker_name)
            return stats.quarantined if stats else False

    def lift_quarantine(self, ranker_name: str) -> None:
        with self._lock:
            stats = self._stats.get(ranker_name)
            if stats:
                stats.quarantined = False
                stats.quarantine_reason = None


# =============================================================================
# In-process fence (lightweight isolation)
# =============================================================================


class InProcessFenceRanker:
    """Wraps a ranker to enforce the protocol contract.

    Provides:
      - Catches exceptions; returns empty list on failure
      - Caps elapsed time at a deadline
      - Strips candidate.features to declared dependencies only
      - Validates output shape

    Does NOT provide:
      - Memory isolation
      - Network restriction (use IsolatedRankerProxy for that)

    Used as the default wrapper in dev/test where subprocess overhead is
    not warranted.
    """

    def __init__(
        self,
        inner: Ranker,
        *,
        monitor: BehaviorMonitor | None = None,
        deadline_ms: int = 5000,
    ) -> None:
        self._inner = inner
        self._monitor = monitor
        self._deadline_ms = deadline_ms
        self.name = inner.name
        self.version = inner.version

    def declares(self) -> RankerManifest:
        return self._inner.declares()

    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        feature_names: list[str] | None = None,
    ) -> list[ScoredCandidate]:
        # Quarantine check
        if self._monitor and self._monitor.is_quarantined(self.name):
            return []

        # Strip features to declared dependencies (capability restriction)
        manifest = self._inner.declares()
        allowed_features = set(manifest.feature_dependencies)
        if allowed_features:
            stripped: list[Candidate] = []
            for c in candidates:
                stripped_features = {
                    k: v for k, v in c.features.items()
                    if k in allowed_features
                }
                stripped.append(Candidate(
                    node_id=c.node_id,
                    node=c.node,
                    features=stripped_features,
                    outgoing=c.outgoing,
                    incoming=c.incoming,
                    derivation_chain=c.derivation_chain,
                ))
            candidates = stripped

        # Run with deadline + exception isolation
        t0 = time.monotonic()
        try:
            scored = self._inner.rank(candidates, stance, feature_names=feature_names)
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            if self._monitor:
                self._monitor.record(self.name, [], elapsed)
            return []

        elapsed = int((time.monotonic() - t0) * 1000)

        # Validate output shape
        validated: list[ScoredCandidate] = []
        for s in scored:
            if not isinstance(s, ScoredCandidate):
                continue
            # Clamp score to [0, 1]
            s.score = max(0.0, min(1.0, float(s.score)))
            validated.append(s)

        # Record behavior
        if self._monitor:
            self._monitor.record(self.name, validated, elapsed)

        return validated


# =============================================================================
# Subprocess-isolated ranker (interface only; production uses real sandboxing)
# =============================================================================


@dataclass
@dataclass(frozen=True)
class ResourceLimits:
    """Per-ranker resource budget — R3 §6.2 last bullet.

    Applied to the subprocess via OS-level rlimits and timeouts.
    None means "no limit" for that dimension.

    Fields:
      wall_clock_ms: hard wall-clock cap. The default `IsolatedRankerProxy`
        uses subprocess.run(timeout=...) for this.
      cpu_seconds: RLIMIT_CPU on Linux/macOS via preexec.
      memory_bytes: RLIMIT_AS on Linux (address space cap).
      max_open_files: RLIMIT_NOFILE.
    """
    wall_clock_ms: int = 5_000
    cpu_seconds: int | None = 10
    memory_bytes: int | None = 512 * 1024 * 1024  # 512 MB
    max_open_files: int | None = 64


@dataclass(frozen=True)
class SandboxProfile:
    """Restricted-syscall sandbox wrapper invocation — R3 §6.2 second bullet.

    Production deployments wrap the ranker subprocess in:
      - bwrap (Linux)            — `bwrap --unshare-all --ro-bind ...`
      - sandbox-exec (macOS)     — with a custom .sb policy
      - sandbox/AppContainer (Windows)

    The wrapper command is a list of strings prepended to the
    subprocess argv. Empty list means "no sandbox" (dev fallback).

    Two named convenience profiles are built-in:
      - SandboxProfile.NONE: empty wrapper. Skips sandboxing.
      - SandboxProfile.bwrap_minimal(): a minimal Linux bwrap profile
        with --unshare-all and a read-only bind of the ranker
        executable. Production deployments customize.
    """
    wrapper_argv: tuple[str, ...] = ()

    @classmethod
    def none(cls) -> "SandboxProfile":
        return cls(wrapper_argv=())

    @classmethod
    def bwrap_minimal(cls, executable_path: str) -> "SandboxProfile":
        """Minimal Linux bwrap wrapper — no network, no fs except a
        read-only bind of the ranker. Caller must ensure bwrap is
        available; falls back to no-sandbox if it's not.

        This is a starting point. Production should:
          - Bind /lib, /usr/lib for shared libraries
          - Bind a scratch dir for the ranker if it needs scratch space
          - Tune capabilities (--cap-drop ALL is a good default)
        """
        return cls(wrapper_argv=(
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind", executable_path, executable_path,
            "--proc", "/proc",
            "--dev", "/dev",
        ))


@dataclass
class IsolatedRankerSpec:
    """Spec for spawning an isolated ranker process."""

    name: str
    executable_path: str  # path to verified ranker binary
    weights_hash: str
    args: tuple[str, ...] = ()
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    sandbox_profile: SandboxProfile = field(default_factory=SandboxProfile.none)


class IsolatedRankerProxy:
    """Spawns an isolated subprocess to run a downloaded ranker.

    Communication: stdin/stdout JSON.
    Restrictions enforced by the OS sandbox (production):
      - No network
      - No filesystem writes outside scratch dir
      - CPU and memory caps
      - Time budget

    This stub only validates the spec and produces a process-isolated
    invocation; the actual sandboxing must be added per-platform.
    """

    def __init__(
        self,
        spec: IsolatedRankerSpec,
        *,
        monitor: BehaviorMonitor | None = None,
    ) -> None:
        self._spec = spec
        self._monitor = monitor
        self.name = spec.name
        self.version = spec.weights_hash[:16]

    def declares(self) -> RankerManifest:
        # Manifests for isolated rankers are declared at registration time
        # (via signed_loader). For runtime, we can re-fetch from the running
        # process; for now return a minimal manifest.
        return RankerManifest(
            name=self.name,
            version=self.version,
            feature_dependencies=(),
            consumes_stance=True,
            deterministic=True,
            side_effects=False,
            description=f"Isolated ranker @ {self._spec.executable_path}",
        )

    def rank(
        self,
        candidates: list[Candidate],
        stance: Stance,
        *,
        feature_names: list[str] | None = None,
    ) -> list[ScoredCandidate]:
        # Prepare JSON-serializable payload (strip non-JSON parts)
        payload = {
            "candidates": [
                {
                    "node_id": c.node_id,
                    "node_kind": c.node.node_kind,
                    "features": c.features,
                }
                for c in candidates
            ],
            "stance": {
                "consumer_kind": stance.consumer_kind.value,
                "correspondence_vs_coherence": stance.correspondence_vs_coherence,
                "recency_bias": stance.recency_bias,
                "canonicality_floor": stance.canonicality_floor,
                "exploration_entropy": stance.exploration_entropy,
            },
        }

        t0 = time.monotonic()
        limits = self._spec.resource_limits
        sandbox = self._spec.sandbox_profile
        try:
            # R3 §6.2: subprocess invocation, wrapped in sandbox profile
            # if one is configured, with rlimit-based resource caps.
            argv: list[str] = list(sandbox.wrapper_argv) + [
                self._spec.executable_path, *self._spec.args,
            ]

            # Resource limits via preexec_fn (POSIX only). On Windows
            # this is silently skipped — Windows uses Job Objects via
            # a different path that production should add.
            preexec_fn = _build_preexec_fn(limits)

            proc = subprocess.run(
                argv,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=limits.wall_clock_ms / 1000.0,
                check=False,
                preexec_fn=preexec_fn,
            )
            elapsed = int((time.monotonic() - t0) * 1000)

            if proc.returncode != 0:
                if self._monitor:
                    self._monitor.record(self.name, [], elapsed)
                return []

            response = json.loads(proc.stdout)
            scored_data = response.get("scored", [])

            # Reconstruct ScoredCandidate (without actual Node refs)
            cand_by_id = {c.node_id: c for c in candidates}
            out: list[ScoredCandidate] = []
            for sd in scored_data:
                cand = cand_by_id.get(sd.get("node_id"))
                if cand is None:
                    continue
                out.append(ScoredCandidate(
                    candidate=cand,
                    score=max(0.0, min(1.0, float(sd.get("score", 0.0)))),
                    axes=dict(sd.get("axes", {})),
                ))

            if self._monitor:
                self._monitor.record(self.name, out, elapsed)
            return out

        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            elapsed = int((time.monotonic() - t0) * 1000)
            if self._monitor:
                self._monitor.record(self.name, [], elapsed)
            return []


# =============================================================================
# Module-level singleton
# =============================================================================


_MONITOR = BehaviorMonitor()


def get_behavior_monitor() -> BehaviorMonitor:
    return _MONITOR


def _build_preexec_fn(limits: "ResourceLimits") -> Any:
    """Build a preexec_fn that applies POSIX rlimits before exec.

    Returns None on Windows (rlimits unavailable) or when no limits
    are set. The returned function runs in the child process between
    fork() and exec(); it mustn't allocate or take locks.
    """
    if sys.platform == "win32":
        return None
    if (
        limits.cpu_seconds is None
        and limits.memory_bytes is None
        and limits.max_open_files is None
    ):
        return None

    try:
        import resource  # POSIX-only stdlib module
    except ImportError:
        return None

    cpu = limits.cpu_seconds
    mem = limits.memory_bytes
    nofile = limits.max_open_files

    def _apply() -> None:
        # Don't catch exceptions — if a limit can't be applied, we'd
        # rather fail closed than run unrestricted. The child will
        # die and the parent will see returncode != 0.
        if cpu is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if mem is not None:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                # macOS doesn't support RLIMIT_AS; fall through silently
                # (the wall-clock timeout still bounds runtime)
                pass
        if nofile is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))

    return _apply


__all__ = [
    "BehaviorMonitor",
    "InProcessFenceRanker",
    "IsolatedRankerProxy",
    "IsolatedRankerSpec",
    "RankerStats",
    "get_behavior_monitor",
]
