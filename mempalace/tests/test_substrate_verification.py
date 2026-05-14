"""
Tests for R3 §9.3 substrate verification and span-pointer provenance.

Coverage:
  - DrawerSpan dataclass: properties, edge-property round-trip, edge cases
  - Graph.add_assertion(derived_from_spans=...): persistence on edges
  - drawer_text() accessor: v0 plaintext / v2 ciphertext-only
  - HandleState.substrate_verification flag
  - Faithfulness scorer: grounded / confabulated / drawer-only / multi-span
  - Threshold helpers: filter_low_faithfulness
"""

from __future__ import annotations

import unittest

from mempalace.drawer.capture import capture_drawer
from mempalace.retrieve.handle import HandleManager
from mempalace.retrieve.scope import Scope
from mempalace.retrieve.substrate_verification import (
    DEFAULT_HIGH_FAITHFULNESS_THRESHOLD,
    DEFAULT_LOW_FAITHFULNESS_THRESHOLD,
    DRAWER_LEVEL_DISCOUNT,
    SubstrateFaithfulness,
    default_text_similarity,
    filter_low_faithfulness,
    verify_assertion,
    verify_assertions,
)
from mempalace.schema.events import NodeCreated
from mempalace.schema.identifiers import (
    SELF_ENTITY_ID,
    make_entity_id,
    make_event_id_log,
)
from mempalace.schema.kinds import EdgeKind
from mempalace.retrieve.fidelity import Fidelity
from mempalace.schema.stance import Stance
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.current import drawer_text, outgoing_edges, tick_views
from mempalace.views.graph import DrawerSpan, Graph


# ---------------------------------------------------------------------------
# DrawerSpan dataclass
# ---------------------------------------------------------------------------


class TestDrawerSpan(unittest.TestCase):
    def test_empty_span(self) -> None:
        span = DrawerSpan(start_token=5, end_token=5)
        self.assertTrue(span.is_empty)
        self.assertEqual(span.token_count, 0)

    def test_token_count(self) -> None:
        span = DrawerSpan(start_token=2, end_token=10)
        self.assertFalse(span.is_empty)
        self.assertEqual(span.token_count, 8)

    def test_to_edge_properties_minimal(self) -> None:
        span = DrawerSpan(start_token=3, end_token=7)
        props = span.to_edge_properties()
        self.assertEqual(props["span_start_token"], 3)
        self.assertEqual(props["span_end_token"], 7)
        # Optional fields omitted when not set
        self.assertNotIn("span_start_line", props)
        self.assertNotIn("span_end_line", props)
        self.assertNotIn("span_excerpt", props)

    def test_to_edge_properties_full(self) -> None:
        span = DrawerSpan(
            start_token=3, end_token=7,
            start_line=2, end_line=4, excerpt="hello world",
        )
        props = span.to_edge_properties()
        self.assertEqual(props["span_start_line"], 2)
        self.assertEqual(props["span_end_line"], 4)
        self.assertEqual(props["span_excerpt"], "hello world")

    def test_excerpt_capped_to_80_chars(self) -> None:
        long_text = "a" * 200
        span = DrawerSpan(start_token=0, end_token=10, excerpt=long_text)
        props = span.to_edge_properties()
        self.assertEqual(len(props["span_excerpt"]), 80)

    def test_round_trip(self) -> None:
        original = DrawerSpan(
            start_token=3, end_token=12, start_line=1, end_line=3,
            excerpt="round trip test",
        )
        props = original.to_edge_properties()
        recovered = DrawerSpan.from_edge_properties(props)
        self.assertEqual(recovered, original)

    def test_round_trip_minimal(self) -> None:
        original = DrawerSpan(start_token=0, end_token=5)
        props = original.to_edge_properties()
        recovered = DrawerSpan.from_edge_properties(props)
        self.assertEqual(recovered, original)

    def test_from_edge_properties_returns_none_when_no_span(self) -> None:
        # Edge property dict without span keys
        props = {"weight": 0.5, "confidence": 1.0}
        self.assertIsNone(DrawerSpan.from_edge_properties(props))


# ---------------------------------------------------------------------------
# Graph.add_assertion span persistence
# ---------------------------------------------------------------------------


