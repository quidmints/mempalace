"""
Shared test fixtures.

Tests in this directory subclass `unittest.TestCase` so they can run
with either `pytest mempalace/tests/` or `python -m unittest discover
mempalace.tests`. The `conftest.py` name is kept so pytest auto-loads
the fixtures here too.

Helpers:

  - fresh_log_client()   → (client, backend) with a fresh MockBackend
  - reset_module_state() → clears module-level singletons that survive
                           between tests (views, graph, canonicalizer,
                           proposal store, feedback ledger, formula
                           registry, mcp server)
  - fresh_palace()       → returns a tuple (log, graph, views_offset)
                           ready for tests that need a working substrate
"""

from __future__ import annotations

from typing import Any


def fresh_log_client():
    """Build a new (LogClient, MockBackend) pair backed by an empty log."""
    from mempalace.log.client import LogClient, MockBackend

    backend = MockBackend()
    client = LogClient(backend=backend)
    return client, backend


def reset_module_state() -> None:
    """Clear every module-level singleton the test suite relies on.

    Tests that mutate the default log/graph/views must call this in
    `setUp()` so they don't leak state.
    """
    # log client (use the public setter, not a private attr)
    import mempalace.log.client as log_client_mod
    new_client, _ = fresh_log_client()
    log_client_mod.set_default_client(new_client)

    # views
    import mempalace.views.current as views_current
    views_current._VIEW_STORE = None

    # subscriber registry (otherwise tick_views uses stale log client)
    import mempalace.log.subscriber as sub_mod
    sub_mod._default_registry = None

    # graph
    import mempalace.views.graph as graph_mod
    graph_mod._DEFAULT_GRAPH = None

    # canonicalizer
    import mempalace.canonicalizer as can_mod
    can_mod._CAN = None

    # proposal store
    import mempalace.miner.proposals as prop_mod
    prop_mod._STORE = None

    # feedback ledger
    import mempalace.miner.feedback as fb_mod
    fb_mod._LEDGER = None

    # formula registry
    import mempalace.resolve.formula_registry as freg
    freg._REGISTRY = None

    # mcp server
    import mempalace.mcp.server as srv
    srv._SERVER = None

    # Track 4A — dependency tracker + ranker cache + invalidation bridge
    import mempalace.derived.dependency as dep_mod
    dep_mod._TRACKER = None
    import mempalace.derived.ranker_cache as cache_mod
    cache_mod._DEFAULT_CACHE = None
    import mempalace.derived.invalidation_bridge as bridge_mod
    bridge_mod._BRIDGE_STARTED = False

    # Track 4B — cache projection
    import mempalace.derived.cache_projection as proj_mod
    proj_mod._PROJECTION = None

    # Track 6E — burn palace gate
    import mempalace.secure.burn as burn_mod
    burn_mod._GATE = None


def fresh_palace() -> dict[str, Any]:
    """Reset state and return a small dict of useful handles for tests.

    Returns:
        {
          "log": LogClient,
          "backend": MockBackend,
          "graph": Graph,
        }
    """
    from mempalace.log.client import LogClient, MockBackend
    from mempalace.views.graph import Graph

    reset_module_state()
    backend = MockBackend()
    log = LogClient(backend=backend)

    # Re-bind default log to this backend so views.current builds against it
    import mempalace.log.client as log_client_mod
    log_client_mod.set_default_client(log)

    graph = Graph(client=log)
    return {"log": log, "backend": backend, "graph": graph}


# pytest fixture aliases (for codepaths that prefer pytest fixtures)
try:
    import pytest

    @pytest.fixture
    def palace():
        return fresh_palace()

    @pytest.fixture
    def log_client():
        return fresh_log_client()
except ImportError:
    # pytest not available — unittest-only flow; the helpers above are
    # what gets used.
    pass


__all__ = ["fresh_log_client", "fresh_palace", "reset_module_state"]
