"""Tests for Phase 4 — dependency tracking over versioned artifacts.

Covers:
  - DependencyKey equality + stable string form
  - Convenience constructors (substrate_field, feature_key, etc.)
  - DependencyTracker: record / clear / dependents / closure
  - Invalidation: direct + transitive
  - RecordingContext for explicit dependency capture
  - End-to-end: a substrate change invalidates exactly the right
    interpretation artifacts
"""

from __future__ import annotations

import unittest

from mempalace.derived.dependency import (
    DependencyKey,
    DependencyKind,
    DependencyTracker,
    RecordingContext,
    canonical_key,
    embedding_key,
    feature_key,
    foyer_render_key,
    get_dependency_tracker,
    proposal_key,
    ranker_output_key,
    set_dependency_tracker,
    signature_key,
    substrate_field,
)
from mempalace.tests.conftest import reset_module_state


class TestDependencyKey(unittest.TestCase):
    def test_keys_with_same_kind_and_identity_equal(self) -> None:
        a = substrate_field("n1", "weight")
        b = substrate_field("n1", "weight")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_keys_with_different_identity_differ(self) -> None:
        a = substrate_field("n1", "weight")
        b = substrate_field("n1", "summary")
        self.assertNotEqual(a, b)

    def test_keys_with_different_kind_differ(self) -> None:
        a = feature_key("n1", "velocity")
        b = embedding_key("n1", "v1")
        self.assertNotEqual(a, b)

    def test_to_str_stable_form(self) -> None:
        s = substrate_field("n1", "weight").to_str()
        self.assertEqual(s, "substrate.node_field:n1:weight")

    def test_constructors_produce_correct_kinds(self) -> None:
        self.assertEqual(
            substrate_field("x", "y").kind,
            DependencyKind.SUBSTRATE_NODE_FIELD,
        )
        self.assertEqual(feature_key("x", "y").kind, DependencyKind.FEATURE)
        self.assertEqual(canonical_key("d", "c").kind, DependencyKind.CANONICAL)
        self.assertEqual(signature_key("p", "s").kind, DependencyKind.SIGNATURE)
        self.assertEqual(proposal_key("p").kind, DependencyKind.PROPOSAL)
        self.assertEqual(ranker_output_key("q", "r").kind, DependencyKind.RANKER_OUTPUT)
        self.assertEqual(
            foyer_render_key("c", "h").kind, DependencyKind.FOYER_RENDER,
        )


