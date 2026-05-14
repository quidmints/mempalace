"""
Enums and constants for node kinds, edge kinds, derivation types, and other
taxonomies used across the system.

Pure data, no logic. The single source of truth for type-name strings.

Spec ref: Part 3 (master views)
"""

from __future__ import annotations

from enum import Enum
from typing import Final


# =============================================================================
# Node kinds (Part 3.1)
# =============================================================================

class NodeKind(str, Enum):
    THEME = "theme"
    PERIOD = "period"
    EVENT = "event"
    ENTITY = "entity"
    SCHEMA = "schema"
    ASSERTION = "assertion"             # renamed from "triple" per R1
    DRAWER_REF = "drawer_ref"
    RECURRENCE_CLUSTER = "recurrence_cluster"


# =============================================================================
# Edge kinds (Part 3.2)
#
# Grouped by structural role. Each edge has constraints on (source_kind,
# target_kind) which are enforced by validators.py at append time.
# =============================================================================

class EdgeKind(str, Enum):
    # Hierarchy and structural composition
    CONTAINS = "contains"
    PARTICIPATES_IN = "participates_in"
    LOCATED_AT = "located_at"
    SUCCEEDS = "succeeds"
    PRECEDES = "precedes"

    # Conceptual layer
    INSTANTIATES = "instantiates"
    REFINES = "refines"
    SUPERSEDES = "supersedes"

    # Goals as edges (R1: not as entity subtype)
    PURSUES = "pursues"
    AIMED_AT = "aimed_at"
    BLOCKS = "blocks"
    ENABLES = "enables"

    # Provenance and inference
    DERIVED_FROM = "derived_from"
    ASSERTED_SUBJECT = "asserted_subject"
    ASSERTED_OBJECT = "asserted_object"

    # Tension and resolution
    CONTRADICTS = "contradicts"
    SUPPORTS = "supports"

    # Identity and association
    SAME_AS = "same_as"
    RECURRENCE_WITH = "recurrence_with"

    # Inhibition (Conway: distinct from decay)
    INHIBITS = "inhibits"

    # Self-entity bindings (Moulin/Rathbone "I am" findings)
    ROLE_IN_PERIOD = "role_in_period"

    # Voice stack (Track 1A — VOICE_STACK_DESIGN.md)
    DRAWER_HAS_SEGMENT = "drawer_has_segment"
    """A drawer contains a first-class segment with its own boundaries
    and aggregate paralinguistic profile."""

    VOICE_MATCHES_REFERENCE = "voice_matches_reference"
    """A drawer segment's voiceprint matches an entity reference. The
    reference is just another entity in the DAG (a colleague the user
    has spoken with, a celebrity the user has referenced). No global
    voice database."""

    PARALINGUISTIC_EVENT_AT = "paralinguistic_event_at"
    """A paralinguistic event (laughter, sigh, breath, code-switching)
    occurred at a specific segment. The event is a first-class node so
    retrieval can ask 'show me drawers with code-switching' via the
    edge-traversal machinery."""

    INTERPRETATION_MEMO_FOR = "interpretation_memo_for"
    """A drawer authored shortly after another links via this edge to
    declare an interpretation override. Targets a drawer (whole-drawer
    memo) — see also INTERPRETATION_MEMO_FOR_SEGMENT for
    finer-grained targeting."""

    INTERPRETATION_MEMO_FOR_SEGMENT = "interpretation_memo_for_segment"
    """Same as INTERPRETATION_MEMO_FOR but targets a specific
    segment. 'The angry tone in seconds 12-18 was theatrical.'
    The refinement engine treats memos as ground truth — they
    override prosody/affect inference for the targeted span."""


# =============================================================================
# Derivation types
#
# Carried on `derived_from` edges. Indicates the epistemic act that produced
# the derivation. Affects confidence aggregation and propagation rules.
# =============================================================================

class DerivationType(str, Enum):
    OBSERVATION = "OBSERVATION"     # directly read from a single drawer
    INFERENCE = "INFERENCE"         # synthesized from multiple drawers
    ASSUMPTION = "ASSUMPTION"       # asserted without drawer support
    CANONICAL = "CANONICAL"         # derived from a canon entry
    AUTO_MERGE = "AUTO_MERGE"       # miner-asserted same_as / merge
    USER_MERGE = "USER_MERGE"       # user-confirmed merge


