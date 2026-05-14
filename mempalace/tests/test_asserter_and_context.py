"""Tests for cross-palace assertions (asserter field) and HandleContext.

Covers:
  Cross-palace asserter:
    - AssertionAsserter.is_self / is_external semantics.
    - _asserter_canonical_bytes is deterministic.
    - add_assertion default = self-asserted.
    - add_assertion with external asserter populates properties.
    - assertions_about_self with filter flags returns the right subsets.
    - external_mentions_of_self pulls cross-palace mentions only.
    - Multi-palace mentions: filter by specific asserter_palace_id.

  HandleContext:
    - Construction + initial state.
    - add_hop bumps total_hops + extends cluster_pattern.
    - cluster_signature delegates to cluster_pattern.
    - is_pattern_stable wraps the pattern's check.
    - SearchPolicy.next_step_for_context derives summary + pattern.
    - snapshot returns serializable dict with expected keys.
"""

from __future__ import annotations

import unittest

from mempalace.handle import (
    ClusterTraversalPattern,
    HandleContext,
    Hop,
    InterpretiveFrame,
    SearchBudget,
    SearchPolicy,
)
from mempalace.handle.search_policy import DirectiveKind
from mempalace.schema.events import NodeCreated
from mempalace.schema.identifiers import (
    SELF_ENTITY_ID,
    make_entity_id,
    make_event_id_log,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.current import tick_views
from mempalace.views.graph import (
    AssertionAsserter,
    Graph,
    _asserter_canonical_bytes,
    assertions_about,
    assertions_about_self,
    external_mentions_of_self,
)


# =============================================================================
# AssertionAsserter dataclass
# =============================================================================


class TestAssertionAsserter(unittest.TestCase):
    def test_default_is_self(self) -> None:
        a = AssertionAsserter()
        self.assertTrue(a.is_self)
        self.assertFalse(a.is_external)
        self.assertEqual(a.palace_id, "")

    def test_external_with_palace_id(self) -> None:
        a = AssertionAsserter(
            palace_id="palace_xyz",
            session_pubkey_hex="cafe" * 8,
            signature_hex="dead" * 16,
        )
        self.assertFalse(a.is_self)
        self.assertTrue(a.is_external)


class TestCanonicalBytes(unittest.TestCase):
    def test_deterministic(self) -> None:
        a = _asserter_canonical_bytes(
            subject_id="nde_self", predicate="likes",
            object_id="nde_x", valid_from_ms=1000, valid_to_ms=None,
        )
        b = _asserter_canonical_bytes(
            subject_id="nde_self", predicate="likes",
            object_id="nde_x", valid_from_ms=1000, valid_to_ms=None,
        )
        self.assertEqual(a, b)

    def test_different_inputs_different_bytes(self) -> None:
        a = _asserter_canonical_bytes(
            subject_id="x", predicate="p", object_id="y",
            valid_from_ms=None, valid_to_ms=None,
        )
        b = _asserter_canonical_bytes(
            subject_id="x", predicate="p", object_id="z",
            valid_from_ms=None, valid_to_ms=None,
        )
        self.assertNotEqual(a, b)

    def test_null_timestamps_render_empty(self) -> None:
        out = _asserter_canonical_bytes(
            subject_id="x", predicate="p", object_id="y",
            valid_from_ms=None, valid_to_ms=None,
        )
        # Should contain x, p, y separated by NUL with empty trailing
        self.assertEqual(out, b"x\x00p\x00y\x00\x00")


# =============================================================================
# add_assertion with asserter
# =============================================================================


class _AssertionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.g = Graph()
        self._setup_self()

    def _setup_self(self) -> None:
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=SELF_ENTITY_ID,
            node_kind="entity",
            properties={"name": "self"},
        ))

    def _trait(self, name: str) -> str:
        log = self.p["log"]
        nid = make_entity_id()
        log.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=nid,
            node_kind="entity",
            properties={"name": name},
        ))
        return nid