class TestAddAssertionSpans(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.g = Graph()
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        self.trait = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=self.trait, node_kind="entity",
            properties={"name": "diligent"},
        ))
        r = capture_drawer(transcript="they are notably diligent at the work")
        self.drawer_id = r.drawer_id
        tick_views()

    def test_assertion_without_spans_unchanged(self) -> None:
        """Backwards-compat: add_assertion without derived_from_spans
        produces drawer-level provenance (no span properties on edge)."""
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[self.drawer_id],
        )
        tick_views()
        edges = outgoing_edges(aid, kind=EdgeKind.DERIVED_FROM)
        self.assertEqual(len(edges), 1)
        self.assertIsNone(DrawerSpan.from_edge_properties(edges[0].properties))

    def test_assertion_with_span_persists(self) -> None:
        span = DrawerSpan(start_token=2, end_token=5,
                          excerpt="notably diligent at")
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[self.drawer_id],
            derived_from_spans={self.drawer_id: span},
        )
        tick_views()
        edges = outgoing_edges(aid, kind=EdgeKind.DERIVED_FROM)
        self.assertEqual(len(edges), 1)
        recovered = DrawerSpan.from_edge_properties(edges[0].properties)
        self.assertEqual(recovered, span)

    def test_mixed_spanned_and_unspanned(self) -> None:
        """When some drawers have spans and others don't, both
        should be persisted correctly."""
        r2 = capture_drawer(transcript="another supporting drawer")
        tick_views()
        span = DrawerSpan(start_token=2, end_token=3)
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[self.drawer_id, r2.drawer_id],
            derived_from_spans={self.drawer_id: span},  # only first has span
        )
        tick_views()
        edges = outgoing_edges(aid, kind=EdgeKind.DERIVED_FROM)
        self.assertEqual(len(edges), 2)

        spans_by_target = {
            e.target_node_id: DrawerSpan.from_edge_properties(e.properties)
            for e in edges
        }
        self.assertEqual(spans_by_target[self.drawer_id], span)
        self.assertIsNone(spans_by_target[r2.drawer_id])


# ---------------------------------------------------------------------------
# drawer_text() accessor
# ---------------------------------------------------------------------------


