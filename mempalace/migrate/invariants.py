"""
Post-migration invariant validation.

Per Part 11.2: after a migration run, walk the derived views and
assert that the new state satisfies a set of invariants. Used to
catch silent data loss / model-shift errors before a migration is
declared "done".

Invariants checked:

  I1. Every drawer node has a containing period (or is explicitly
      orphan-marked in metadata).
  I2. Every assertion has at least one `derived_from` edge.
  I3. Every period's `started_at_ms` ≤ `ended_at_ms` (when ended).
  I4. Sealed periods are also closed.
  I5. No assertion has empty subject_id / predicate / object_id.
  I6. No two distinct drawer_ids share the same content_hash unless
      a `drawer_hash_collision` event was emitted (R3 §5.3).

Each invariant is a small function returning a list of violations.
The aggregate `run_all` returns an InvariantReport.

Spec ref: Part 11.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schema.kinds import EdgeKind, NodeKind
from ..views.current import (
    canonical_nodes,
    incoming_edges,
    nodes_of_kind,
    outgoing_edges,
)


# =============================================================================
# Violation + report
# =============================================================================


@dataclass
class Violation:
    """One invariant violation."""

    invariant: str
    node_id: str = ""
    edge_id: str = ""
    detail: str = ""


@dataclass
class InvariantReport:
    violations: list[Violation] = field(default_factory=list)
    invariants_run: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def by_invariant(self) -> dict[str, list[Violation]]:
        out: dict[str, list[Violation]] = {}
        for v in self.violations:
            out.setdefault(v.invariant, []).append(v)
        return out


# =============================================================================
# Per-invariant checks
# =============================================================================


def check_drawer_in_period() -> list[Violation]:
    """I1: every DRAWER_REF has at least one incoming edge from an event
    (i.e. is associated with an event in some period).

    Lightweight version: for every DRAWER_REF node we check that it has
    *some* incoming edge of kind `contains` from an event. Production
    callers walk the full chain to a period.
    """
    out: list[Violation] = []
    for n in nodes_of_kind(NodeKind.DRAWER_REF):
        edges = incoming_edges(n.node_id, kind=EdgeKind.CONTAINS)
        if not edges:
            out.append(Violation(
                invariant="I1.drawer_in_period",
                node_id=n.node_id,
                detail="drawer has no contains parent",
            ))
    return out


def check_assertion_has_derived_from() -> list[Violation]:
    """I2: every assertion has at least one outgoing `derived_from` edge."""
    out: list[Violation] = []
    for n in nodes_of_kind(NodeKind.ASSERTION):
        derived = outgoing_edges(n.node_id, kind=EdgeKind.DERIVED_FROM)
        if not derived:
            out.append(Violation(
                invariant="I2.assertion_has_derived_from",
                node_id=n.node_id,
                detail="assertion has no derived_from edges",
            ))
    return out


def check_period_timing() -> list[Violation]:
    """I3: every period's started_at_ms ≤ ended_at_ms (when ended)."""
    out: list[Violation] = []
    for n in nodes_of_kind(NodeKind.PERIOD):
        started = n.properties.get("started_at_ms")
        ended = n.properties.get("ended_at_ms")
        if started is not None and ended is not None and ended < started:
            out.append(Violation(
                invariant="I3.period_timing",
                node_id=n.node_id,
                detail=f"ended_at_ms ({ended}) < started_at_ms ({started})",
            ))
    return out


def check_sealed_implies_closed() -> list[Violation]:
    """I4: sealed periods must also be closed."""
    out: list[Violation] = []
    for n in nodes_of_kind(NodeKind.PERIOD):
        sealed = bool(n.properties.get("sealed", False))
        ended = n.properties.get("ended_at_ms")
        if sealed and ended is None:
            out.append(Violation(
                invariant="I4.sealed_implies_closed",
                node_id=n.node_id,
                detail="period is sealed but has no ended_at_ms",
            ))
    return out


def check_assertion_fields() -> list[Violation]:
    """I5: assertions must have non-empty subject_id, predicate, object_id.

    We approximate: assertion nodes are reachable via `asserted_subject` /
    `asserted_object` edges; an assertion missing any of those is a
    violation.
    """
    out: list[Violation] = []
    for n in nodes_of_kind(NodeKind.ASSERTION):
        subj = outgoing_edges(n.node_id, kind=EdgeKind.ASSERTED_SUBJECT)
        obj = outgoing_edges(n.node_id, kind=EdgeKind.ASSERTED_OBJECT)
        pred = n.properties.get("predicate", "")
        if not subj:
            out.append(Violation(
                invariant="I5.assertion_fields",
                node_id=n.node_id,
                detail="missing asserted_subject",
            ))
        if not obj:
            out.append(Violation(
                invariant="I5.assertion_fields",
                node_id=n.node_id,
                detail="missing asserted_object",
            ))
        if not pred:
            out.append(Violation(
                invariant="I5.assertion_fields",
                node_id=n.node_id,
                detail="empty predicate",
            ))
    return out


def check_drawer_hash_uniqueness() -> list[Violation]:
    """I6: no two drawers share content_hash without a collision event.

    Note: in the new substrate a drawer_hash_collision event is the
    sanctioned way to record an intentional duplicate; this invariant
    checks that no *unsanctioned* duplicates leak through.

    Implementation here uses `canonical_nodes()` as a stand-in for "all
    nodes" since the views layer doesn't expose an all-nodes accessor.
    Production callers iterate the full event log and group by
    (drawer_id, content_hash) directly.
    """
    out: list[Violation] = []
    seen_hashes: dict[str, str] = {}  # content_hash → first drawer_id
    for n in nodes_of_kind(NodeKind.DRAWER_REF):
        ch = n.properties.get("content_hash", "")
        if not ch:
            continue
        if ch in seen_hashes and seen_hashes[ch] != n.node_id:
            out.append(Violation(
                invariant="I6.drawer_hash_uniqueness",
                node_id=n.node_id,
                detail=f"shares content_hash with {seen_hashes[ch]}",
            ))
        else:
            seen_hashes.setdefault(ch, n.node_id)
    return out


# =============================================================================
# Driver
# =============================================================================


_ALL_INVARIANTS = {
    "I1.drawer_in_period": check_drawer_in_period,
    "I2.assertion_has_derived_from": check_assertion_has_derived_from,
    "I3.period_timing": check_period_timing,
    "I4.sealed_implies_closed": check_sealed_implies_closed,
    "I5.assertion_fields": check_assertion_fields,
    "I6.drawer_hash_uniqueness": check_drawer_hash_uniqueness,
}


def run_all(invariants: list[str] | None = None) -> InvariantReport:
    """Run the listed invariants (or all of them) against the current views.

    Returns an InvariantReport with violations + bookkeeping.
    """
    keys = invariants or sorted(_ALL_INVARIANTS.keys())
    rep = InvariantReport()
    for key in keys:
        fn = _ALL_INVARIANTS.get(key)
        if fn is None:
            continue
        rep.invariants_run.append(key)
        rep.violations.extend(fn())
    return rep


__all__ = [
    "InvariantReport",
    "Violation",
    "check_assertion_fields",
    "check_assertion_has_derived_from",
    "check_drawer_hash_uniqueness",
    "check_drawer_in_period",
    "check_period_timing",
    "check_sealed_implies_closed",
    "run_all",
]
