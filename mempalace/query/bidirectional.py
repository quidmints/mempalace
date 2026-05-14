"""
Bi-directional query primitives.

# What this module is

The mempalace can query the user, not just receive queries. This is
the surface for that direction.

The IO reference: a conversation with the operator's voice agent
(in the React-Native app, separate repo) makes it possible for the
mempalace to ask, prompt, and complete. The actual rendering /
turn-taking happens in the app. This module ships the substrate
side: the query types, the canonicalizer-routing, and the queue
of pending mempalace-initiated queries.

# Query types and the canonicalizer

Different query shapes route to different canonicalizers:

  - `FILL_IN_THE_BLANK`: "dum spero ..." → "spiro". The
    canonicalizer reframes the handle to a partial-match search
    over the user's substrate plus public completions. The
    ellipsis can be at any position.
  - `WHAT_IF`: counterfactual exploration. The canonicalizer
    reframes the handle as a branching walk (uses the Step
    primitive's BRANCHES intent in `traversal_extension.py`).
  - `PERIPHERAL_VISION`: returns context adjacent to a focal
    node, not the node itself. Useful for "what was I missing"
    kinds of questions.
  - `METACOGNITION`: thinking about thinking. The canonicalizer
    routes the handle through itself recursively at one extra
    level (asks: what frames *would* answer this query, and
    surface those instead of the answer).
  - `CLARIFICATION`: the mempalace prompts the user for missing
    context. Shaped like fill-in-the-blank but generated *by* the
    palace, not the user. Becomes a pending bi-directional query.

# What this module ships

  - `QueryType` enum.
  - `BiDirectionalQuery` — a query from either direction.
  - `PendingQueryQueue` — the queue of palace-initiated queries
    waiting for the user.
  - Routing helpers that pick the right canonicalizer for each
    type.

# What this module does NOT ship

  - The voice/conversation loop (separate React Native module,
    listed in REACT_NATIVE_TODO.md).
  - The actual canonicalizers — they live in their existing
    locations; this module just routes to them.
  - The walk/branch implementation for WHAT_IF — that uses the
    Step primitive's BRANCHES intent, wired up at the policy
    level (see `mempalace.stack.traversal_extension`).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


# =============================================================================
# Query types
# =============================================================================


class QueryType(str, Enum):
    """How to interpret + route a query."""

    LITERAL = "literal"
    """Standard substring / semantic search. The default."""

    FILL_IN_THE_BLANK = "fill_in_the_blank"
    """Partial expression with an ellipsis (or `...` token) somewhere.
    The canonicalizer matches the surrounding context and produces
    candidate completions. Like Google search suggestions but
    informed by the operator's substrate."""

    WHAT_IF = "what_if"
    """Counterfactual. The canonicalizer reframes the handle as a
    branching walk; each branch evaluates one counterfactual."""

    PERIPHERAL_VISION = "peripheral_vision"
    """Returns nodes adjacent to (but not including) a focal node.
    Surfaces context, not direct answers."""

    METACOGNITION = "metacognition"
    """Thinking about thinking. The canonicalizer evaluates which
    frames would best answer the query, then returns those frames
    rather than substrate matches."""

    CLARIFICATION = "clarification"
    """Mempalace-initiated. Asks the user to fill in something the
    substrate is missing. Shaped narratively (a fill-in-the-blank)
    rather than as a direct question."""


class QueryDirection(str, Enum):
    """Who initiated the query."""

    USER_TO_PALACE = "user_to_palace"
    PALACE_TO_USER = "palace_to_user"


# =============================================================================
# Query envelope
# =============================================================================


@dataclass
class BiDirectionalQuery:
    """One query in either direction."""

    query_id: str
    direction: QueryDirection
    query_type: QueryType
    text: str
    """The query surface — for FILL_IN_THE_BLANK, contains the
    ellipsis. For PALACE_TO_USER queries, is the prompt the agent
    will surface to the operator."""

    blank_position: int | None = None
    """For FILL_IN_THE_BLANK: index where the blank starts. None
    means the canonicalizer should detect."""

    context_node_ids: list[str] = field(default_factory=list)
    """For PERIPHERAL_VISION / WHAT_IF: nodes the query is centered
    on or branches off from."""

    asked_at_ms: int = 0
    expires_at_ms: int | None = None
    """When this query stops being useful. PALACE_TO_USER queries
    have an expiry so the queue doesn't accumulate stale prompts."""

    extras: dict[str, str] = field(default_factory=dict)


