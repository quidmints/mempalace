"""
mempalace.handle — typed handle infrastructure.

This package will eventually host the full HandleState machinery
described in HANDLES_DESIGN.md v2. Currently it ships:

  - `frame` — typed `InterpretiveFrame` and the five axis dataclasses
    (`SignatureRegion`, `ConwayRate`, `CoActivationPattern`,
    `RefinementCues`, `VoiceFlavor`). Output of Track 2's
    substrate-signal analysis pass.
  - `cluster_pattern` — `Hop` + `ClusterTraversalPattern`. Track 3
    supporting types; also feeds Track 4A's cache-key derivation.
  - `search_policy` — `SearchPolicy` with `adaptive()`, `next_step()`;
    `SearchBudget`, `StepDirective`, `FrameConfidenceSummary`,
    `PolicyAdjustment`. Track 3.
  - `walk_driver` — `WalkDriver` orchestrates the policy/walk/audit
    loop; emits `WalkCompleted` audit events. Track 3.

The full HandleState shape (with `cluster_pattern`, `search_policy`,
`projection_cache_status`, etc. all wired together) hasn't yet been
promoted from the existing prototype in `mempalace/retrieve/handle.py`.
That promotion is a follow-on.

Spec ref: HANDLES_DESIGN.md v2, SUBSTRATE_SIGNAL_ANALYSIS.md.
"""

from .cluster_pattern import (
    DEFAULT_CLUSTER_WINDOW_K,
    ClusterTraversalPattern,
    Hop,
)
from .context import HandleContext
from .frame import (
    CONWAY_RATE_CLASS_1,
    CONWAY_RATE_CLASS_2,
    CONWAY_RATE_CLASS_3,
    CoActivationPattern,
    ConwayRate,
    InterpretiveFrame,
    RefinementCues,
    SignatureRegion,
    VoiceFlavor,
    default_rate_features_weight,
)
from .search_policy import (
    CLOSE_FRAMES_DELTA,
    DEFAULT_ALTERNATE_DEPTH,
    DEFAULT_BREADTH_BUDGET,
    DEFAULT_BREADTH_FANOUT,
    DEFAULT_DEPTH_BUDGET,
    DEFAULT_HOP_BUDGET,
    DOMINANT_FRAME_THRESHOLD,
    HIGH_DISPERSION_STDDEV,
    STUCK_PATTERN_HOPS,
    DirectiveKind,
    FrameConfidenceSummary,
    NoopAdjustment,
    PolicyAdjustment,
    SearchBudget,
    SearchPolicy,
    StepDirective,
    summarize_frames,
)
from .walk_driver import (
    MAX_DRIVER_STEPS,
    StepOutcome,
    WalkDriver,
    WalkExecutor,
    WalkOutcome,
)

__all__ = [
    # frame (Track 2)
    "CONWAY_RATE_CLASS_1",
    "CONWAY_RATE_CLASS_2",
    "CONWAY_RATE_CLASS_3",
    "CoActivationPattern",
    "ConwayRate",
    "InterpretiveFrame",
    "RefinementCues",
    "SignatureRegion",
    "VoiceFlavor",
    "default_rate_features_weight",
    # cluster_pattern (Track 3)
    "DEFAULT_CLUSTER_WINDOW_K",
    "ClusterTraversalPattern",
    "Hop",
    # context (HandleContext — fills the dangling reference in
    # ranker_cache.py and search_policy.py docstrings)
    "HandleContext",
    # search_policy (Track 3)
    "CLOSE_FRAMES_DELTA",
    "DEFAULT_ALTERNATE_DEPTH",
    "DEFAULT_BREADTH_BUDGET",
    "DEFAULT_BREADTH_FANOUT",
    "DEFAULT_DEPTH_BUDGET",
    "DEFAULT_HOP_BUDGET",
    "DOMINANT_FRAME_THRESHOLD",
    "DirectiveKind",
    "FrameConfidenceSummary",
    "HIGH_DISPERSION_STDDEV",
    "NoopAdjustment",
    "PolicyAdjustment",
    "STUCK_PATTERN_HOPS",
    "SearchBudget",
    "SearchPolicy",
    "StepDirective",
    "summarize_frames",
    # walk_driver (Track 3)
    "MAX_DRIVER_STEPS",
    "StepOutcome",
    "WalkDriver",
    "WalkExecutor",
    "WalkOutcome",
]
