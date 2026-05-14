"""
mempalace.miner — three-class miner with provisional/confirmed/rejected
lifecycle and per-(output, consumer) feedback ledger.

Per Part 10.5 / 10.6 / R3 §9.1: the miner runs in three classes at three
cadences, mapping to Conway's two-systems consolidation pattern.

Submodules:

  base       — MinerPass abstract, ProposalLifecycle enum, PassResult
  class1     — Streaming, per-drawer enrichment (memory_type, affect, etc.)
  class2     — Periodic, cross-drawer aggregation (assertions, contradictions,
               event boundaries, velocity, period state)
  class3     — Asynchronous schema induction (versioned schema snapshots)
  proposals  — Proposal lifecycle store + auto-promotion check
  feedback   — Per-consumer feedback ledger + multi-loss aggregation

Spec ref: Part 10.5, 10.6.
"""

from .base import (
    MinerPass,
    PassContext,
    PassResult,
    ProposalLifecycle,
    ProposalRecord,
    ViewSnapshot,
)
from .class1 import Class1Pass
from .class2 import Class2Pass
from .class3 import Class3Pass, SchemaSnapshotEntry
from .feedback import (
    AggregatedLoss,
    FEEDBACK_CONSUMER,
    FEEDBACK_VALENCE,
    FeedbackEvent,
    FeedbackKind,
    FeedbackLedger,
    LOSS_WEIGHTS,
    aggregate_loss,
    get_feedback_ledger,
    set_feedback_ledger,
)
from .proposals import (
    DEFAULT_AUTO_PROMOTION_CONFIDENCE,
    DEFAULT_AUTO_PROMOTION_CORROBORATION,
    ProposalEntry,
    ProposalStore,
    auto_promotion_check,
    get_proposal_store,
    set_proposal_store,
)

__all__ = [
    "AggregatedLoss",
    "Class1Pass",
    "Class2Pass",
    "Class3Pass",
    "DEFAULT_AUTO_PROMOTION_CONFIDENCE",
    "DEFAULT_AUTO_PROMOTION_CORROBORATION",
    "FEEDBACK_CONSUMER",
    "FEEDBACK_VALENCE",
    "FeedbackEvent",
    "FeedbackKind",
    "FeedbackLedger",
    "LOSS_WEIGHTS",
    "MinerPass",
    "PassContext",
    "PassResult",
    "ProposalEntry",
    "ProposalLifecycle",
    "ProposalRecord",
    "ProposalStore",
    "SchemaSnapshotEntry",
    "ViewSnapshot",
    "aggregate_loss",
    "auto_promotion_check",
    "get_feedback_ledger",
    "get_proposal_store",
    "set_feedback_ledger",
    "set_proposal_store",
]