# =============================================================================
# Schema kinds (R1 §3.1)
# =============================================================================

class SchemaKind(str, Enum):
    TRAIT = "trait"
    RELATIONAL = "relational"
    POSSIBLE_SELF = "possible_self"
    SELF_GUIDE = "self_guide"
    VALUE = "value"


# =============================================================================
# Period state (Part 3.1)
# =============================================================================

class PeriodState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SEALED = "sealed"


# =============================================================================
# Memory types (Class 1 miner output)
#
# Initial seed set for the canonicalizer (R3 §4). Miner is allowed to propose
# new types; they enter as candidates and promote to canonical with audit.
# =============================================================================

class MemoryType(str, Enum):
    CORRECTION = "correction"
    DECISION = "decision"
    FACT = "fact"
    PREFERENCE = "preference"
    LESSON = "lesson"
    OBSERVATION = "observation"
    REFLECTION = "reflection"


# =============================================================================
# Entity types (Part 3.1)
#
# `sui_generis` per R1: allows entities that don't fit a canonical universal
# (specific events as referents, concepts the user thinks about, artifacts,
# relationship-as-referent, etc.) without forcing classification.
# =============================================================================

class EntityType(str, Enum):
    PERSON = "person"
    PLACE = "place"
    OBJECT = "object"
    SUI_GENERIS = "sui_generis"


# =============================================================================
# Interactional kind (Part 5; structural-facet typed field)
#
# Per R3: interactional folded into structural facet as a typed property,
# not its own facet.
# =============================================================================

class InteractionalKind(str, Enum):
    MEMO_TO_SELF = "memo_to_self"
    DICTATION = "dictation"
    CONVERSATION = "conversation"
    OVERHEARD = "overheard"
    AUDIO_LETTER = "audio_letter"


# =============================================================================
# Privacy modes (R3 §1.4)
#
# Determines what stack-execution constraints apply. LOCAL_ONLY stacks must
# wrap every InferenceStep in AttestedStep; SANDBOX stacks have the same
# requirement inside the sandbox boundary; EXTERNAL stacks treat attestation
# as informational.
# =============================================================================

class PrivacyMode(str, Enum):
    LOCAL_ONLY = "LOCAL_ONLY"
    SANDBOX = "SANDBOX"
    EXTERNAL = "EXTERNAL"


# =============================================================================
# Resolvability classifications (R3 §3.2)
#
# Output of the resolvability classifier; determines which resolution path
# a market routes to.
# =============================================================================

class Resolvability(str, Enum):
    PUBLIC_LLM_RESOLVABLE = "PUBLIC_LLM_RESOLVABLE"
    PRIVACY_PRESERVING_REQUIRED = "PRIVACY_PRESERVING_REQUIRED"
    JURY_ONLY = "JURY_ONLY"
    NOT_RESOLVABLE = "NOT_RESOLVABLE"


# =============================================================================
# Contradiction resolution states (R1 §3.2)
# =============================================================================

class ContradictionResolution(str, Enum):
    UNRESOLVED = "unresolved"
    OUTWEIGHED = "outweighed"           # one assertion wins on weight/confidence
    JUSTIFIED = "justified"             # both can stand; context differs
    CLOSED = "closed"                   # superseded; no longer relevant
    SUPERSEDED = "superseded"           # one explicitly replaced the other


# =============================================================================
# Resolvability sub-classifications for stack composition
# =============================================================================

class ConsumerKind(str, Enum):
    """Which consumer is asking. Drives stance defaults and ranker dispatch."""
    CLAUDE_THREAD = "claude_thread"
    MONTAGE = "montage"
    MATCHING = "matching"
    FOYER = "foyer"
    AGENT = "agent"
    REVIEW_MODE = "review_mode"
    RESOLVE = "resolve"


# =============================================================================
# Fidelity ladder (Part 6.3)
# =============================================================================

class Fidelity(str, Enum):
    TRAVERSAL = "traversal"     # local graph topology only, no content
    META = "meta"               # node fields about a single node
    SUMMARY = "summary"         # synthesized gist
    FULL = "full"               # verbatim content


# =============================================================================
# Job state transitions (Part 1.3)
# =============================================================================

