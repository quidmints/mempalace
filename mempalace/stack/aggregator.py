"""
Trusted aggregator.

Per R3 §1.2 and §6.2: when stacks combine outputs from multiple steps
(especially rankers), combination is performed by a *trusted aggregator*
in the daemon's core, not by any individual step. This prevents a
malicious step from manipulating the combination — for example, a rogue
ranker returning all-zeros doesn't zero out the stack because the
aggregator gates the combination logic.

This module provides aggregation primitives that the Stack class and the
ranker dispatch use. The aggregator runs in the trusted core; rogue
steps cannot reach into it.

Aggregation strategies:
  - take_first        : keep first non-failure result, ignore later
  - last_writer_wins  : use last value (default for non-list outputs)
  - merge_dicts       : deep-merge dict outputs
  - sum_numeric       : sum numeric outputs
  - weighted_average  : numeric average with declared weights
  - multiply_clamped  : multiplicative combination, clamped to [floor, 1]
  - rank_compose      : compose ranker scores (the production case for
                        ranker stacking; bounded by floor to defeat
                        zero-out attacks)

Spec ref: R3 §1.2, §6.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AggregationKind(str, Enum):
    TAKE_FIRST = "take_first"
    LAST_WRITER_WINS = "last_writer_wins"
    MERGE_DICTS = "merge_dicts"
    SUM_NUMERIC = "sum_numeric"
    WEIGHTED_AVERAGE = "weighted_average"
    MULTIPLY_CLAMPED = "multiply_clamped"
    RANK_COMPOSE = "rank_compose"


@dataclass
class AggregationSpec:
    """How to combine values from multiple step outputs for a given key."""

    kind: AggregationKind
    weights: dict[str, float] | None = None  # for WEIGHTED_AVERAGE
    floor: float = 0.05                       # for MULTIPLY_CLAMPED, RANK_COMPOSE
    ceiling: float = 1.0


# =============================================================================
# Aggregation kernels
# =============================================================================


def aggregate(
    values: list[Any],
    spec: AggregationSpec,
    sources: list[str] | None = None,
) -> Any:
    """Combine values per spec.

    `sources` is the parallel list of source step names (used by
    WEIGHTED_AVERAGE to look up weights).
    """
    if not values:
        return None

    match spec.kind:
        case AggregationKind.TAKE_FIRST:
            return values[0]

        case AggregationKind.LAST_WRITER_WINS:
            return values[-1]

        case AggregationKind.MERGE_DICTS:
            out: dict = {}
            for v in values:
                if isinstance(v, dict):
                    out.update(v)
            return out

        case AggregationKind.SUM_NUMERIC:
            return sum(float(v) for v in values if isinstance(v, (int, float)))

        case AggregationKind.WEIGHTED_AVERAGE:
            if not sources or spec.weights is None:
                # Equal weights fallback
                nums = [float(v) for v in values if isinstance(v, (int, float))]
                return sum(nums) / len(nums) if nums else 0.0
            total_weight = 0.0
            weighted_sum = 0.0
            for v, src in zip(values, sources, strict=True):
                if not isinstance(v, (int, float)):
                    continue
                w = float(spec.weights.get(src, 0.0))
                weighted_sum += w * float(v)
                total_weight += w
            return weighted_sum / total_weight if total_weight > 0 else 0.0

        case AggregationKind.MULTIPLY_CLAMPED:
            product = 1.0
            for v in values:
                if not isinstance(v, (int, float)):
                    continue
                # Clamp each factor to [floor, ceiling] to defeat zero-out
                clamped = max(spec.floor, min(spec.ceiling, float(v)))
                product *= clamped
            return product

        case AggregationKind.RANK_COMPOSE:
            # For ranker stacking: combine multiple ranker outputs over a
            # shared candidate set. Each value is dict[node_id, score].
            # Output is dict[node_id, geomean of clamped scores].
            if not values:
                return {}
            # Collect all keys
            all_keys: set[str] = set()
            for v in values:
                if isinstance(v, dict):
                    all_keys.update(v.keys())
            out_scored: dict[str, float] = {}
            for key in all_keys:
                product = 1.0
                count = 0
                for v in values:
                    if isinstance(v, dict) and key in v:
                        score = v[key]
                        if isinstance(score, (int, float)):
                            clamped = max(spec.floor, min(spec.ceiling, float(score)))
                            product *= clamped
                            count += 1
                if count > 0:
                    # Geometric mean — robust to outlier rankers
                    out_scored[key] = product ** (1.0 / count)
            return out_scored

    raise ValueError(f"unknown aggregation kind: {spec.kind}")


# =============================================================================
# TrustedAggregator class
# =============================================================================


class TrustedAggregator:
    """Holds aggregation specs per output key.

    Used by Stack for outputs that need cross-step combination. Note:
    the default Stack.execute() uses last-writer-wins; consumers that
    want explicit aggregation construct a TrustedAggregator and apply
    it to step results.
    """

    def __init__(self, specs: dict[str, AggregationSpec] | None = None) -> None:
        self._specs = dict(specs or {})

    def set_spec(self, key: str, spec: AggregationSpec) -> None:
        self._specs[key] = spec

    def aggregate_step_results(
        self,
        outputs_per_step: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Given (step_name, outputs) pairs, produce the combined output dict.

        Keys without a registered spec default to LAST_WRITER_WINS.
        """
        # Group values by key
        keys_to_values: dict[str, list[Any]] = {}
        keys_to_sources: dict[str, list[str]] = {}
        for step_name, step_outputs in outputs_per_step:
            for k, v in step_outputs.items():
                keys_to_values.setdefault(k, []).append(v)
                keys_to_sources.setdefault(k, []).append(step_name)

        out: dict[str, Any] = {}
        for k, values in keys_to_values.items():
            spec = self._specs.get(k) or AggregationSpec(
                kind=AggregationKind.LAST_WRITER_WINS
            )
            out[k] = aggregate(values, spec, sources=keys_to_sources[k])
        return out


__all__ = [
    "AggregationKind",
    "AggregationSpec",
    "TrustedAggregator",
    "aggregate",
]