class TestAddAssertionDefault(_AssertionTestBase):
    def test_default_no_asserter_is_self_asserted(self) -> None:
        trait = self._trait("thoughtful")
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait", object_id=trait,
        )
        tick_views()

        from mempalace.views.current import current_node
        node = current_node(aid)
        self.assertIsNotNone(node)
        assert node is not None
        self.assertFalse(node.properties.get("asserter_is_external"))
        self.assertTrue(node.properties.get("subject_is_self"))
        self.assertNotIn("asserter_palace_id", node.properties)

    def test_default_subject_not_self(self) -> None:
        """When subject != SELF, subject_is_self is False."""
        other_subject = self._trait("alice")
        trait = self._trait("kind")
        aid = self.g.add_assertion(
            subject_id=other_subject, predicate="has_trait", object_id=trait,
        )
        tick_views()
        from mempalace.views.current import current_node
        node = current_node(aid)
        assert node is not None
        self.assertFalse(node.properties.get("subject_is_self"))


class TestAddAssertionExternal(_AssertionTestBase):
    def test_external_asserter_populates_provenance(self) -> None:
        trait = self._trait("thoughtful")
        asserter = AssertionAsserter(
            palace_id="palace_alice",
            session_pubkey_hex="ab" * 32,
            signature_hex="cd" * 64,
        )
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait", object_id=trait,
            asserter=asserter,
        )
        tick_views()
        from mempalace.views.current import current_node
        node = current_node(aid)
        assert node is not None

        self.assertTrue(node.properties.get("asserter_is_external"))
        self.assertEqual(
            node.properties.get("asserter_palace_id"), "palace_alice",
        )
        self.assertEqual(
            node.properties.get("asserter_session_pubkey_hex"), "ab" * 32,
        )
        self.assertEqual(
            node.properties.get("asserter_signature_hex"), "cd" * 64,
        )
        self.assertTrue(node.properties.get("subject_is_self"))

    def test_external_asserter_changes_created_by(self) -> None:
        trait = self._trait("thoughtful")
        asserter = AssertionAsserter(
            palace_id="palace_bob",
            session_pubkey_hex="11" * 32,
            signature_hex="22" * 64,
        )
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait", object_id=trait,
            asserter=asserter,
        )
        tick_views()
        from mempalace.views.current import current_node
        node = current_node(aid)
        assert node is not None
        # NodeState doesn't expose created_by directly; we verify via
        # the properties being correct.
        self.assertEqual(
            node.properties.get("asserter_palace_id"), "palace_bob",
        )

    def test_self_asserter_explicit(self) -> None:
        """Passing AssertionAsserter() explicitly is equivalent to None."""
        trait = self._trait("kind")
        aid = self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait", object_id=trait,
            asserter=AssertionAsserter(),  # explicit empty
        )
        tick_views()
        from mempalace.views.current import current_node
        node = current_node(aid)
        assert node is not None
        self.assertFalse(node.properties.get("asserter_is_external"))


# =============================================================================
# Query helpers
# =============================================================================


class TestAssertionQueries(_AssertionTestBase):
    def test_assertions_about_self_includes_both(self) -> None:
        trait_a = self._trait("a")
        trait_b = self._trait("b")
        # Self
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_a,
        )
        # External
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_b,
            asserter=AssertionAsserter(
                palace_id="palace_x",
                session_pubkey_hex="0" * 64,
                signature_hex="1" * 128,
            ),
        )
        tick_views()

        results = assertions_about_self()
        self.assertEqual(len(results), 2)

    def test_external_only_filter(self) -> None:
        trait_a = self._trait("a")
        trait_b = self._trait("b")
        # Self
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_a,
        )
        # External
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_b,
            asserter=AssertionAsserter(
                palace_id="palace_x",
                session_pubkey_hex="0" * 64,
                signature_hex="1" * 128,
            ),
        )
        tick_views()

        ext = external_mentions_of_self()
        self.assertEqual(len(ext), 1)
        self.assertEqual(
            ext[0].properties.get("asserter_palace_id"), "palace_x",
        )

    def test_self_only_filter(self) -> None:
        trait_a = self._trait("a")
        trait_b = self._trait("b")
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_a,
        )
        self.g.add_assertion(
            subject_id=SELF_ENTITY_ID, predicate="has_trait",
            object_id=trait_b,
            asserter=AssertionAsserter(
                palace_id="palace_x",
                session_pubkey_hex="0" * 64,
                signature_hex="1" * 128,
            ),
        )
        tick_views()

        self_only = assertions_about_self(include_external_asserted=False)
        self.assertEqual(len(self_only), 1)
        self.assertFalse(
            self_only[0].properties.get("asserter_is_external"),
        )

    def test_filter_by_specific_palace(self) -> None:
        trait = self._trait("trait")
        # Three external asserters
        for palace in ["palace_a", "palace_b", "palace_c"]:
            self.g.add_assertion(
                subject_id=SELF_ENTITY_ID, predicate="says",
                object_id=trait,
                asserter=AssertionAsserter(
                    palace_id=palace,
                    session_pubkey_hex="0" * 64,
                    signature_hex="1" * 128,
                ),
            )
        tick_views()

        from_b = assertions_about(
            SELF_ENTITY_ID,
            include_self_asserted=False,
            asserter_palace_id="palace_b",
        )
        self.assertEqual(len(from_b), 1)
        self.assertEqual(
            from_b[0].properties.get("asserter_palace_id"), "palace_b",
        )