class JobState(str, Enum):
    SCHEDULED = "scheduled"
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    RESUMED = "resumed"


# =============================================================================
# Coherence kinds for transition cache (Part 5.4)
# =============================================================================

class CoherenceKind(str, Enum):
    SEMANTIC = "semantic"
    ACOUSTIC = "acoustic"
    STRUCTURAL = "structural"
    SOCIAL = "social"
    CONCEPTUAL_RHYME = "conceptual_rhyme"
    PHONETIC_RHYME = "phonetic_rhyme"


# =============================================================================
# Canonicalizer domains (R3 §4.2)
# =============================================================================

class CanonicalDomain(str, Enum):
    PREDICATE = "predicate"
    MEMORY_TYPE = "memory_type"
    SCHEMA_NAME = "schema_name"
    ENTITY_ALIAS = "entity_alias"
    PERIOD_NAME = "period_name"
    THEME_NAME = "theme_name"
    GOAL_MARKER = "goal_marker"


# Default thresholds per canonicalizer domain. Per-palace configurable.
CANONICAL_THRESHOLDS: Final[dict[CanonicalDomain, float]] = {
    CanonicalDomain.PREDICATE: 0.85,
    CanonicalDomain.MEMORY_TYPE: 0.90,
    CanonicalDomain.SCHEMA_NAME: 0.78,
    CanonicalDomain.ENTITY_ALIAS: 0.92,
    CanonicalDomain.PERIOD_NAME: 0.80,
    CanonicalDomain.THEME_NAME: 0.85,
    CanonicalDomain.GOAL_MARKER: 0.75,
}

# Promotion threshold: how many cluster members + how many miner passes before
# a candidate is promoted to canonical (R3 §4.3).
CANONICAL_MIN_CLUSTER_MEMBERS: Final[int] = 3
CANONICAL_MIN_STABLE_PASSES: Final[int] = 2
CANONICAL_PROMOTION_THRESHOLD_BUMP: Final[float] = 0.05


# =============================================================================
# Heat dynamics (Part 6.1, R3 §8)
# =============================================================================

# Hysteresis thresholds for canon entry and exit.
CANON_ENTRY_HEAT_THRESHOLD: Final[float] = 0.85
CANON_EXIT_HEAT_THRESHOLD: Final[float] = 0.50  # lower than entry, hysteresis
CANON_HEAT_FLOOR: Final[float] = 0.95           # canonical nodes pinned high

# Decay parameters
DEFAULT_HEAT_HALF_LIFE_DAYS: Final[float] = 30.0
HEAT_FLOOR: Final[float] = 0.10                 # non-canonical heat floor


# =============================================================================
# Velocity windows (Part 6.1)
# =============================================================================

VELOCITY_WINDOW_DAYS: Final[tuple[int, ...]] = (7, 30, 90)


# =============================================================================
# Match request lifecycle (R3 §3.3)
# =============================================================================

DEFAULT_MATCH_CACHE_TTL_DAYS: Final[int] = 7
DEFAULT_BASELINE_WINDOW_MIN_DAYS: Final[int] = 90
DEFAULT_HEARTBEAT_INTERVAL_HOURS: Final[int] = 1
DEFAULT_HEARTBEAT_GRACE_COUNT: Final[int] = 3   # missed before lockout


# =============================================================================
# Triage feedback loop (R3 §8.3)
# =============================================================================

TRIAGE_COOLDOWN_DAYS: Final[int] = 30
TRIAGE_DIMENSION_FLOOR: Final[float] = 0.10     # cap on down-weighting


# =============================================================================
# Edge-kind constraints
#
# Maps each edge kind to allowed (source_kind, target_kind) pairs. None means
# either side is unconstrained at the kind level (further validation at
# property level by validators.py).
# =============================================================================

EdgeConstraint = tuple[NodeKind | None, NodeKind | None]