class TestDependencyTrackerBasics(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.tracker = DependencyTracker()

    def test_record_and_query(self) -> None:
        artifact = feature_key("n1", "velocity")
        dep = substrate_field("n1", "events_30d")
        self.tracker.record_dependency(artifact, dep)

        self.assertEqual(self.tracker.dependencies_of(artifact), {dep})
        self.assertEqual(self.tracker.dependents_of(dep), {artifact})

    def test_record_dependencies_bulk(self) -> None:
        artifact = signature_key("p1", "s1")
        deps = [
            substrate_field("p1", "drawer_count"),
            feature_key("p1", "velocity"),
            canonical_key("themes", "running"),
        ]
        self.tracker.record_dependencies(artifact, deps)
        self.assertEqual(self.tracker.dependencies_of(artifact), set(deps))
        for d in deps:
            self.assertIn(artifact, self.tracker.dependents_of(d))

    def test_record_idempotent(self) -> None:
        artifact = feature_key("n1", "velocity")
        dep = substrate_field("n1", "events_30d")
        self.tracker.record_dependency(artifact, dep)
        self.tracker.record_dependency(artifact, dep)
        self.assertEqual(len(self.tracker.dependencies_of(artifact)), 1)

    def test_clear_dependencies_removes_both_directions(self) -> None:
        artifact = feature_key("n1", "v")
        dep = substrate_field("n1", "x")
        self.tracker.record_dependency(artifact, dep)

        self.tracker.clear_dependencies(artifact)
        self.assertEqual(self.tracker.dependencies_of(artifact), set())
        self.assertEqual(self.tracker.dependents_of(dep), set())


class TestInvalidation(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.tracker = DependencyTracker()

    def test_direct_invalidation(self) -> None:
        # feature reads substrate
        feat = feature_key("n1", "velocity")
        sub = substrate_field("n1", "events_30d")
        self.tracker.record_dependency(feat, sub)

        # Substrate changes
        report = self.tracker.invalidate(sub)
        self.assertEqual(report.invalidated_keys, [feat])
        self.assertTrue(self.tracker.is_dirty(feat))

    def test_transitive_invalidation(self) -> None:
        # Layered: ranker reads feature reads substrate
        sub = substrate_field("n1", "events_30d")
        feat = feature_key("n1", "velocity")
        rank = ranker_output_key("q1", "factored")
        self.tracker.record_dependency(feat, sub)
        self.tracker.record_dependency(rank, feat)

        # Substrate changes → both feat AND rank invalidate
        report = self.tracker.invalidate(sub)
        self.assertIn(feat, report.invalidated_keys)
        self.assertIn(rank, report.invalidated_keys)
        self.assertTrue(self.tracker.is_dirty(feat))
        self.assertTrue(self.tracker.is_dirty(rank))

    def test_propagate_false_invalidates_only_direct(self) -> None:
        sub = substrate_field("n1", "x")
        feat = feature_key("n1", "v")
        rank = ranker_output_key("q", "r")
        self.tracker.record_dependency(feat, sub)
        self.tracker.record_dependency(rank, feat)

        report = self.tracker.invalidate(sub, propagate=False)
        self.assertEqual(report.invalidated_keys, [feat])
        # rank is NOT dirty (we didn't propagate)
        self.assertFalse(self.tracker.is_dirty(rank))

    def test_invalidation_idempotent_already_dirty(self) -> None:
        sub = substrate_field("n1", "x")
        feat = feature_key("n1", "v")
        self.tracker.record_dependency(feat, sub)

        r1 = self.tracker.invalidate(sub)
        r2 = self.tracker.invalidate(sub)
        # Both reports list feat in r1; r2 finds it already dirty so
        # doesn't re-emit
        self.assertIn(feat, r1.invalidated_keys)
        self.assertNotIn(feat, r2.invalidated_keys)

    def test_unrelated_change_does_not_propagate(self) -> None:
        # The keystone property: a change to a field NOBODY depends on
        # produces zero invalidations.
        feat_a = feature_key("a", "x")
        feat_b = feature_key("b", "x")
        sub_a = substrate_field("a", "field1")
        sub_b = substrate_field("b", "field2")
        sub_c = substrate_field("c", "field3")  # nobody depends on c
        self.tracker.record_dependency(feat_a, sub_a)
        self.tracker.record_dependency(feat_b, sub_b)

        report = self.tracker.invalidate(sub_c)
        self.assertEqual(report.invalidated_keys, [])
        self.assertFalse(self.tracker.is_dirty(feat_a))
        self.assertFalse(self.tracker.is_dirty(feat_b))

    def test_mark_clean_removes_from_dirty(self) -> None:
        feat = feature_key("n1", "v")
        sub = substrate_field("n1", "x")
        self.tracker.record_dependency(feat, sub)
        self.tracker.invalidate(sub)
        self.assertTrue(self.tracker.is_dirty(feat))

        self.tracker.mark_clean(feat)
        self.assertFalse(self.tracker.is_dirty(feat))


class TestClosure(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.tracker = DependencyTracker()

    def test_closure_walks_full_dependency_chain(self) -> None:
        # A → B → C → D
        a = feature_key("a", "x")
        b = feature_key("b", "x")
        c = feature_key("c", "x")
        d = substrate_field("d", "x")
        self.tracker.record_dependency(a, b)
        self.tracker.record_dependency(b, c)
        self.tracker.record_dependency(c, d)

        clos = self.tracker.closure(a)
        self.assertEqual(clos, {b, c, d})

    def test_reverse_closure_walks_dependents(self) -> None:
        a = feature_key("a", "x")
        b = feature_key("b", "x")
        c = feature_key("c", "x")
        d = substrate_field("d", "x")
        self.tracker.record_dependency(a, d)
        self.tracker.record_dependency(b, d)
        self.tracker.record_dependency(c, a)

        rev = self.tracker.reverse_closure(d)
        self.assertEqual(rev, {a, b, c})

    def test_closure_handles_diamond_without_duplicates(self) -> None:
        #     A
        #    / \
        #   B   C
        #    \ /
        #     D
        a = feature_key("a", "x")
        b = feature_key("b", "x")
        c = feature_key("c", "x")
        d = substrate_field("d", "x")
        self.tracker.record_dependency(a, b)
        self.tracker.record_dependency(a, c)
        self.tracker.record_dependency(b, d)
        self.tracker.record_dependency(c, d)

        # Closure visits each node exactly once
        clos = self.tracker.closure(a)
        self.assertEqual(clos, {b, c, d})


class TestRecordingContext(unittest.TestCase):
    def test_collects_reads(self) -> None:
        with RecordingContext() as rc:
            rc.read(substrate_field("n1", "x"))
            rc.read(feature_key("n1", "v"))
        self.assertEqual(len(rc.collected), 2)
        self.assertIn(substrate_field("n1", "x"), rc.collected)
        self.assertIn(feature_key("n1", "v"), rc.collected)

    def test_idempotent_within_context(self) -> None:
        with RecordingContext() as rc:
            rc.read(substrate_field("n1", "x"))
            rc.read(substrate_field("n1", "x"))
        self.assertEqual(len(rc.collected), 1)


class TestSingleton(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        set_dependency_tracker(None)

    def test_get_returns_same_instance(self) -> None:
        a = get_dependency_tracker()
        b = get_dependency_tracker()
        self.assertIs(a, b)

    def test_set_replaces_singleton(self) -> None:
        custom = DependencyTracker()
        set_dependency_tracker(custom)
        self.assertIs(get_dependency_tracker(), custom)


class TestEndToEndPropagation(unittest.TestCase):
    """The keystone Phase 4 property: a substrate change invalidates
    exactly the right artifacts and no more."""

    def setUp(self) -> None:
        reset_module_state()
        self.tracker = DependencyTracker()

    def test_realistic_topology(self) -> None:
        # Realistic chain: drawer events → embedding → feature →
        # signature → finding
        # Plus: an unrelated chain in another node
        sub_n1 = substrate_field("n1", "verbatim")
        emb_n1 = embedding_key("n1", "v1")
        feat_n1 = feature_key("n1", "velocity")
        sig_p1 = signature_key("p1", "s1")
        find_m1 = (lambda match_id: __import__(
            "mempalace.derived.dependency", fromlist=["finding_key"]
        ).finding_key(match_id))("m1")

        self.tracker.record_dependency(emb_n1, sub_n1)
        self.tracker.record_dependency(feat_n1, emb_n1)
        self.tracker.record_dependency(sig_p1, feat_n1)
        self.tracker.record_dependency(find_m1, sig_p1)

        # Unrelated chain on n2
        sub_n2 = substrate_field("n2", "verbatim")
        emb_n2 = embedding_key("n2", "v1")
        feat_n2 = feature_key("n2", "velocity")
        self.tracker.record_dependency(emb_n2, sub_n2)
        self.tracker.record_dependency(feat_n2, emb_n2)

        # Change n1.verbatim — should propagate through the n1 chain
        # but leave the n2 chain alone
        report = self.tracker.invalidate(sub_n1)
        invalidated = set(report.invalidated_keys)
        self.assertIn(emb_n1, invalidated)
        self.assertIn(feat_n1, invalidated)
        self.assertIn(sig_p1, invalidated)
        self.assertIn(find_m1, invalidated)
        # n2 chain untouched
        self.assertNotIn(emb_n2, invalidated)
        self.assertNotIn(feat_n2, invalidated)
        self.assertFalse(self.tracker.is_dirty(emb_n2))
        self.assertFalse(self.tracker.is_dirty(feat_n2))

    def test_no_op_change_invalidates_zero_artifacts(self) -> None:
        """The property the original concern asked about: a change to
        a substrate field that nobody depends on produces zero
        invalidations — provably."""
        # Build a small graph where some fields are tracked
        feat = feature_key("n1", "velocity")
        sub_tracked = substrate_field("n1", "events_30d")
        self.tracker.record_dependency(feat, sub_tracked)

        # Change a sibling field — same node, different field, no one
        # declared a dependency on it
        sub_untracked = substrate_field("n1", "summary")
        report = self.tracker.invalidate(sub_untracked)

        # ZERO invalidations
        self.assertEqual(report.invalidated_keys, [])
        self.assertFalse(self.tracker.is_dirty(feat))


if __name__ == "__main__":
    unittest.main()
