"""
Switchboard SDK glue (item 2 deliverable).

This package implements the resolver-side runtime per
ORACLE_THREAT_MODEL.md §4. It's the Python wire for switchboard
nodes — palaces volunteering to host resolution jobs for markets
classified as `PUBLIC_LLM_RESOLVABLE` or `PRIVACY_PRESERVING_REQUIRED`
(R3 §3.2).

# What's here

  - `node_listener.py` — polls assignment PDAs; emits internal
    `switchboard_assignment_received` events.
  - `job.py` — `ResolutionJob` runtime; ingests slice, runs
    formula in sandbox, emits findings.
  - `decloak.py` — Case C subject-blind decloak watcher and
    challenge-issuer.

# Status

Skeleton — entry points defined; full implementation is follow-on
work blocking on:

  - The on-chain instructions (`assign_resolver.rs`,
    `submit_finding.rs`, etc.) being written.
  - The plan-commitment registry (mapping privacy mode + market
    shape → required plan hash) being written.
  - The resolver-randomness source being chosen (Solana VRF or
    block-hash mix).

The skeleton compiles, imports cleanly, and is exercised by
`tests/test_switchboard_skeleton.py`. Live integration tests
land with the on-chain instructions.

# Why a separate package and not under federate/

`federate/` is per-palace federation: this palace as one peer,
running matches against another palace. `switchboard/` is the
oracle role: this palace running resolution jobs that other
markets dispatched. Distinct concerns; same primitives (sandbox,
findings, attestation chain) used in different orchestration
flows.
"""

from mempalace.switchboard.chain_observer import (
    observe_finding_submitted,
    observe_subject_blind_challenged,
    observe_subject_blind_submitted,
)
from mempalace.switchboard.job import (
    JobStatus,
    ResolutionFinding,
    ResolutionJob,
    SliceRequestSpec,
)
from mempalace.switchboard.node_listener import (
    SwitchboardNodeListener,
    is_switchboard_node_enabled,
)

__all__ = [
    "JobStatus",
    "ResolutionFinding",
    "ResolutionJob",
    "SliceRequestSpec",
    "SwitchboardNodeListener",
    "is_switchboard_node_enabled",
    "observe_finding_submitted",
    "observe_subject_blind_challenged",
    "observe_subject_blind_submitted",
]
