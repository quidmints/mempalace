"""
Class 3 — schema induction (rare, asynchronous).

Per Part 10.5: induces traits, relational schemas, self-with-other
units, possible selves. Cross-event abstraction; most expensive;
runs daily during downtime.

Schemas are *versioned*, not rewritten. Each Class 3 pass produces a
snapshot. Comparing snapshots gives stable / drifting / broken
classifications. Stable schemas don't need attention; drifting schemas
surface as "your understanding refined"; broken schemas trigger
split / replace / retire flow with user adjudication.

Spec ref: Part 10.5.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .base import (
    MinerPass,
    PassContext,
    PassResult,
    ProposalRecord,
)


# =============================================================================
# Schema-shape extraction
# =============================================================================


def _schema_shape(predicate: str, subject_kind: str, object_kind: str) -> tuple[str, ...]:
    return (predicate, subject_kind, object_kind)


def _shape_fingerprint(shape: tuple[str, ...]) -> str:
    return hashlib.blake2b(
        "|".join(shape).encode("utf-8"), digest_size=8,
    ).hexdigest()


def _classify_schema_status(
    current_count: int,
    previous_count: int,
    *,
    drift_ratio: float = 0.5,
) -> str:
    """Classify a schema as stable / drifting / broken given count change."""
    if previous_count == 0 and current_count > 0:
        return "new"
    if previous_count > 0 and current_count == 0:
        return "broken"
    if previous_count == 0:
        return "new"
    ratio = abs(current_count - previous_count) / max(previous_count, 1)
    if ratio < drift_ratio:
        return "stable"
    return "drifting"


# =============================================================================
# Class 3 pass
# =============================================================================


@dataclass
class SchemaSnapshotEntry:
    """One entry in a schema-snapshot proposal."""

    fingerprint: str
    shape: tuple[str, ...]
    instance_count: int
    status: str = "new"


class Class3Pass(MinerPass):
    """Schema-induction pass."""

    name = "miner.class3"
    pass_class = 3
    miner_version = "0.1.0"

    def __init__(self, *, min_instances: int = 3) -> None:
        self._min_instances = min_instances

    def declares_inputs(self) -> tuple[str, ...]:
        return ("assertions", "previous_schema_snapshot")

    def run(self, ctx: PassContext) -> PassResult:
        assertions: list[dict[str, Any]] = ctx.parameters.get("assertions", [])
        if not assertions and ctx.view is not None:
            assertions = list(ctx.view.current_assertions())
        previous_snapshot: dict[str, int] = ctx.parameters.get(
            "previous_schema_snapshot", {}
        )

        # Count assertion shapes
        shape_counts: Counter[tuple[str, ...]] = Counter()
        for a in assertions:
            pred = a.get("predicate", "")
            subj_kind = a.get("subject_kind", "entity")
            obj_kind = a.get("object_kind", "entity")
            if not pred:
                continue
            shape_counts[_schema_shape(pred, subj_kind, obj_kind)] += 1

        # Build snapshot entries (only over min_instances)
        entries: list[SchemaSnapshotEntry] = []
        for shape, count in shape_counts.items():
            if count < self._min_instances:
                continue
            fp = _shape_fingerprint(shape)
            previous_count = previous_snapshot.get(fp, 0)
            status = _classify_schema_status(count, previous_count)
            entries.append(
                SchemaSnapshotEntry(
                    fingerprint=fp,
                    shape=shape,
                    instance_count=count,
                    status=status,
                )
            )

        # Detect schemas present in the previous snapshot but missing now
        for fp, prev_count in previous_snapshot.items():
            if not any(e.fingerprint == fp for e in entries):
                # If the schema exists in our predicate-counts at all,
                # it's already classified above; this branch flags
                # outright-broken schemas.
                entries.append(
                    SchemaSnapshotEntry(
                        fingerprint=fp,
                        shape=("__retired__",),
                        instance_count=0,
                        status="broken",
                    )
                )

        # Emit one proposal per schema shape
        proposals: list[ProposalRecord] = []
        for entry in entries:
            seed = hashlib.blake2b(
                f"class3|{entry.fingerprint}|{self.miner_version}".encode("utf-8"),
                digest_size=8,
            ).hexdigest()
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_schema_{entry.fingerprint}",
                    proposal_kind="schema_snapshot",
                    target_node_id="",
                    proposed_value={
                        "fingerprint": entry.fingerprint,
                        "shape": list(entry.shape),
                        "instance_count": entry.instance_count,
                        "status": entry.status,
                        "miner_version": self.miner_version,
                    },
                    confidence=0.55 if entry.status in ("new", "drifting") else 0.85,
                    miner_class=3,
                    miner_version=self.miner_version,
                    derivation_seed=seed,
                )
            )

        # Bonus: trait/possible-selves proposals are deferred until the
        # local fine-tune is in place (Part 10.5 'cold start vs trained').
        # The cold-start version emits structural snapshots only; the
        # fine-tuned version will emit richer proposals here.

        return PassResult(
            pass_name=self.name,
            pass_class=3,
            success=True,
            proposals=proposals,
            inputs_consumed=len(assertions),
            outputs_emitted=len(proposals),
        )


__all__ = ["Class3Pass", "SchemaSnapshotEntry"]
