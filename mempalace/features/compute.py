"""
Feature compute functions.

Each builtin feature in `features.registry.BUILTINS` gets a compute function
here. The functions read from views (current_nodes, heat_field, etc.) and
return values matching the declared dtype.

Compute is *pure* — given the same view state and same subject_id, it returns
the same value. This matters because feature values are persisted (see
`features.persist`) and expected to be replayable.

Stance-aware features take the active stance from context['stance'] and
adjust accordingly. Stance-unaware features ignore it.

Spec ref: Part 7.1, R3 §1.4 (attestation hooks for inference-driven features
will land in batch 9 / miner.py).
"""

from __future__ import annotations

import math
import time
from typing import Any

from ..schema.stance import Stance
from ..views.current import _get_store, current_node
from .registry import ComputeFn, FeatureDef, get_registry


# =============================================================================
# Helper: time decay
# =============================================================================


def _exp_decay(now_ms: int, then_ms: int, half_life_days: float) -> float:
    """Exponential decay from then to now with given half-life."""
    if then_ms <= 0:
        return 0.0
    delta_ms = max(0, now_ms - then_ms)
    half_life_ms = half_life_days * 24 * 3600 * 1000
    if half_life_ms <= 0:
        return 1.0
    return math.pow(0.5, delta_ms / half_life_ms)


# =============================================================================
# drawer_recency_score
# =============================================================================


def compute_drawer_recency_score(subject_id: str, ctx: dict[str, Any]) -> float:
    """Recency in [0,1] using exponential decay with 30-day half-life."""
    now_ms = ctx.get("now_ms") or int(time.time() * 1000)
    node = current_node(subject_id)
    if node is None:
        return 0.0
    # Use the node's last_modified offset projected to time. In the
    # in-memory view we don't have offset → ts mapping; fall back to the
    # node's properties carrying capture_recorded_at if available, else
    # use the index time.
    captured_ms = node.properties.get("capture_recorded_at") or now_ms
    return _exp_decay(now_ms, captured_ms, half_life_days=30.0)


# =============================================================================
# drawer_heat
# =============================================================================


def compute_drawer_heat(subject_id: str, ctx: dict[str, Any]) -> float:
    """Live heat from the heat field. The view store carries `heat` per node.

    Returns 0.5 (the unfortunate-no-info prior used elsewhere) for nodes
    not yet bumped, matching the defaultdict factory in the view store.
    """
    store = _get_store()
    return store.heat.get(subject_id, 0.5)


# =============================================================================
# drawer_velocity_30d
# =============================================================================


def compute_drawer_velocity_30d(subject_id: str, ctx: dict[str, Any]) -> float:
    """Access velocity over the trailing 30 days.

    Reads from the velocity_field view if available; falls back to a
    last-bumped-time decay when not. The Rust velocity_field carries
    per-node access logs and recomputes velocities on bump events; the
    Python fallback approximates with a simpler heuristic.
    """
    store = _get_store()
    last = store.heat_last_bumped.get(subject_id)
    if last is None:
        return 0.0
    # Heuristic: heat × decay-on-recency gives a velocity-like signal.
    now_ms = ctx.get("now_ms") or int(time.time() * 1000)
    heat = store.heat.get(subject_id, 0.0)
    recency = _exp_decay(now_ms, last, half_life_days=15.0)
    return heat * recency * 30.0  # scale to per-30d access count


# =============================================================================
# theme_canonicality
# =============================================================================


def compute_theme_canonicality(subject_id: str, ctx: dict[str, Any]) -> float:
    """1.0 if canonical; otherwise scaled by canonical-promotion progress."""
    node = current_node(subject_id)
    if node is None:
        return 0.0
    if node.canonical:
        return 1.0
    # Approximate progress toward canonicality from importance + heat.
    store = _get_store()
    heat = store.heat.get(subject_id, 0.0)
    importance = float(node.properties.get("importance", 0.0) or 0.0)
    # Combine: weighted average, clamped to [0, 1)
    return max(0.0, min(0.99, 0.6 * heat + 0.4 * importance))


# =============================================================================
# event_fork_significance
# =============================================================================


def compute_event_fork_significance(subject_id: str, ctx: dict[str, Any]) -> float:
    """KisMATH-inspired fork significance.

    A fork is an event where multiple coherent continuations are possible
    and one is chosen. Approximated here by:
      - number of distinct outgoing edges relative to the median for events
      - presence of contradiction-resolution edges incident to the event
      - schema-instantiation transitions across the event boundary

    Returns a value in [0, 1]; higher means more decision-significant.
    """
    node = current_node(subject_id)
    if node is None:
        return 0.0

    store = _get_store()
    out_edge_ids = store.outgoing.get(subject_id, [])
    in_edge_ids = store.incoming.get(subject_id, [])
    out_edges = [store.edges[eid] for eid in out_edge_ids if eid in store.edges]
    in_edges = [store.edges[eid] for eid in in_edge_ids if eid in store.edges]

    # Heuristic 1: edge fan-out. More distinct edge kinds → more options.
    distinct_kinds_out = len({e.edge_kind for e in out_edges})
    fan_out_signal = min(1.0, distinct_kinds_out / 5.0)

    # Heuristic 2: explicit fork markers in properties (set by miner pass).
    explicit = float(node.properties.get("fork_significance_explicit", 0.0) or 0.0)

    # Heuristic 3: contradicts/supports edges incident.
    contradiction_count = sum(
        1 for e in (out_edges + in_edges)
        if e.edge_kind in ("contradicts", "supports")
    )
    contradiction_signal = min(1.0, contradiction_count / 3.0)

    # Combine: explicit markers dominate when present; otherwise blend.
    if explicit > 0:
        return min(1.0, max(explicit, 0.5 * fan_out_signal + 0.5 * contradiction_signal))
    return min(1.0, 0.5 * fan_out_signal + 0.5 * contradiction_signal)


