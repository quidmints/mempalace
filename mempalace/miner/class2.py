"""
Class 2 — cross-drawer aggregation (periodic).

Per Part 10.5: detects event boundaries, asserts triples across
drawers, surfaces contradictions, updates velocity, proposes period
state transitions. Cross-drawer; expensive.

Runs hourly, end-of-day, or end-of-period. Pulls a window of recent
drawers + Class 1 proposals + existing assertions and emits:

  - assertion proposals (subject, predicate, object) over the window
  - event_boundary proposals (where the activity context shifted)
  - contradiction proposals (assertions that conflict with prior ones)
  - period_state proposals (period-level summary updates)

Spec ref: Part 10.5.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .base import (
    MinerPass,
    PassContext,
    PassResult,
    ProposalRecord,
)


# =============================================================================
# Heuristics
# =============================================================================


def _activity_signature(verbatim: str) -> set[str]:
    """Bag-of-meaningful-words for boundary detection."""
    words = [w.lower() for w in verbatim.split() if len(w) > 4]
    return set(words)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _detect_event_boundaries(
    drawers: list[dict[str, Any]],
    boundary_jaccard: float = 0.15,
) -> list[tuple[str, str]]:
    """Return list of (prev_drawer_id, next_drawer_id) where the
    activity signature crosses a similarity gap."""
    boundaries: list[tuple[str, str]] = []
    for i in range(1, len(drawers)):
        prev = drawers[i - 1]
        cur = drawers[i]
        sig_prev = _activity_signature(str(prev.get("verbatim", "") or ""))
        sig_cur = _activity_signature(str(cur.get("verbatim", "") or ""))
        if _jaccard(sig_prev, sig_cur) < boundary_jaccard:
            boundaries.append((prev.get("drawer_id", ""), cur.get("drawer_id", "")))
    return boundaries


def _extract_assertion_candidates(
    drawer: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """Very simple verb-extraction stub: looks for 'X <verb> Y'.

    Production replaces this with a proper relation extractor; this is
    just enough to exercise the proposal pipeline.
    """
    text = str(drawer.get("verbatim", "") or "")
    # Look for common predicates in a window of 3-token spans
    out: list[tuple[str, str, str]] = []
    tokens = [t.strip(".,;:!?") for t in text.split()]
    predicates = {"is", "was", "loves", "knows", "has", "owns", "made", "wants"}
    for i in range(1, len(tokens) - 1):
        if tokens[i].lower() in predicates:
            subj = tokens[i - 1]
            obj = tokens[i + 1]
            if subj and obj:
                out.append((subj, tokens[i].lower(), obj))
    return out


def _derivation_seed(drawer_ids: list[str], pass_version: str) -> str:
    """For re-derivability per R3 §9.1."""
    return hashlib.blake2b(
        ("class2|" + ",".join(sorted(drawer_ids)) + f"|{pass_version}").encode("utf-8"),
        digest_size=8,
    ).hexdigest()


# =============================================================================
# Class 2 pass
# =============================================================================


class Class2Pass(MinerPass):
    """Cross-drawer aggregation pass."""

    name = "miner.class2"
    pass_class = 2
    miner_version = "0.1.0"

    def __init__(
        self,
        *,
        max_drawers_per_run: int = 1024,
        boundary_jaccard: float = 0.15,
    ) -> None:
        self._max = max_drawers_per_run
        self._boundary_jaccard = boundary_jaccard

    def declares_inputs(self) -> tuple[str, ...]:
        return ("drawers", "existing_assertions")

    def run(self, ctx: PassContext) -> PassResult:
        drawers: list[dict[str, Any]] = ctx.parameters.get("drawers", [])
        if not drawers and ctx.view is not None:
            drawers = list(ctx.view.current_drawers())
        existing_assertions: list[dict[str, Any]] = ctx.parameters.get(
            "existing_assertions", []
        )
        if not existing_assertions and ctx.view is not None:
            existing_assertions = list(ctx.view.current_assertions())
        existing_keys = {
            (
                a.get("subject_id", ""),
                a.get("predicate", ""),
                a.get("object_id", ""),
            )
            for a in existing_assertions
        }

        drawers = drawers[: self._max]
        proposals: list[ProposalRecord] = []

        # 1. event boundaries
        boundaries = _detect_event_boundaries(drawers, self._boundary_jaccard)
        for prev_id, next_id in boundaries:
            seed = _derivation_seed([prev_id, next_id], self.miner_version)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_evt_{prev_id}_{next_id}",
                    proposal_kind="event_boundary",
                    target_node_id="",
                    proposed_value={
                        "before_drawer_id": prev_id,
                        "after_drawer_id": next_id,
                    },
                    confidence=0.6,
                    miner_class=2,
                    miner_version=self.miner_version,
                    derivation_seed=seed,
                )
            )

        # 2. assertion candidates
        for d in drawers:
            drawer_id = d.get("drawer_id", "")
            for s, p, o in _extract_assertion_candidates(d):
                key = (s, p, o)
                seed = _derivation_seed([drawer_id], self.miner_version)
                pr = ProposalRecord(
                    proposal_id=f"prop_ast_{drawer_id}_{seed}",
                    proposal_kind="assertion",
                    target_node_id=drawer_id,
                    proposed_value={
                        "subject": s,
                        "predicate": p,
                        "object": o,
                    },
                    confidence=0.5,
                    miner_class=2,
                    miner_version=self.miner_version,
                    derivation_seed=seed,
                )
                # Contradiction check: same subject + predicate but different
                # object as an existing assertion → flag contradiction
                contradicts = [
                    (es, ep, eo) for (es, ep, eo) in existing_keys
                    if es == s and ep == p and eo != o
                ]
                if contradicts:
                    proposals.append(
                        ProposalRecord(
                            proposal_id=f"prop_contra_{drawer_id}_{seed}",
                            proposal_kind="contradiction",
                            target_node_id=drawer_id,
                            proposed_value={
                                "candidate": (s, p, o),
                                "existing": contradicts,
                            },
                            confidence=0.7,
                            miner_class=2,
                            miner_version=self.miner_version,
                            derivation_seed=seed,
                        )
                    )
                # Don't re-assert what's already in the KG
                if key not in existing_keys:
                    proposals.append(pr)

        # 3. velocity update (one proposal per theme observed)
        theme_drawer_counts: dict[str, int] = {}
        for d in drawers:
            for t in d.get("themes", []):
                if isinstance(t, str):
                    theme_drawer_counts[t] = theme_drawer_counts.get(t, 0) + 1
        for theme_id, count in theme_drawer_counts.items():
            seed = _derivation_seed([f"theme_{theme_id}"], self.miner_version)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_vel_{theme_id}_{seed}",
                    proposal_kind="velocity_update",
                    target_node_id=theme_id,
                    proposed_value={"recent_drawer_count": count},
                    confidence=0.8,
                    miner_class=2,
                    miner_version=self.miner_version,
                    derivation_seed=seed,
                )
            )

        # 4. period-state proposal: simple summary count
        if drawers:
            seed = _derivation_seed(
                [str(d.get("drawer_id", "")) for d in drawers[:8]],
                self.miner_version,
            )
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_period_{seed}",
                    proposal_kind="period_state",
                    proposed_value={
                        "drawer_count_in_window": len(drawers),
                        "boundaries_detected": len(boundaries),
                    },
                    confidence=0.6,
                    miner_class=2,
                    miner_version=self.miner_version,
                    derivation_seed=seed,
                )
            )

        return PassResult(
            pass_name=self.name,
            pass_class=2,
            success=True,
            proposals=proposals,
            inputs_consumed=len(drawers),
            outputs_emitted=len(proposals),
        )


__all__ = ["Class2Pass"]
