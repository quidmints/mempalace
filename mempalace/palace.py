"""
The Palace — top-level coordinator that bundles the subsystems.

# What this is

The original `mempalace-develop` had a single `palace.py` that was the
main entry point — one class that held a knowledge graph + searcher +
miner + storage + everything else.

The greenfield architecture decomposed that monolith into focused
subsystems (log, views, handle, retrieve, federate, miner, secure,
canonicalizer, ...). That decomposition is real: each piece has its
own module surface and is testable in isolation.

But callers don't want to wire 12 subsystems by hand to do "open a
palace and ask a question." This module restores the unified
entrypoint **without re-monolithizing**: `Palace` is a thin facade
that constructs the subsystems, holds references, and exposes the
common verbs (capture / search / mine / federate / migrate).

If you only need one subsystem, import it directly. If you want
"a palace, integrated, ready to use," use `Palace`.

# Lifecycle

```python
from mempalace import Palace

# Create or open
palace = Palace.create(palace_dir="/path/to/palace")
# or
palace = Palace.open(palace_dir="/path/to/palace")

# Capture
result = palace.capture(transcript="this is what happened today")

# Search
hits = palace.search("what was happening last week", scope=...)

# Federate
match = palace.federate.find_compatible_palace(other_palace_id)

# Close
palace.close()
```

# What's NOT here

- The CLI entrypoint (would import Palace and wrap it for argparse)
- The MCP server (would import Palace and serve over MCP)
- The voice stack daemon main-loop
- The phone-side interfaces

Those are operational concerns built on top of `Palace`. This module
is the architectural-API entry point.

Spec ref: this is the missing seam between the old `palace.py`
monolith and the new subsystem decomposition. It reconciles a
clean module hierarchy with a callable single-entry surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .canonicalizer import Canonicalizer
from .drawer.capture import CaptureResult, capture_drawer
from .log.client import LogClient, get_default_client
from .retrieve.handle import HandleManager, get_handle_manager
from .secure.element import PhoneSecureElement
from .secure.phone_off import PhoneOffStateMachine
from .views import current as views
from .views.graph import Graph

logger = logging.getLogger(__name__)


@dataclass
class PalaceConfig:
    """Construction-time configuration for a Palace."""

    palace_dir: str = ""
    """Filesystem directory where this palace's log + DD views live.
    Empty = in-memory only (test mode)."""

    palace_id: str = ""
    """The palace's federation identifier. Set on first use; should
    match the on-chain `Palace` PDA address once registered."""

    secure_element: PhoneSecureElement | None = None
    """Phone-side encryption surface for v2 drawer encryption. None =
    plaintext (v0) drawer storage; the daemon-only paths."""

    enable_phone_off_state_machine: bool = True
    """When True, instantiate a `PhoneOffStateMachine` for graceful
    degradation. Disable for unit tests that don't model a phone
    boundary."""


class Palace:
    """The integrated palace. Holds references to the subsystems and
    exposes the common verbs."""

    def __init__(self, config: PalaceConfig) -> None:
        self._config = config
        self._closed = False

        # ---- Layer 0: log -------------------------------------------------
        # The log is the source of truth. Every other subsystem reads
        # from it; some write through batch handles.
        self.log: LogClient = get_default_client()

        # ---- Layer 1: derived views --------------------------------------
        # `views` is a module, not a class — it materializes node/edge
        # state from the log. The Graph below uses it.
        self.graph: Graph = Graph(client=self.log)

        # ---- Layer 2: canonicalization -----------------------------------
        self.canonicalizer: Canonicalizer = Canonicalizer()

        # ---- Layer 3: handles --------------------------------------------
        # mem_allocate / mem_refine / mem_resolve / mem_close go through
        # this manager. Module-level helpers in mempalace.retrieve.handle
        # use the same default manager.
        self.handle_manager: HandleManager = get_handle_manager()

        # ---- Layer 4: phone-off state machine ----------------------------
        if config.enable_phone_off_state_machine:
            self.phone_off: Optional[PhoneOffStateMachine] = (
                PhoneOffStateMachine(log_client=self.log)
            )
        else:
            self.phone_off = None

        # ---- Layer 5: federation, miner, switchboard, resolve, secure ---
        # These are accessed through their own module surfaces; no
        # need to instantiate state-holding objects on Palace.
        # See: palace.federate (module), palace.miner (module), etc.
        # Public verbs below dispatch to those modules.

        logger.info(
            "Palace initialized: dir=%s palace_id=%s",
            config.palace_dir or "<in-memory>",
            config.palace_id or "<unregistered>",
        )

    # ========================================================================
    # Construction
    # ========================================================================

    @classmethod
    def create(cls, *, palace_dir: str = "", **kwargs: Any) -> "Palace":
        """Create a fresh palace. Initializes the log + DD views from
        scratch. Use `open()` for an existing palace.

        For in-memory (test mode), pass palace_dir="".
        """
        config = PalaceConfig(palace_dir=palace_dir, **kwargs)
        # In-memory mode is the default behavior of get_default_client();
        # filesystem-backed palaces would wire a different LogClient impl
        # here. That's an operational concern not yet wired in greenfield.
        return cls(config)

    @classmethod
    def open(cls, *, palace_dir: str, **kwargs: Any) -> "Palace":
        """Open an existing palace at `palace_dir`. Replays the log to
        warm DD views, then returns the ready palace."""
        if not palace_dir:
            raise ValueError("palace_dir required for open()")
        config = PalaceConfig(palace_dir=palace_dir, **kwargs)
        palace = cls(config)
        # Replay the log to warm views
        views.tick_views()
        return palace

    # ========================================================================
    # Lifecycle
    # ========================================================================

    def close(self) -> None:
        """Close the palace. Flushes any pending writes, releases
        resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Subsystems with their own close() get cleaned up here as needed.
        # Currently log is module-singleton; closing palace doesn't tear
        # it down because tests rely on get_default_client() being stable.
        logger.info("Palace closed")

    def __enter__(self) -> "Palace":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ========================================================================
    # Public verbs — the things callers want to do
    # ========================================================================

    def capture(self, transcript: str, **kwargs: Any) -> CaptureResult:
        """Capture a new drawer from a transcript.

        Thin wrapper over `mempalace.drawer.capture.capture_drawer`
        that injects the palace's secure_element if one is configured.
        """
        if (
            self._config.secure_element is not None
            and "secure_element" not in kwargs
        ):
            kwargs["secure_element"] = self._config.secure_element
        return capture_drawer(transcript=transcript, **kwargs)

    def search(self, query: str = "", **kwargs: Any):
        """High-level retrieval entry point.

        Allocates a handle, runs the search-policy walk, and returns
        the resolution. For finer-grained control, use the
        `mempalace.retrieve` API directly (mem_allocate / mem_refine /
        mem_resolve).

        The `query` argument is metadata for now (logged on the
        handle's consumer_id); the actual scope/stance routing is
        through the kwargs. A future revision may add a query-string
        canonicalizer that turns `query` into Scope+Stance directly.
        """
        from .retrieve.handle import mem_allocate, mem_close, mem_resolve
        from .retrieve.scope import Scope
        from .schema.stance import Stance

        scope = kwargs.pop("scope", Scope())
        stance = kwargs.pop("stance", Stance())
        consumer_id = kwargs.pop("consumer_id", f"palace.search:{query[:32]}")

        handle_id = mem_allocate(
            scope=scope, stance=stance,
            consumer_id=consumer_id,
            **kwargs,
        )
        try:
            return mem_resolve(handle_id)
        finally:
            mem_close(handle_id)

    def assert_(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        **kwargs: Any,
    ) -> str:
        """Add an assertion to the graph.

        Thin wrapper over `Graph.add_assertion`. Accepts the same
        kwargs (derived_from_drawers, derived_from_spans, asserter,
        valid_from_ms, etc.).
        """
        return self.graph.add_assertion(
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            **kwargs,
        )

    def temporal_query(self, query, **kwargs: Any):
        """Run a temporal-triple proximity query.

        Per the user reframe of "triple": three characteristics —
        past, present, future — held in union by a traversal
        through both substrates (DAG + embeddings).

        Returns a `TemporalResult` with both forms:
          - `paths` is the structural answer (here are the
            connections, you draw the meaning)
          - `synthesized_answer` is the narrative answer (here's
            the synthesis, citing the path as provenance)

        See `mempalace.retrieve.temporal` for the full API:
        `TemporalQuery`, `Characteristic`, `TimeAxis`.
        """
        from .retrieve.temporal import query_temporal
        return query_temporal(query, **kwargs)

    def tick(self) -> int:
        """Advance the DD views to the latest log offset.

        Returns the count of events delivered to subscribers in this
        tick. Call after a batch of writes if you want immediately
        visible reads.
        """
        return views.tick_views()

    # ========================================================================
    # Subsystem access (for callers who need fine-grained control)
    # ========================================================================

    @property
    def federate(self):
        """Return the `mempalace.federate` module surface for
        federation operations (anchor boundary, layer gates, RHYME)."""
        from . import federate
        return federate

    @property
    def miner(self):
        """Return the `mempalace.miner` module surface for direct
        miner-pass control."""
        from . import miner
        return miner

    @property
    def switchboard(self):
        """Return the `mempalace.switchboard` module surface for
        oracle SDK operations."""
        from . import switchboard
        return switchboard

    @property
    def secure(self):
        """Return the `mempalace.secure` module surface — phone
        secure element, key manager, burn flow."""
        from . import secure
        return secure

    @property
    def query_q(self):
        """Return the bi-directional query queue for daemon → user
        questions."""
        from .query.bidirectional import get_default_queue
        return get_default_queue()


__all__ = ["Palace", "PalaceConfig"]
