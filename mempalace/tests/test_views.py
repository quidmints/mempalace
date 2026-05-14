"""Tests for derived views (Part 3)."""

from __future__ import annotations

import unittest

from mempalace.schema.kinds import NodeKind
from mempalace.tests.conftest import fresh_palace
from mempalace.views.current import (
    canonical_nodes,
    current_node,
    nodes_of_kind,
    outgoing_edges,
    tick_views,
)


class TestViewsBasics(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()
        self.graph = self.p["graph"]

    def test_create_theme_then_lookup(self) -> None:
        tid = self.graph.create_theme(name="Running")
        tick_views()
        themes = nodes_of_kind(NodeKind.THEME)
        ids = [t.node_id for t in themes]
        self.assertIn(tid, ids)
        node = current_node(tid)
        self.assertIsNotNone(node)
        self.assertEqual(node.properties.get("name"), "Running")

    def test_create_period_under_theme(self) -> None:
        tid = self.graph.create_theme(name="Diet")
        pid = self.graph.create_period(
            theme_id=tid, name="Q1", started_at_ms=1_000_000,
        )
        tick_views()
        periods = nodes_of_kind(NodeKind.PERIOD)
        self.assertIn(pid, [p.node_id for p in periods])

    def test_add_assertion_creates_edges(self) -> None:
        a = self.graph.create_entity(name="Alice")
        c = self.graph.create_entity(name="coffee")
        aid = self.graph.add_assertion(
            subject_id=a, predicate="loves", object_id=c, confidence=0.9,
        )
        tick_views()
        # outgoing from the assertion: asserted_subject + asserted_object
        from mempalace.schema.kinds import EdgeKind
        subj_edges = outgoing_edges(aid, kind=EdgeKind.ASSERTED_SUBJECT)
        obj_edges = outgoing_edges(aid, kind=EdgeKind.ASSERTED_OBJECT)
        self.assertEqual(len(subj_edges), 1)
        self.assertEqual(len(obj_edges), 1)
        self.assertEqual(subj_edges[0].target_node_id, a)
        self.assertEqual(obj_edges[0].target_node_id, c)

    def test_assert_triple_deprecation_alias_works(self) -> None:
        """Backwards-compat: old callers using assert_triple still work
        but emit a DeprecationWarning."""
        import warnings

        a = self.graph.create_entity(name="Alice")
        c = self.graph.create_entity(name="coffee")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            aid = self.graph.assert_triple(
                subject_id=a, predicate="loves", object_id=c, confidence=0.9,
            )
            # Behavior identical to add_assertion
            self.assertTrue(aid.startswith("ast_"))
            # And a DeprecationWarning was emitted
            deprecation_warnings = [
                warning for warning in w
                if issubclass(warning.category, DeprecationWarning)
            ]
            self.assertEqual(len(deprecation_warnings), 1)
            self.assertIn("assert_triple", str(deprecation_warnings[0].message))
            self.assertIn("add_assertion", str(deprecation_warnings[0].message))


class TestCanonicalNodes(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()
        self.graph = self.p["graph"]

    def test_canonical_nodes_returns_list(self) -> None:
        # Create some nodes
        self.graph.create_theme(name="T1")
        self.graph.create_theme(name="T2")
        tick_views()
        nodes = canonical_nodes()
        self.assertIsInstance(nodes, list)


if __name__ == "__main__":
    unittest.main()