# =============================================================================
# Pending-query queue (palace → user)
# =============================================================================


@dataclass
class PendingQueryQueue:
    """The queue of mempalace-initiated queries awaiting the user.

    Production wiring: the conversation-mode trigger (per-day
    review, on-demand session, etc.) drains this queue. Each query
    surfaces in the voice agent; the user's response gets appended
    back to the substrate via `add_assertion` or `capture_drawer`.

    In-process queue. Production may want a persistent queue scoped
    to a user-conversation handle; this is the minimal shape.
    """

    _queue: deque[BiDirectionalQuery] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    max_size: int = 50

    def enqueue(self, query: BiDirectionalQuery) -> bool:
        """Append a query. Returns True if added, False if dropped
        (queue full or expired)."""
        if query.direction != QueryDirection.PALACE_TO_USER:
            return False
        if query.expires_at_ms is not None:
            now_ms = int(time.time() * 1000)
            if query.expires_at_ms < now_ms:
                return False
        with self._lock:
            if len(self._queue) >= self.max_size:
                return False
            self._queue.append(query)
        return True

    def drain_one(self) -> BiDirectionalQuery | None:
        """Pop the next pending query, skipping expired."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            while self._queue:
                q = self._queue.popleft()
                if q.expires_at_ms is None or q.expires_at_ms >= now_ms:
                    return q
        return None

    def peek(self, n: int = 5) -> list[BiDirectionalQuery]:
        """Look at the next N queries without popping."""
        with self._lock:
            return list(self._queue)[:n]

    def size(self) -> int:
        with self._lock:
            return len(self._queue)


_QUEUE: PendingQueryQueue | None = None


def get_default_queue() -> PendingQueryQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = PendingQueryQueue()
    return _QUEUE


def reset_default_queue() -> None:
    global _QUEUE
    _QUEUE = None


# =============================================================================
# Canonicalizer routing
# =============================================================================


_CANONICALIZER_HINTS: dict[QueryType, str] = {
    QueryType.LITERAL: "default",
    QueryType.FILL_IN_THE_BLANK: "fill_in_the_blank",
    QueryType.WHAT_IF: "what_if_branch",
    QueryType.PERIPHERAL_VISION: "peripheral_vision",
    QueryType.METACOGNITION: "metacognition",
    QueryType.CLARIFICATION: "clarification_response",
}
"""Canonicalizer routing key per query type. The actual
canonicalizer registry maps these strings to instances; see
`mempalace.canonicalize`."""


def canonicalizer_hint(query_type: QueryType) -> str:
    return _CANONICALIZER_HINTS[query_type]


# =============================================================================
# Fill-in-the-blank parser
# =============================================================================


def detect_blank_position(text: str) -> int | None:
    """Find the position of the blank in a fill-in-the-blank query.

    Recognized markers (in order of preference):
      - `...` (three ASCII dots)
      - `…` (Unicode ellipsis)
      - `___` (three underscores)
      - `_____` (any 5+ underscores)
      - `<blank>` literal token

    Returns None if no marker is found.
    """
    for marker in ("...", "…", "___", "<blank>"):
        idx = text.find(marker)
        if idx >= 0:
            return idx
    # Fallback: 5+ consecutive underscores
    for i in range(len(text) - 4):
        if text[i:i+5] == "_____":
            return i
    return None


def split_around_blank(
    text: str,
    blank_position: int,
) -> tuple[str, str]:
    """Split a fill-in-the-blank text into (prefix, suffix).

    Strips the blank marker from both sides.
    """
    prefix = text[:blank_position].rstrip()
    rest = text[blank_position:]
    # Strip the marker from the start of `rest`
    for marker in ("...", "…", "___", "<blank>"):
        if rest.startswith(marker):
            rest = rest[len(marker):]
            break
    else:
        # Underscore-run case
        i = 0
        while i < len(rest) and rest[i] == "_":
            i += 1
        rest = rest[i:]
    suffix = rest.lstrip()
    return prefix, suffix


__all__ = [
    "BiDirectionalQuery",
    "PendingQueryQueue",
    "QueryDirection",
    "QueryType",
    "canonicalizer_hint",
    "detect_blank_position",
    "get_default_queue",
    "reset_default_queue",
    "split_around_blank",
]
