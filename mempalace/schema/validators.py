"""
Runtime validation of event payloads.

Validators run synchronously on every log append. Failures emit
`append_rejected` events; rejected events do not advance the log offset
(the rejection itself is logged for audit, but the would-be event is
discarded).

Three categories of validation:

  1. Per-kind schema — payload fields are present and well-typed.
  2. Edge constraint — edge kinds connect compatible node kinds.
  3. Forbidden patterns — the patterns enumerated in R3 §3.4 are rejected.

FK existence is *not* validated here at the Python layer because the master
views may be at a different log offset than the appender; the Rust side
enforces FK at the view-update boundary using DDflow's incremental joins.

Spec ref: Part 1.5
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .events import (
    AnyEvent, AppendRejected, DrawerCaptured, EdgeCreated, EdgeInvalidated,
    EVENT_KIND_TO_CLASS, Event, NodeCreated, NodePropertySet,
)
from .identifiers import is_valid_or_reserved
from .kinds import (
    EDGE_CONSTRAINTS, EdgeKind, NodeKind,
)


# =============================================================================
# Validation result
# =============================================================================

@dataclass
class ValidationError:
    """A validation failure. Multiple errors may be reported per event."""
    code: str
    field: str | None = None
    message: str = ""

    def __str__(self) -> str:
        prefix = f"[{self.code}]"
        if self.field:
            prefix = f"{prefix}[{self.field}]"
        return f"{prefix} {self.message}"


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError]

    @classmethod
    def passed(cls) -> "ValidationResult":
        return cls(ok=True, errors=[])

    @classmethod
    def failed(cls, *errors: ValidationError) -> "ValidationResult":
        return cls(ok=False, errors=list(errors))

    def to_rejected_event(self, original: Event) -> AppendRejected:
        summary = ", ".join(str(e) for e in self.errors)
        return AppendRejected(
            rejected_kind=original.kind,
            rejected_payload_summary=str(original)[:500],
            error=summary,
        )


# =============================================================================
# Per-kind validators
# =============================================================================

def _required_str(value: Any, field: str) -> ValidationError | None:
    if not isinstance(value, str) or not value:
        return ValidationError(
            code="required_field_missing", field=field,
            message=f"{field} must be a non-empty string",
        )
    return None


def _required_id(value: Any, field: str) -> ValidationError | None:
    if not isinstance(value, str) or not is_valid_or_reserved(value):
        return ValidationError(
            code="invalid_id", field=field,
            message=f"{field} is not a valid id: {value!r}",
        )
    return None


def _validate_drawer_captured(ev: DrawerCaptured) -> list[ValidationError]:
    errs: list[ValidationError] = []
    if e := _required_id(ev.drawer_id, "drawer_id"):
        errs.append(e)
    if e := _required_str(ev.content_hash, "content_hash"):
        errs.append(e)
    if len(ev.content_hash) != 64:
        errs.append(ValidationError(
            code="bad_content_hash", field="content_hash",
            message=f"content_hash must be 64 hex chars, got {len(ev.content_hash)}",
        ))
    if ev.duration_ms < 0:
        errs.append(ValidationError(
            code="negative_duration", field="duration_ms",
            message="duration_ms must be >= 0",
        ))
    if ev.self_other_world not in ("self", "other", "world"):
        errs.append(ValidationError(
            code="bad_enum_value", field="self_other_world",
            message=f"self_other_world must be self/other/world",
        ))
    return errs


def _validate_node_created(ev: NodeCreated) -> list[ValidationError]:
    errs: list[ValidationError] = []
    if e := _required_id(ev.node_id, "node_id"):
        errs.append(e)
    if e := _required_str(ev.node_kind, "node_kind"):
        errs.append(e)
    # NodeKind value check
    valid_kinds = {k.value for k in NodeKind}
    if ev.node_kind and ev.node_kind not in valid_kinds:
        errs.append(ValidationError(
            code="unknown_node_kind", field="node_kind",
            message=f"unknown node_kind: {ev.node_kind!r}",
        ))
    # Forbidden pattern: canonical=true requires canon_path
    if ev.canonical and not ev.canon_path:
        errs.append(ValidationError(
            code="canonical_node_without_canon_path",
            message="nodes with canonical=True must have a canon_path",
        ))
    if not (0.0 <= ev.importance <= 1.0):
        errs.append(ValidationError(
            code="importance_out_of_range", field="importance",
            message="importance must be in [0, 1]",
        ))
    return errs


def _validate_node_property_set(ev: NodePropertySet) -> list[ValidationError]:
    errs: list[ValidationError] = []
    if e := _required_id(ev.node_id, "node_id"):
        errs.append(e)
    if e := _required_str(ev.field_name, "field_name"):
        errs.append(e)
    return errs


def _validate_edge_created(ev: EdgeCreated) -> list[ValidationError]:
    errs: list[ValidationError] = []
    if e := _required_id(ev.edge_id, "edge_id"):
        errs.append(e)
    if e := _required_str(ev.edge_kind, "edge_kind"):
        errs.append(e)
    if e := _required_id(ev.source_node_id, "source_node_id"):
        errs.append(e)
    if e := _required_id(ev.target_node_id, "target_node_id"):
        errs.append(e)

    # EdgeKind value check
    valid_kinds = {k.value for k in EdgeKind}
    if ev.edge_kind and ev.edge_kind not in valid_kinds:
        errs.append(ValidationError(
            code="unknown_edge_kind", field="edge_kind",
            message=f"unknown edge_kind: {ev.edge_kind!r}",
        ))

    # Bitemporal validity sanity
    if ev.valid_from is not None and ev.valid_to is not None:
        if ev.valid_to < ev.valid_from:
            errs.append(ValidationError(
                code="invalid_validity_range", field="valid_to",
                message="valid_to must be >= valid_from",
            ))

    if not (0.0 <= ev.weight):
        errs.append(ValidationError(
            code="weight_negative", field="weight",
            message="weight must be >= 0",
        ))
    if not (0.0 <= ev.confidence <= 1.0):
        errs.append(ValidationError(
            code="confidence_out_of_range", field="confidence",
            message="confidence must be in [0, 1]",
        ))
    return errs


def _validate_edge_invalidated(ev: EdgeInvalidated) -> list[ValidationError]:
    errs: list[ValidationError] = []
    if e := _required_id(ev.edge_id, "edge_id"):
        errs.append(e)
    return errs


_VALIDATORS: dict[str, Callable[[Any], list[ValidationError]]] = {
    "drawer_captured": _validate_drawer_captured,
    "node_created": _validate_node_created,
    "node_property_set": _validate_node_property_set,
    "edge_created": _validate_edge_created,
    "edge_invalidated": _validate_edge_invalidated,
    # Other event kinds use only the generic checks.
}


# =============================================================================
# Edge constraint check (Part 3.4 forbidden patterns)
#
# Validates that an EdgeCreated's (source_kind, target_kind) is allowed for
# its edge_kind. Requires knowing the source and target node kinds, which
# typically requires a view lookup. This validator accepts an optional
# `lookup_node_kind` callable so callers can plug in their view.
# =============================================================================

NodeKindLookup = Callable[[str], NodeKind | None]


def validate_edge_kind_constraint(
    ev: EdgeCreated,
    lookup_node_kind: NodeKindLookup | None,
) -> list[ValidationError]:
    """Validate that the edge's source/target kinds are allowed for the edge_kind.

    If `lookup_node_kind` is None, this validator is a no-op (the constraint
    will be re-checked at the Rust view layer where node kinds are known).
    """
    if lookup_node_kind is None:
        return []

    errs: list[ValidationError] = []
    try:
        edge_kind = EdgeKind(ev.edge_kind)
    except ValueError:
        # already caught by per-kind validator
        return []

    constraints = EDGE_CONSTRAINTS.get(edge_kind, [])
    if not constraints or constraints == [(None, None)]:
        return []

    src_kind = lookup_node_kind(ev.source_node_id)
    tgt_kind = lookup_node_kind(ev.target_node_id)

    if src_kind is None or tgt_kind is None:
        # Can't validate; leave to Rust layer
        return []

    for required_src, required_tgt in constraints:
        if (
            (required_src is None or src_kind == required_src)
            and (required_tgt is None or tgt_kind == required_tgt)
        ):
            return []

    errs.append(ValidationError(
        code="edge_kind_constraint_violation", field="edge_kind",
        message=(
            f"edge_kind {ev.edge_kind} not allowed between "
            f"{src_kind.value} and {tgt_kind.value}"
        ),
    ))
    return errs


# =============================================================================
# Top-level validate() entry point
# =============================================================================

def validate_event(
    event: Event,
    lookup_node_kind: NodeKindLookup | None = None,
) -> ValidationResult:
    """Validate an event against per-kind, edge-constraint, and forbidden-pattern rules.

    Args:
        event: the event to validate.
        lookup_node_kind: optional callable for resolving node kinds during
            edge-constraint checks. If None, edge-kind constraints are
            deferred to the Rust view layer.

    Returns:
        ValidationResult with .ok and .errors.
    """
    errs: list[ValidationError] = []

    # Per-kind validation
    validator = _VALIDATORS.get(event.kind)
    if validator is not None:
        errs.extend(validator(event))

    # Edge-kind constraint
    if isinstance(event, EdgeCreated):
        errs.extend(validate_edge_kind_constraint(event, lookup_node_kind))

    # Universal: kind must be registered
    if event.kind not in EVENT_KIND_TO_CLASS:
        errs.append(ValidationError(
            code="unknown_event_kind", field="kind",
            message=f"unregistered event kind: {event.kind!r}",
        ))

    if errs:
        return ValidationResult.failed(*errs)
    return ValidationResult.passed()