class TestDrawerTextAccessor(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_v0_plaintext_drawer_returns_text(self) -> None:
        r = capture_drawer(transcript="this is the verbatim transcript")
        tick_views()
        self.assertEqual(
            drawer_text(r.drawer_id),
            "this is the verbatim transcript",
        )

    def test_unknown_drawer_returns_empty(self) -> None:
        self.assertEqual(drawer_text("drw_does_not_exist"), "")

    def test_v2_encrypted_drawer_returns_empty(self) -> None:
        """Encrypted drawers don't surface plaintext via drawer_text()
        — caller must use the secure_read path."""
        # Construct a v2 drawer manually (via capture without real SE
        # the test would still emit v0; manually emit a v2 event instead)
        from mempalace.schema.events import DrawerCaptured
        from mempalace.schema.identifiers import make_drawer_id

        log = self.p["log"]
        drawer_id = make_drawer_id()
        evt = DrawerCaptured(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            drawer_id=drawer_id,
            content_hash="h",
            encryption_schema_version="v2",
            verbatim_text="",  # ciphertext-only path
            verbatim_ciphertext=b"\x00\x01\x02",
        )
        log.append(evt)
        tick_views()
        # drawer_text returns empty even though the event was processed
        self.assertEqual(drawer_text(drawer_id), "")


# ---------------------------------------------------------------------------
# HandleState substrate_verification flag
# ---------------------------------------------------------------------------


class TestHandleStateFlag(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_default_off(self) -> None:
        mgr = HandleManager()
        hid = mgr.allocate(
            scope=Scope(),
            stance=Stance(),
        )
        h = mgr.get_state(hid)
        self.assertIsNotNone(h)
        self.assertFalse(h.substrate_verification)

    def test_can_be_enabled(self) -> None:
        mgr = HandleManager()
        hid = mgr.allocate(
            scope=Scope(),
            stance=Stance(),
        )
        h = mgr.get_state(hid)
        h.substrate_verification = True
        same = mgr.get_state(hid)
        self.assertIsNotNone(same)
        self.assertTrue(same.substrate_verification)


# ---------------------------------------------------------------------------
# Faithfulness scorer
# ---------------------------------------------------------------------------


class TestFaithfulnessScorer(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.g = Graph()
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        self.trait = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=self.trait, node_kind="entity",
            properties={"name": "curious"},
        ))

    def test_grounded_assertion_scores_above_zero(self) -> None:
        r = capture_drawer(
            transcript="they are a curious person who asks questions",
        )
        tick_views()
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[r.drawer_id],
            derived_from_spans={r.drawer_id: DrawerSpan(
                start_token=3, end_token=6,  # "a curious person"
            )},
        )
        tick_views()
        v = verify_assertion(aid)
        self.assertIsNotNone(v)
        self.assertGreater(v.aggregate_score, 0.0)

    def test_confabulated_assertion_scores_zero(self) -> None:
        r = capture_drawer(
            transcript="they went grocery shopping for milk and bread",
        )
        tick_views()
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[r.drawer_id],
            derived_from_spans={r.drawer_id: DrawerSpan(
                start_token=0, end_token=4,
            )},
        )
        tick_views()
        v = verify_assertion(aid)
        self.assertIsNotNone(v)
        self.assertEqual(v.aggregate_score, 0.0)

    def test_drawer_level_provenance_is_discounted(self) -> None:
        """An assertion derived from a drawer without a span gets
        the DRAWER_LEVEL_DISCOUNT applied — so the same content, same
        substrate, but no span should score lower than the spanned
        equivalent."""
        r = capture_drawer(transcript="they are a curious person")
        tick_views()
        # Span-pinned to the substring "a curious person"
        aid_span = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[r.drawer_id],
            derived_from_spans={r.drawer_id: DrawerSpan(
                start_token=2, end_token=5,
            )},
        )
        # Drawer-level (no span)
        aid_full = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[r.drawer_id],
        )
        tick_views()

        v_span = verify_assertion(aid_span)
        v_full = verify_assertion(aid_full)
        self.assertGreater(v_span.aggregate_score, v_full.aggregate_score)
        self.assertTrue(v_span.has_any_spans)
        self.assertFalse(v_full.has_any_spans)

    def test_max_of_spans_aggregation(self) -> None:
        """An assertion with multiple supporting drawers gets
        max-of-spans, so a single well-grounded span is enough."""
        r_good = capture_drawer(transcript="they are a curious person here")
        r_bad = capture_drawer(transcript="totally unrelated content here")
        tick_views()
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=self.trait,
            derived_from_drawers=[r_good.drawer_id, r_bad.drawer_id],
            derived_from_spans={
                r_good.drawer_id: DrawerSpan(start_token=2, end_token=5),
                r_bad.drawer_id: DrawerSpan(start_token=0, end_token=3),
            },
        )
        tick_views()
        v = verify_assertion(aid)
        # The good span has a positive score; the bad one is 0.
        # Aggregate should equal the good one (max-of-spans).
        per_span_scores = [s.faithfulness for s in v.per_span]
        self.assertEqual(v.aggregate_score, max(per_span_scores))

    def test_non_assertion_node_returns_none(self) -> None:
        # Pass an entity ID, not an assertion ID
        v = verify_assertion(self.trait)
        self.assertIsNone(v)

    def test_unknown_id_returns_none(self) -> None:
        v = verify_assertion("nonexistent_id_123")
        self.assertIsNone(v)


# ---------------------------------------------------------------------------
# Pluggable text_similarity
# ---------------------------------------------------------------------------


class TestPluggableSimilarity(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.g = Graph()
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        self.trait = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=self.trait, node_kind="entity",
            properties={"name": "anything"},
        ))

    def test_custom_similarity_used(self) -> None:
        r = capture_drawer(transcript="some content here")
        tick_views()
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="p", object_id=self.trait,
            derived_from_drawers=[r.drawer_id],
            derived_from_spans={r.drawer_id: DrawerSpan(0, 3)},
        )
        tick_views()

        # Custom similarity always returns 0.99
        def always_high(a: str, b: str) -> float:
            return 0.99

        v = verify_assertion(aid, text_similarity=always_high)
        self.assertEqual(v.aggregate_score, 0.99)


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------


