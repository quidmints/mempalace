"""
Class 1 — drawer-level enrichment (streaming, near write-time).

Per Part 10.5: refines memory_type, computes affect derivatives,
resolves entities to KG entity IDs, classifies interactional and
self-other-world, marks goal-state markers. Per-drawer; fast.

This is the "episodic-buffer-flavored" pass (R3 §9.1): cheap, quick,
runs on every new drawer.

Cold-start uses heuristics. Once the local fine-tune has accumulated
enough feedback, it takes over (Part 10.5).

Spec ref: Part 10.5, R3 §9.1.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..schema.identifiers import make_assertion_id
from .base import (
    MinerPass,
    PassContext,
    PassResult,
    ProposalLifecycle,
    ProposalRecord,
)


# =============================================================================
# Heuristic helpers (cold start)
# =============================================================================


# memory_type buckets per Conway §SMS — kept lightweight; fine-tune
# replaces these heuristics later.
_MEMORY_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "episodic": ("today", "yesterday", "this morning", "i just", "i was"),
    "semantic": ("the fact is", "i know that", "in general", "always"),
    "autobiographical": ("when i was", "back in", "growing up", "my whole life"),
    "procedural": ("the way to", "how i", "i usually", "i always"),
}

_INTERACTIONAL_KEYWORDS = (
    "i told", "they said", "we agreed", "we discussed",
    "i asked", "they asked", "we talked",
)

_SELF_OTHER_WORLD_KEYWORDS = {
    "self": ("i feel", "i think", "i am", "i'm"),
    "other": ("they feel", "they think", "she", "he", "they"),
    "world": ("the world", "the country", "the system"),
}

_GOAL_KEYWORDS = (
    "i want", "i plan", "i will", "i should", "i need to",
    "i'm going to", "my goal", "i aim to",
)

_AFFECT_VOCAB = {
    "positive": (
        "happy", "good", "great", "love", "wonderful", "excited", "joy",
    ),
    "negative": (
        "sad", "bad", "awful", "hate", "frustrated", "angry", "anxious",
    ),
}


def _classify_memory_type(text: str) -> tuple[str, float]:
    text_lower = text.lower()
    best_kind = "episodic"
    best_count = 0
    for kind, words in _MEMORY_TYPE_KEYWORDS.items():
        count = sum(1 for w in words if w in text_lower)
        if count > best_count:
            best_kind, best_count = kind, count
    confidence = min(0.9, 0.4 + 0.1 * best_count) if best_count > 0 else 0.4
    return best_kind, confidence


def _classify_interactional(text: str) -> tuple[bool, float]:
    text_lower = text.lower()
    matches = sum(1 for w in _INTERACTIONAL_KEYWORDS if w in text_lower)
    return matches > 0, min(0.9, 0.5 + 0.1 * matches)


def _classify_self_other_world(text: str) -> tuple[str, float]:
    text_lower = text.lower()
    counts = {
        cat: sum(1 for w in words if w in text_lower)
        for cat, words in _SELF_OTHER_WORLD_KEYWORDS.items()
    }
    if all(v == 0 for v in counts.values()):
        return "unknown", 0.3
    best = max(counts, key=lambda k: counts[k])
    total = sum(counts.values())
    confidence = 0.4 + 0.5 * (counts[best] / total) if total else 0.4
    return best, min(0.9, confidence)


def _detect_goal_marker(text: str) -> tuple[bool, float]:
    text_lower = text.lower()
    matches = sum(1 for w in _GOAL_KEYWORDS if w in text_lower)
    return matches > 0, min(0.9, 0.5 + 0.1 * matches)


def _affect_derivatives(text: str) -> dict[str, float]:
    text_lower = text.lower()
    pos = sum(1 for w in _AFFECT_VOCAB["positive"] if w in text_lower)
    neg = sum(1 for w in _AFFECT_VOCAB["negative"] if w in text_lower)
    total = pos + neg
    if total == 0:
        return {"valence": 0.0, "arousal": 0.0, "intensity": 0.0}
    valence = (pos - neg) / total              # in [-1, 1]
    intensity = min(1.0, total / 5.0)
    return {
        "valence": valence,
        "arousal": intensity,
        "intensity": intensity,
    }


def _derivation_seed(drawer_id: str, version: str) -> str:
    return hashlib.blake2b(
        f"class1|{drawer_id}|{version}".encode("utf-8"), digest_size=8,
    ).hexdigest()


# =============================================================================
# Class 1 pass
# =============================================================================


class Class1Pass(MinerPass):
    """Streaming, per-drawer enrichment pass."""

    name = "miner.class1"
    pass_class = 1
    miner_version = "0.1.0"

    def __init__(self, *, max_drawers_per_run: int = 256) -> None:
        self._max = max_drawers_per_run

    def declares_inputs(self) -> tuple[str, ...]:
        return ("drawers",)

    def run(self, ctx: PassContext) -> PassResult:
        drawers: list[dict[str, Any]] = ctx.parameters.get("drawers", [])
        if not drawers and ctx.view is not None:
            drawers = list(ctx.view.current_drawers())

        proposals: list[ProposalRecord] = []
        consumed = 0
        for d in drawers[: self._max]:
            consumed += 1
            drawer_id = d.get("drawer_id", "")
            verbatim = str(d.get("verbatim", "") or "")
            if not drawer_id:
                continue

            # 1. memory_type
            mtype, mconf = _classify_memory_type(verbatim)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_{drawer_id}_memtype",
                    proposal_kind="memory_type",
                    target_node_id=drawer_id,
                    proposed_value={"memory_type": mtype},
                    confidence=mconf,
                    miner_class=1,
                    miner_version=self.miner_version,
                    derivation_seed=_derivation_seed(drawer_id, self.miner_version),
                )
            )

            # 2. interactional
            is_inter, iconf = _classify_interactional(verbatim)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_{drawer_id}_inter",
                    proposal_kind="interactional",
                    target_node_id=drawer_id,
                    proposed_value={"interactional": is_inter},
                    confidence=iconf,
                    miner_class=1,
                    miner_version=self.miner_version,
                    derivation_seed=_derivation_seed(drawer_id, self.miner_version),
                )
            )

            # 3. self/other/world
            sow, sconf = _classify_self_other_world(verbatim)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_{drawer_id}_sow",
                    proposal_kind="self_other_world",
                    target_node_id=drawer_id,
                    proposed_value={"sow_category": sow},
                    confidence=sconf,
                    miner_class=1,
                    miner_version=self.miner_version,
                    derivation_seed=_derivation_seed(drawer_id, self.miner_version),
                )
            )

            # 4. goal marker
            has_goal, gconf = _detect_goal_marker(verbatim)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_{drawer_id}_goal",
                    proposal_kind="goal_marker",
                    target_node_id=drawer_id,
                    proposed_value={"has_goal_marker": has_goal},
                    confidence=gconf,
                    miner_class=1,
                    miner_version=self.miner_version,
                    derivation_seed=_derivation_seed(drawer_id, self.miner_version),
                )
            )

            # 5. affect derivatives
            affect = _affect_derivatives(verbatim)
            proposals.append(
                ProposalRecord(
                    proposal_id=f"prop_{drawer_id}_affect",
                    proposal_kind="affect_derivatives",
                    target_node_id=drawer_id,
                    proposed_value=affect,
                    confidence=0.6,
                    miner_class=1,
                    miner_version=self.miner_version,
                    derivation_seed=_derivation_seed(drawer_id, self.miner_version),
                )
            )

        return PassResult(
            pass_name=self.name,
            pass_class=1,
            success=True,
            proposals=proposals,
            inputs_consumed=consumed,
            outputs_emitted=len(proposals),
        )


__all__ = ["Class1Pass"]