EDGE_CONSTRAINTS: Final[dict[EdgeKind, list[EdgeConstraint]]] = {
    EdgeKind.CONTAINS: [
        (NodeKind.THEME, NodeKind.PERIOD),
        (NodeKind.PERIOD, NodeKind.EVENT),
        (NodeKind.EVENT, NodeKind.DRAWER_REF),
    ],
    EdgeKind.PARTICIPATES_IN: [
        (NodeKind.ENTITY, NodeKind.EVENT),
    ],
    EdgeKind.LOCATED_AT: [
        (NodeKind.EVENT, NodeKind.ENTITY),
    ],
    EdgeKind.SUCCEEDS: [
        (NodeKind.PERIOD, NodeKind.PERIOD),
        (NodeKind.EVENT, NodeKind.EVENT),
    ],
    EdgeKind.PRECEDES: [
        (NodeKind.PERIOD, NodeKind.PERIOD),
        (NodeKind.EVENT, NodeKind.EVENT),
    ],
    EdgeKind.INSTANTIATES: [
        (NodeKind.EVENT, NodeKind.SCHEMA),
        (NodeKind.ASSERTION, NodeKind.SCHEMA),
    ],
    EdgeKind.REFINES: [
        (NodeKind.SCHEMA, NodeKind.SCHEMA),
        (NodeKind.ASSERTION, NodeKind.ASSERTION),
    ],
    EdgeKind.SUPERSEDES: [
        # supersedes can apply to any same-kind versioning; left unconstrained
        # at the kind level. validators.py enforces same-kind invariant.
        (None, None),
    ],
    EdgeKind.PURSUES: [
        (NodeKind.PERIOD, NodeKind.ENTITY),
        (NodeKind.PERIOD, NodeKind.SCHEMA),
        (NodeKind.ENTITY, NodeKind.ENTITY),  # self-entity → state-of-affairs
        (NodeKind.ENTITY, NodeKind.SCHEMA),
    ],
    EdgeKind.AIMED_AT: [
        (NodeKind.EVENT, NodeKind.ENTITY),
        (NodeKind.EVENT, NodeKind.SCHEMA),
    ],
    EdgeKind.BLOCKS: [(None, None)],
    EdgeKind.ENABLES: [(None, None)],
    EdgeKind.DERIVED_FROM: [
        (NodeKind.ASSERTION, NodeKind.DRAWER_REF),
        (NodeKind.SCHEMA, NodeKind.EVENT),
        (NodeKind.SCHEMA, NodeKind.ASSERTION),
    ],
    EdgeKind.ASSERTED_SUBJECT: [
        (NodeKind.ASSERTION, NodeKind.ENTITY),
        (NodeKind.ASSERTION, NodeKind.SCHEMA),
    ],
    EdgeKind.ASSERTED_OBJECT: [
        (NodeKind.ASSERTION, NodeKind.ENTITY),
        (NodeKind.ASSERTION, NodeKind.SCHEMA),
        (NodeKind.ASSERTION, NodeKind.ASSERTION),  # assertion-as-object (RDF*)
    ],
    EdgeKind.CONTRADICTS: [
        (NodeKind.ASSERTION, NodeKind.ASSERTION),
    ],
    EdgeKind.SUPPORTS: [
        (NodeKind.DRAWER_REF, NodeKind.ASSERTION),
        (NodeKind.SCHEMA, NodeKind.SCHEMA),
        (NodeKind.ASSERTION, NodeKind.ASSERTION),
    ],
    EdgeKind.SAME_AS: [(None, None)],     # same-kind enforced by validator
    EdgeKind.RECURRENCE_WITH: [
        (NodeKind.DRAWER_REF, NodeKind.RECURRENCE_CLUSTER),
    ],
    EdgeKind.INHIBITS: [
        # Context-node → target-node. Context can be a period, schema, or
        # stance-tagged synthetic node. Target is whatever should be
        # suppressed under that context.
        (None, None),
    ],
    EdgeKind.ROLE_IN_PERIOD: [
        (NodeKind.ENTITY, NodeKind.SCHEMA),    # "I am [trait]"
        (NodeKind.ENTITY, NodeKind.ENTITY),    # "I am [other-entity]'s [role]"
    ],
}


# =============================================================================
# Forbidden patterns (R3 §3.4)
#
# Validated at append time. These return validation errors that emit
# `append_rejected` events.
# =============================================================================

FORBIDDEN_PATTERNS: Final[tuple[str, ...]] = (
    "asserted_subject_with_non_assertion_source",
    "pursues_with_no_target",
    "canonical_node_without_canon_path",
    "same_as_across_kinds",
    "edge_invalidated_for_already_invalidated",
    "node_created_for_existing_id",
)