class TestFilterLowFaithfulness(unittest.TestCase):
    def test_filter_returns_only_low_score_entries(self) -> None:
        verifications = [
            SubstrateFaithfulness(
                assertion_id="a", assertion_text="t1",
                aggregate_score=0.1, has_any_spans=True,
            ),
            SubstrateFaithfulness(
                assertion_id="b", assertion_text="t2",
                aggregate_score=0.5, has_any_spans=True,
            ),
            SubstrateFaithfulness(
                assertion_id="c", assertion_text="t3",
                aggregate_score=0.2, has_any_spans=False,
            ),
        ]
        flagged = filter_low_faithfulness(verifications)
        ids = [f.assertion_id for f in flagged]
        # 0.1 and 0.2 < 0.3 (default threshold); 0.5 isn't
        self.assertIn("a", ids)
        self.assertIn("c", ids)
        self.assertNotIn("b", ids)

    def test_filter_with_custom_threshold(self) -> None:
        verifications = [
            SubstrateFaithfulness(
                assertion_id="a", assertion_text="",
                aggregate_score=0.5, has_any_spans=True,
            ),
        ]
        flagged = filter_low_faithfulness(verifications, threshold=0.6)
        self.assertEqual(len(flagged), 1)


class TestThresholdProperties(unittest.TestCase):
    def test_low_faithfulness_property(self) -> None:
        v = SubstrateFaithfulness(
            assertion_id="x", assertion_text="",
            aggregate_score=DEFAULT_LOW_FAITHFULNESS_THRESHOLD - 0.01,
        )
        self.assertTrue(v.is_low_faithfulness)

    def test_high_faithfulness_property(self) -> None:
        v = SubstrateFaithfulness(
            assertion_id="x", assertion_text="",
            aggregate_score=DEFAULT_HIGH_FAITHFULNESS_THRESHOLD + 0.01,
        )
        self.assertTrue(v.is_high_faithfulness)

    def test_drawer_level_discount_applied(self) -> None:
        """Drawer-level (no span) provenance applies the discount.
        Use a custom similarity that returns 1.0 to isolate the
        discount factor."""
        reset_module_state()
        fresh_palace()
        g = Graph()
        from mempalace.log.client import get_default_client
        log = get_default_client()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        trait = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=trait, node_kind="entity", properties={"name": "x"},
        ))
        r = capture_drawer(transcript="content here")
        tick_views()
        aid = g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="p", object_id=trait,
            derived_from_drawers=[r.drawer_id],  # no span
        )
        tick_views()
        # Custom similarity returns 1.0; the discount factor should
        # be the only thing reducing the score.
        v = verify_assertion(aid, text_similarity=lambda a, b: 1.0)
        self.assertAlmostEqual(v.aggregate_score, DRAWER_LEVEL_DISCOUNT)


# ---------------------------------------------------------------------------
# Bulk helper
# ---------------------------------------------------------------------------


class TestVerifyAssertions(unittest.TestCase):
    def test_bulk_drops_invalid_ids(self) -> None:
        reset_module_state()
        fresh_palace()
        g = Graph()
        from mempalace.log.client import get_default_client
        log = get_default_client()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        trait = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1000, actor="t",
            node_id=trait, node_kind="entity", properties={"name": "x"},
        ))
        r = capture_drawer(transcript="something something content")
        tick_views()
        aid = g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="p", object_id=trait,
            derived_from_drawers=[r.drawer_id],
        )
        tick_views()

        results = verify_assertions([aid, "garbage_id", trait])
        # Only the valid assertion should yield a result.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].assertion_id, aid)


# ---------------------------------------------------------------------------
# Default similarity behavior
# ---------------------------------------------------------------------------


class TestDefaultSimilarity(unittest.TestCase):
    def test_identical_text_scores_one(self) -> None:
        self.assertEqual(default_text_similarity("hello world", "hello world"), 1.0)

    def test_disjoint_text_scores_zero(self) -> None:
        self.assertEqual(default_text_similarity("hello world", "foo bar"), 0.0)

    def test_partial_overlap(self) -> None:
        # tokens={a, b}, {b, c} → intersection={b}, union={a,b,c} → 1/3
        self.assertAlmostEqual(default_text_similarity("a b", "b c"), 1 / 3)

    def test_both_empty_scores_one(self) -> None:
        self.assertEqual(default_text_similarity("", ""), 1.0)

    def test_one_empty_scores_zero(self) -> None:
        self.assertEqual(default_text_similarity("a", ""), 0.0)


if __name__ == "__main__":
    unittest.main()
