"""
Miner pass abstract base.

Per Part 10.5 / 10.6: the miner runs in three classes (1/2/3) at three
cadences (streaming / periodic / asynchronous). Each pass implements a
common interface so the multiplexer can schedule them uniformly.

This module owns:

  - MinerPass: abstract base for any miner pass
  - PassResult: standard return type
  - PassContext: read-only view + log-append handle
  - ProposalLifecycle enum (provisional / confirmed / rejected)

Spec ref: Part 10.5, 10.6.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ..log.client import LogClient


# =============================================================================
# Lifecycle
# =============================================================================


class ProposalLifecycle(str, Enum):
    """Per Part 10.6: every miner output is provisional until confirmed."""

    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


# =============================================================================
# Pass context
# =============================================================================


@runtime_checkable
class ViewSnapshot(Protocol):
    """Read-only view at a fixed log offset."""

    def offset(self) -> int: ...
    def current_drawers(self) -> list[dict[str, Any]]: ...
    def current_assertions(self) -> list[dict[str, Any]]: ...


@dataclass
class PassContext:
    """Per-pass state passed to MinerPass.run().

    Holds a snapshot view (for read-only consumption at the pass's
    pinned offset), a log client (for emitting proposals), and the
    pass-specific parameters dict.
    """

    view: ViewSnapshot | None = None
    log: LogClient | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    # Set if this run is a resume from a previous checkpoint
    resume_from_offset: int | None = None


# =============================================================================
# Pass result
# =============================================================================


@dataclass
class ProposalRecord:
    """One proposal emitted by a miner pass."""

    proposal_id: str
    proposal_kind: str                       # "memory_type" | "assertion" | "schema" | etc.
    target_node_id: str = ""                 # what this proposal touches
    proposed_value: Any = None
    confidence: float = 0.0
    miner_class: int = 0                     # 1 / 2 / 3
    miner_version: str = "0.1.0"

    lifecycle: ProposalLifecycle = ProposalLifecycle.PROVISIONAL

    # Link back to the inputs that produced this proposal — supports
    # downstream feedback credit assignment (R3 §11)
    input_offsets: list[int] = field(default_factory=list)
    derivation_seed: str = ""                # for re-derivability (§9.1)

    # Phase 2: content-version stamp. ProposalStore stamps this on
    # `add()` if not already set; miner passes can pre-stamp during
    # generation if they have the dependency info.
    version_stamp: "VersionStamp" = field(
        default_factory=lambda: __import__(
            "mempalace.schema.versioning", fromlist=["VersionStamp"]
        ).VersionStamp(),
    )


@dataclass
class PassResult:
    """Standard miner pass return type."""

    pass_name: str
    pass_class: int                          # 1 / 2 / 3
    success: bool
    proposals: list[ProposalRecord] = field(default_factory=list)
    inputs_consumed: int = 0
    outputs_emitted: int = 0
    error_kind: str = ""
    error_message: str = ""

    # Checkpoint info for resume on preemption
    final_offset: int = 0


# =============================================================================
# Abstract base
# =============================================================================


class MinerPass(ABC):
    """Abstract base for a miner pass."""

    name: str = "miner.base"
    pass_class: int = 0
    miner_version: str = "0.1.0"

    @abstractmethod
    def run(self, ctx: PassContext) -> PassResult:
        """Execute the pass against the given context. Return a PassResult."""
        ...

    # Subclasses can override to declare their input requirements
    # (the multiplexer can use this to gate scheduling)
    def declares_inputs(self) -> tuple[str, ...]:
        return ()


__all__ = [
    "MinerPass",
    "PassContext",
    "PassResult",
    "ProposalLifecycle",
    "ProposalRecord",
    "ViewSnapshot",
]
