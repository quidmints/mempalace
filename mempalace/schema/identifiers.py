"""
Identifier generation and validation for nodes, edges, drawers, handles,
events, and other persistent objects.

All IDs in the system go through this module so that namespacing, encoding,
and validation are uniform.

Spec ref: Part 1
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Final


# =============================================================================
# ID format
#
# IDs are <prefix>_<timestamp_ms>_<random>:
#   - prefix is 2-6 lowercase ASCII chars marking the kind
#   - timestamp_ms is base36-encoded, sortable
#   - random is 8 base36 chars from a CSPRNG
#
# Total length: roughly 20-26 chars. Ordered by creation time at ms resolution
# while still being globally unique within a palace.
# =============================================================================

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID_RE = re.compile(r"^[a-z]{2,6}_[0-9a-z]{1,12}_[0-9a-z]{8}$")
_RANDOM_LEN: Final[int] = 8


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    s = []
    while n:
        n, r = divmod(n, 36)
        s.append(_BASE36[r])
    return "".join(reversed(s))


def _random_base36(length: int = _RANDOM_LEN) -> str:
    out = []
    for _ in range(length):
        out.append(_BASE36[secrets.randbelow(36)])
    return "".join(out)


def make_id(prefix: str, *, ts_ms: int | None = None) -> str:
    """Generate a fresh ID with the given prefix.

    Args:
        prefix: 2-6 lowercase ASCII chars identifying the kind of object.
        ts_ms: optional explicit timestamp; defaults to current time.

    Returns:
        A new globally-unique ID for this palace.

    Raises:
        ValueError: if the prefix is malformed.
    """
    if not (2 <= len(prefix) <= 6) or not prefix.isascii() or not prefix.islower():
        raise ValueError(
            f"prefix must be 2-6 lowercase ASCII chars, got {prefix!r}"
        )
    if not prefix.replace("_", "").isalpha():
        raise ValueError(f"prefix must contain only letters, got {prefix!r}")
    if ts_ms is None:
        ts_ms = time.time_ns() // 1_000_000
    return f"{prefix}_{_to_base36(ts_ms)}_{_random_base36()}"


def is_valid_id(value: str) -> bool:
    """Return True if value matches the canonical ID format."""
    return bool(_ID_RE.match(value))


def parse_prefix(value: str) -> str:
    """Extract the kind-prefix from an ID. Raises if malformed."""
    if not is_valid_id(value):
        raise ValueError(f"not a valid id: {value!r}")
    return value.split("_", 1)[0]


# =============================================================================
# Per-kind prefixes
#
# Conventional prefixes for each kind so IDs are visually inspectable.
# =============================================================================

# Nodes
NODE_PREFIX_THEME: Final[str] = "thm"
NODE_PREFIX_PERIOD: Final[str] = "prd"
NODE_PREFIX_EVENT: Final[str] = "evt"
NODE_PREFIX_ENTITY: Final[str] = "ent"
NODE_PREFIX_SCHEMA: Final[str] = "sch"
NODE_PREFIX_ASSERTION: Final[str] = "ast"
NODE_PREFIX_DRAWER_REF: Final[str] = "drw"
NODE_PREFIX_RECURRENCE: Final[str] = "rec"

# Edges
EDGE_PREFIX: Final[str] = "edg"

# Events (in the log)
EVENT_PREFIX: Final[str] = "ev"

# Handles
HANDLE_PREFIX: Final[str] = "hdl"

# Jobs
JOB_PREFIX: Final[str] = "job"

# Sandbox sessions
SANDBOX_PREFIX: Final[str] = "sbx"

# Match requests
MATCH_PREFIX: Final[str] = "mtc"

# Canonicalizer candidates
CANDIDATE_PREFIX: Final[str] = "cnd"

# Batch framing (Phase 1)
BATCH_PREFIX: Final[str] = "bat"


# =============================================================================
# Convenience factories per kind
# =============================================================================

def make_theme_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_THEME, ts_ms=ts_ms)


def make_period_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_PERIOD, ts_ms=ts_ms)


def make_event_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_EVENT, ts_ms=ts_ms)


def make_entity_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_ENTITY, ts_ms=ts_ms)


def make_schema_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_SCHEMA, ts_ms=ts_ms)


def make_assertion_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_ASSERTION, ts_ms=ts_ms)


def make_drawer_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_DRAWER_REF, ts_ms=ts_ms)


def make_recurrence_cluster_id(ts_ms: int | None = None) -> str:
    return make_id(NODE_PREFIX_RECURRENCE, ts_ms=ts_ms)


def make_edge_id(ts_ms: int | None = None) -> str:
    return make_id(EDGE_PREFIX, ts_ms=ts_ms)


def make_event_id_log(ts_ms: int | None = None) -> str:
    """Distinct from event-node IDs: this is the log-entry ID."""
    return make_id(EVENT_PREFIX, ts_ms=ts_ms)


def make_handle_id(ts_ms: int | None = None) -> str:
    return make_id(HANDLE_PREFIX, ts_ms=ts_ms)


def make_job_id(ts_ms: int | None = None) -> str:
    return make_id(JOB_PREFIX, ts_ms=ts_ms)


def make_sandbox_id(ts_ms: int | None = None) -> str:
    return make_id(SANDBOX_PREFIX, ts_ms=ts_ms)


def make_match_id(ts_ms: int | None = None) -> str:
    return make_id(MATCH_PREFIX, ts_ms=ts_ms)


def make_candidate_id(ts_ms: int | None = None) -> str:
    return make_id(CANDIDATE_PREFIX, ts_ms=ts_ms)


def make_batch_id(ts_ms: int | None = None) -> str:
    """Generate a batch_id for a multi-event batch (Phase 1).

    Used by `LogClient.batch()` context manager. Consumers may also
    pre-compute a batch_id with their own prefix encoded into it
    (e.g. `bat_<consumer_short>_<...>`); this helper just gives a
    well-formed default.
    """
    return make_id(BATCH_PREFIX, ts_ms=ts_ms)


# =============================================================================
# Designated IDs
#
# A few IDs are special-cased rather than generated. The self-entity is the
# most important — every palace has exactly one self-entity, and assertions /
# I-am edges reference it directly.
# =============================================================================

SELF_ENTITY_ID: Final[str] = "ent_self_self0000"
"""The designated self-entity node ID.

