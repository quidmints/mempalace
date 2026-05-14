"""Tests for Track 4A's invalidation bridge.

Covers:
  - start_invalidation_bridge() registers; idempotent.
  - DrawerInvalidated → cache entries with substrate_drawer dep evict.
  - DrawerRevalidated → tracker re-marks clean (cache stays cleared).
  - NodeInvalidated → cache entries with node-keyed deps evict.
  - EdgeInvalidated → cache entries with edge-keyed deps evict.
  - Cross-flow: capture drawer → ranker computes + caches with
    substrate_drawer dep → user invalidates drawer → cache evicts.
"""

from __future__ import annotations

import unittest

from mempalace.derived.dependency import (
    edge_field,
    embedding_key,
    feature_key,
    get_dependency_tracker,
    ranker_output_pattern_key,
    substrate_drawer,
    substrate_field,
)
from mempalace.derived.invalidation_bridge import (
    is_invalidation_bridge_started,
    start_invalidation_bridge,
    tick_invalidation_bridge,
)
from mempalace.derived.ranker_cache import (
    RankerOutputCache,
    RankerOutputCacheKey,
    get_default_cache,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.invalidate import (
    invalidate_drawer,
    invalidate_edge,
    invalidate_node,
    revalidate_drawer,
    revalidate_node,
)


# =============================================================================
# Bridge lifecycle
# =============================================================================


class TestBridgeLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_starts_idle(self) -> None:
        self.assertFalse(is_invalidation_bridge_started())

    def test_start_idempotent(self) -> None:
        start_invalidation_bridge()
        self.assertTrue(is_invalidation_bridge_started())
        start_invalidation_bridge()  # second call should not raise
        self.assertTrue(is_invalidation_bridge_started())


# =============================================================================
# Drawer invalidation propagation
# =============================================================================


class TestDrawerInvalidationPropagation(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        start_invalidation_bridge()

    def test_drawer_invalidate_evicts_cache(self) -> None:
        """A cache entry with a substrate_drawer dep evicts when the
        drawer is invalidated."""
        cache = get_default_cache()
        drawer_id = "drw_test_xxxxxxxx"

        # Cache something with a drawer dependency
        key = RankerOutputCacheKey("q-h", "ranker-v1", "cs-x")
        deps = [substrate_drawer(drawer_id)]
        cache.put(key, ["ranked-result"], substrate_deps=deps)
        self.assertEqual(len(cache), 1)

        # User invalidates the drawer
        invalidate_drawer(drawer_id, log_client=self.p["log"])
        tick_invalidation_bridge()

        # Cache entry is gone
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.get(key))

    def test_drawer_invalidate_does_not_evict_unrelated(self) -> None:
        """A cache entry that has no drawer dep stays put."""
        cache = get_default_cache()

        # Two entries: one with drawer dep, one without
        key_a = RankerOutputCacheKey("q-a", "r", "cs-a")
        key_b = RankerOutputCacheKey("q-b", "r", "cs-b")
        cache.put(
            key_a,
            ["a"],
            substrate_deps=[substrate_drawer("drw_target_xxxxxxxx")],
        )
        cache.put(
            key_b,
            ["b"],
            substrate_deps=[substrate_field("nde_other", "name")],
        )
        self.assertEqual(len(cache), 2)

        invalidate_drawer("drw_target_xxxxxxxx", log_client=self.p["log"])
        tick_invalidation_bridge()

        # key_a evicted; key_b survives
        self.assertEqual(len(cache), 1)
        self.assertIsNone(cache.get(key_a))
        self.assertIsNotNone(cache.get(key_b))

    def test_drawer_revalidate_does_not_restore_cache(self) -> None:
        """Revalidation un-marks dirty but does NOT restore cached
        values that were evicted. The cache must recompute on next
        access."""
        cache = get_default_cache()
        drawer_id = "drw_resurrect_xxx"

        key = RankerOutputCacheKey("q-h", "r", "cs-x")
        cache.put(key, ["v"], substrate_deps=[substrate_drawer(drawer_id)])

        invalidate_drawer(drawer_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        self.assertEqual(len(cache), 0)

        revalidate_drawer(drawer_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        # Cache is still empty — revalidation doesn't restore
        self.assertEqual(len(cache), 0)


# =============================================================================
# Node invalidation propagation
# =============================================================================


class TestNodeInvalidationPropagation(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        start_invalidation_bridge()

    def test_node_invalidate_evicts_node_dep(self) -> None:
        """Cache entries with substrate_field(node_id, ...) deps
        evict when the node is invalidated."""
        cache = get_default_cache()
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import (
            make_entity_id,
            make_event_id_log,
        )

        # Seed a real node so invalidate_node can find it (the node
        # invalidation event runs through views.current; we need
        # views to know the node exists)
        node_id = make_entity_id()
        evt = NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={},
        )
        self.p["log"].append(evt)

        # Cache an entry depending on a field of this node
        key = RankerOutputCacheKey("q-h", "r", "cs")
        cache.put(
            key,
            ["v"],
            substrate_deps=[substrate_field(node_id, "name")],
        )
        self.assertEqual(len(cache), 1)

        # Invalidate node → bridge propagates
        invalidate_node(node_id, log_client=self.p["log"])
        tick_invalidation_bridge()

        # Cache evicted
        self.assertEqual(len(cache), 0)

    def test_node_invalidate_evicts_feature_dep(self) -> None:
        """FEATURE deps keyed by node_id also propagate."""
        cache = get_default_cache()
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import (
            make_entity_id,
            make_event_id_log,
        )

        node_id = make_entity_id()
        self.p["log"].append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={},
        ))

        key = RankerOutputCacheKey("q-h", "r", "cs")
        cache.put(
            key,
            ["v"],
            substrate_deps=[feature_key(node_id, "centrality")],
        )

        invalidate_node(node_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        self.assertEqual(len(cache), 0)

    def test_node_revalidate_marks_clean(self) -> None:
        """After revalidate, the tracker no longer reports artifacts
        as dirty.

        invalidate(dep) marks the artifacts that depend on it dirty,
        not the dep itself. Revalidate marks them clean again.
        """
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import (
            make_entity_id,
            make_event_id_log,
        )

        node_id = make_entity_id()
        self.p["log"].append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={},
        ))

        # Register a dep edge: artifact reads node-field
        tracker = get_dependency_tracker()
        artifact = ranker_output_pattern_key("q", "r", "cs")
        dep = substrate_field(node_id, "name")
        tracker.record_dependency(artifact, dep)

        # Invalidate the node → bridge invalidates `dep`, which
        # propagates dirty to `artifact`
        invalidate_node(node_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        self.assertTrue(
            tracker.is_dirty(artifact),
            "artifact should be dirty after node invalidation",
        )

        # Revalidating clears the artifact's dirty flag
        revalidate_node(node_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        self.assertFalse(
            tracker.is_dirty(artifact),
            "artifact should be clean after node revalidation",
        )


# =============================================================================
# Edge invalidation propagation
# =============================================================================


class TestEdgeInvalidationPropagation(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        start_invalidation_bridge()

    def test_edge_invalidate_evicts_edge_dep(self) -> None:
        cache = get_default_cache()
        from mempalace.schema.events import EdgeCreated, NodeCreated
        from mempalace.schema.identifiers import (
            make_edge_id,
            make_entity_id,
            make_event_id_log,
        )

        # Seed two nodes + an edge so invalidate_edge has something
        # to operate on
        a = make_entity_id()
        b = make_entity_id()
        for n in (a, b):
            self.p["log"].append(NodeCreated(
                event_id=make_event_id_log(),
                recorded_at=1000,
                actor="test",
                node_id=n,
                node_kind="entity",
                properties={},
            ))
        edge_id = make_edge_id()
        self.p["log"].append(EdgeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            edge_id=edge_id,
            edge_kind="asserted_subject",
            source_node_id=a,
            target_node_id=b,
        ))

        # Cache an entry that depends on this edge
        key = RankerOutputCacheKey("q", "r", "cs")
        cache.put(
            key,
            ["v"],
            substrate_deps=[edge_field(edge_id, "weight")],
        )

        invalidate_edge(edge_id, log_client=self.p["log"])
        tick_invalidation_bridge()
        self.assertEqual(len(cache), 0)


# =============================================================================
# End-to-end: full pipeline
# =============================================================================


class TestEndToEndCacheInvalidation(unittest.TestCase):
    """The full path: cache something with a real drawer dep,
    invalidate the drawer through the user-facing API, confirm
    cache evicts and downstream re-compute happens correctly."""

    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        start_invalidation_bridge()

    def test_full_eviction_flow(self) -> None:
        from mempalace.drawer.capture import capture_drawer
        from mempalace.embed.client import EmbeddingStore, InMemoryBackend
        from mempalace.embed.model import EmbeddingService
        from mempalace.schema.kinds import InteractionalKind

        cache = get_default_cache()

        # Capture a real drawer
        result = capture_drawer(
            transcript="real drawer content",
            actor="test",
            duration_ms=500,
            log_client=self.p["log"],
            embedding_service=EmbeddingService(),
            embedding_store=EmbeddingStore(backend=InMemoryBackend()),
            interactional=InteractionalKind.MEMO_TO_SELF,
        )
        drawer_id = result.drawer_id

        # Simulate a ranker run that depended on the drawer
        key = RankerOutputCacheKey("query-hash", "factored", "cs-1")
        cache.put(
            key,
            ["scored-cand-1", "scored-cand-2"],
            substrate_deps=[substrate_drawer(drawer_id)],
        )

        # Verify cached
        first_get = cache.get(key)
        self.assertIsNotNone(first_get)
        self.assertEqual(first_get, ["scored-cand-1", "scored-cand-2"])

        # User decides to hide the drawer
        invalidate_drawer(drawer_id, log_client=self.p["log"])
        tick_invalidation_bridge()

        # Cache entry gone — next ranker call recomputes from
        # whatever-is-left after the drawer's been hidden
        second_get = cache.get(key)
        self.assertIsNone(second_get)


if __name__ == "__main__":
    unittest.main()