# =============================================================================
# assertion_substrate_faithfulness
# =============================================================================


def compute_assertion_substrate_faithfulness(subject_id: str, ctx: dict[str, Any]) -> float:
    """How well an assertion's text matches its derived_from substrate.

    The full implementation requires reading drawer text and comparing token
    overlap. For this batch we ship a structural proxy: an assertion with
    multiple derived_from edges is more faithful (multi-source); an
    assertion with no derived_from is faithful only if confidence is high.
    """
    store = _get_store()
    out_edge_ids = store.outgoing.get(subject_id, [])
    out_edges = [store.edges[eid] for eid in out_edge_ids if eid in store.edges]
    derived_count = sum(1 for e in out_edges if e.edge_kind == "derived_from")
    if derived_count == 0:
        node = current_node(subject_id)
        confidence = float(node.properties.get("confidence", 0.5)) if node else 0.5
        return 0.3 * confidence  # heavily-discounted floor
    # Each additional source raises faithfulness with diminishing returns.
    return min(1.0, 1.0 - math.pow(0.6, derived_count))


# =============================================================================
# period_velocity_coupling
# =============================================================================


def compute_period_velocity_coupling(subject_id: str, ctx: dict[str, Any]) -> float:
    """Cross-theme velocity correlation during this period.

    Implementation deferred — requires the velocity_field view's per-theme
    decomposition which is in the Rust view layer (Batch 2). Returns a
    placeholder zero until that's wired through PyO3 in a later batch.
    """
    return 0.0


# =============================================================================
# stance_alignment_score
# =============================================================================


def compute_stance_alignment_score(subject_id: str, ctx: dict[str, Any]) -> float:
    """How well a candidate aligns with the stance dimensions.

    Subject_id may be any kind (this is the only "global" feature that can
    apply across kinds). Reads stance from ctx['stance'] (a Stance dataclass)
    and computes a weighted score using:
      - heat × correspondence_vs_coherence
      - canonicality × canonicality_floor
      - recency × recency_bias
    """
    stance: Stance | None = ctx.get("stance")
    if stance is None:
        return 0.5  # neutral default

    node = current_node(subject_id)
    if node is None:
        return 0.0

    store = _get_store()
    heat = store.heat.get(subject_id, 0.0)

    # Recency component
    now_ms = ctx.get("now_ms") or int(time.time() * 1000)
    captured_ms = node.properties.get("capture_recorded_at") or now_ms
    recency = _exp_decay(now_ms, captured_ms, half_life_days=30.0)

    # Canonicality component
    canon = 1.0 if node.canonical else 0.0

    # Correspondence-vs-coherence: heat gives coherence pull; substrate
    # presence gives correspondence pull. Stance dim in [0, 1] sets weight
    # directly.
    corr_pull = canon  # canonical = stable evidence anchor
    coh_pull = heat   # heat = currently activated, coherence-driven
    cvc_weight = stance.correspondence_vs_coherence
    cvc_score = cvc_weight * corr_pull + (1.0 - cvc_weight) * coh_pull

    # Combine
    score = (
        0.5 * cvc_score
        + 0.3 * (recency * stance.recency_bias)
        + 0.2 * max(canon, stance.canonicality_floor)
    )
    return max(0.0, min(1.0, score))


# =============================================================================
# Wire compute functions into registry
# =============================================================================

_COMPUTES: dict[str, ComputeFn] = {
    "drawer_recency_score": compute_drawer_recency_score,
    "drawer_heat": compute_drawer_heat,
    "drawer_velocity_30d": compute_drawer_velocity_30d,
    "theme_canonicality": compute_theme_canonicality,
    "event_fork_significance": compute_event_fork_significance,
    "assertion_substrate_faithfulness": compute_assertion_substrate_faithfulness,
    "period_velocity_coupling": compute_period_velocity_coupling,
    "stance_alignment_score": compute_stance_alignment_score,
}


def _wire_computes() -> None:
    reg = get_registry()
    for name, fn in _COMPUTES.items():
        feature_def = reg.get(name)
        if feature_def is not None:
            reg.register(feature_def, fn)


_wire_computes()


# =============================================================================
# compute() — single-feature entry point
# =============================================================================


def compute(
    feature_name: str,
    subject_id: str,
    *,
    stance: Stance | None = None,
    now_ms: int | None = None,
    extra_context: dict[str, Any] | None = None,
) -> Any:
    """Compute a feature's value for a subject.

    Validates the result against the feature's dtype before returning,
    so misconfigured compute functions surface immediately.
    """
    reg = get_registry()
    fdef = reg.get(feature_name)
    if fdef is None:
        raise KeyError(f"unknown feature: {feature_name}")
    fn = reg.get_compute(feature_name)
    if fn is None:
        raise RuntimeError(f"feature {feature_name} has no compute function registered")
    ctx: dict[str, Any] = {
        "stance": stance,
        "now_ms": now_ms,
    }
    if extra_context:
        ctx.update(extra_context)
    value = fn(subject_id, ctx)
    fdef.validate_value(value)
    return value


def compute_many(
    feature_names: list[str],
    subject_id: str,
    *,
    stance: Stance | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Compute several features for the same subject, sharing context."""
    return {
        name: compute(name, subject_id, stance=stance, now_ms=now_ms)
        for name in feature_names
    }


__all__ = ["compute", "compute_many"]