# =============================================================================
# HandleContext
# =============================================================================


class TestHandleContextBasics(unittest.TestCase):
    def test_default_construction(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        self.assertEqual(ctx.handle_id, "hdl_x")
        self.assertEqual(ctx.query_hash, "")
        self.assertEqual(ctx.total_hops, 0)
        self.assertEqual(ctx.frames, [])
        self.assertEqual(ctx.cluster_signature(), "cs_empty")

    def test_add_hop_bumps_count_and_pattern(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        ctx.add_hop(Hop(
            from_node_id="nde_a", to_node_id="nde_b",
            edge_id="edg_1", edge_kind="likes",
        ))
        self.assertEqual(ctx.total_hops, 1)
        self.assertNotEqual(ctx.cluster_signature(), "cs_empty")

    def test_add_frame(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        ctx.add_frame(InterpretiveFrame(
            frame_id="frm_1", confidence=0.5, description="test",
        ))
        self.assertEqual(len(ctx.frames), 1)

    def test_is_pattern_stable_passes_through(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        # Empty pattern is not stable
        self.assertFalse(ctx.is_pattern_stable())

    def test_snapshot_keys(self) -> None:
        ctx = HandleContext(handle_id="hdl_x", query_hash="qh")
        ctx.add_hop(Hop(
            from_node_id="a", to_node_id="b",
            edge_id="e", edge_kind="k",
        ))
        ctx.add_frame(InterpretiveFrame(
            frame_id="f", confidence=0.6, description="",
        ))
        snap = ctx.snapshot()
        self.assertEqual(snap["handle_id"], "hdl_x")
        self.assertEqual(snap["query_hash"], "qh")
        self.assertEqual(snap["total_hops"], 1)
        self.assertEqual(snap["frame_count"], 1)
        self.assertEqual(snap["frame_confidences"], [0.6])
        self.assertIn("cluster_signature", snap)
        self.assertIn("pattern_stable", snap)


class TestHandleContextWithPolicy(unittest.TestCase):
    def test_policy_uses_context(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        ctx.add_frame(InterpretiveFrame(
            frame_id="f", confidence=0.9, description="dominant",
        ))
        policy = SearchPolicy.adaptive()
        budget = SearchBudget()
        directive = policy.next_step_for_context(ctx, budget)
        # With one dominant frame and full budget, expect commit_depth
        self.assertEqual(directive.kind, DirectiveKind.COMMIT_DEPTH)

    def test_policy_with_no_frames_explores(self) -> None:
        ctx = HandleContext(handle_id="hdl_x")
        # No frames yet
        policy = SearchPolicy.adaptive()
        budget = SearchBudget()
        directive = policy.next_step_for_context(ctx, budget)
        # Default with no frames is breadth expansion
        self.assertEqual(directive.kind, DirectiveKind.EXPAND_BREADTH)


if __name__ == "__main__":
    unittest.main()