Special-cased rather than generated: there is exactly one self-entity per
palace, created during initialization. I-am bindings (role-edges) and
self-assertions all reference this ID.

Note: this ID intentionally does not match `is_valid_id()` because it lacks
the timestamp component. The validator allows reserved IDs explicitly.
"""

RESERVED_IDS: Final[frozenset[str]] = frozenset({SELF_ENTITY_ID})


def is_reserved(value: str) -> bool:
    """Return True if value is a reserved/designated ID."""
    return value in RESERVED_IDS


def is_valid_or_reserved(value: str) -> bool:
    """Return True if value is either a valid ID or a reserved one."""
    return is_reserved(value) or is_valid_id(value)


# =============================================================================
# Content hashes (drawers, formulas, etc.)
# =============================================================================

@dataclass(frozen=True)
class ContentHash:
    """Wrapper around a hex-encoded SHA-256 digest.

    Used for drawer content addressing, formula commitment, audit log entries,
    and anywhere in the system that requires content-addressed identity.
    """
    hex: str

    def __post_init__(self) -> None:
        if not isinstance(self.hex, str):
            raise TypeError(f"content_hash must be str, got {type(self.hex)}")
        if len(self.hex) != 64:
            raise ValueError(
                f"content_hash must be 64 hex chars (sha256), got {len(self.hex)}"
            )
        if not all(c in "0123456789abcdef" for c in self.hex):
            raise ValueError("content_hash must be lowercase hex")

    def __str__(self) -> str:
        return self.hex


def make_content_hash(payload: bytes) -> ContentHash:
    """Compute SHA-256 of payload and wrap as ContentHash."""
    import hashlib
    return ContentHash(hashlib.sha256(payload).hexdigest())
