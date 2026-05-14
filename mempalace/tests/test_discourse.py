"""
Tests for R3 §9.5 — discourse-pattern extraction and comparison.

Coverage:
  - extract_discourse_pattern on isolated nodes (no edges)
  - Refinement chain walking (depth, cycle-safety, cap)
  - Contradiction-resolution ratio computation
  - Support/oppose balance computation
  - compare_discourse_patterns: identical / disjoint / partial
"""

from __future__ import annotations

import unittest

from mempalace.federate.discourse import (
    MAX_REFINEMENT_CHAIN_LENGTH,
    DiscoursePattern,
    compare_discourse_patterns,
    extract_discourse_pattern,
)
from mempalace.schema.events import EdgeCreated, NodeCreated
from mempalace.schema.identifiers import (
    make_event_id_log,
    make_edge_id,
    make_entity_id,
)
from mempalace.schema.kinds import DerivationType, EdgeKind
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.current import tick_views


def _create_node(log, node_id: str | None = None,
                 kind: str = "entity") -> str:
    nid = node_id or make_entity_id()
    log.append(NodeCreated(
        event_id=make_event_id_log(), recorded_at=1, actor="t",
        node_id=nid, node_kind=kind, properties={},
    ))
    return nid


def _create_edge(log, source: str, target: str, kind: EdgeKind) -> str:
    eid = make_edge_id()
    log.append(EdgeCreated(
        event_id=make_event_id_log(), recorded_at=1, actor="t",
        edge_id=eid, edge_kind=kind.value,
        source_node_id=source, target_node_id=target,
        derivation=DerivationType.OBSERVATION.value,
    ))
    return eid


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestIsolatedNode(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.log = self.p["log"]

    def test_node_with_no_edges_has_no_discourse(self) -> None:
        nid = _create_node(self.log)
        tick_views()
        pattern = extract_discourse_pattern(nid)
        self.assertEqual(pattern.total_discourse_edges, 0)
        self.assertFalse(pattern.has_discourse)
        self.assertEqual(pattern.refinement_chain_count, 0)


class TestRefinementChain(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.log = self.p["log"]

    def test_simple_chain_depth(self) -> None:
        # a -refines-> b -refines-> c -refines-> d
        a = _create_node(self.log)
        b = _create_node(self.log)
        c = _create_node(self.log)
        d = _create_node(self.log)
        _create_edge(self.log, a, b, EdgeKind.REFINES)
        _create_edge(self.log, b, c, EdgeKind.REFINES)
        _create_edge(self.log, c, d, EdgeKind.REFINES)
        tick_views()

        pa = extract_discourse_pattern(a)
        # chain depth from a: 3 (a→b→c→d)
        self.assertEqual(pa.refinement_chain_count, 1)
        self.assertEqual(pa.refinement_chain_lengths, (3,))

    def test_cycle_does_not_loop_forever(self) -> None:
        # a -refines-> b -refines-> a (cycle)
        a = _create_node(self.log)
        b = _create_node(self.log)
        _create_edge(self.log, a, b, EdgeKind.REFINES)
        _create_edge(self.log, b, a, EdgeKind.REFINES)
        tick_views()

        pa = extract_discourse_pattern(a)
        # The walker stops when it revisits a seen node, so depth is
        # bounded — should not exceed MAX_REFINEMENT_CHAIN_LENGTH and
        # should be a small specific value.
        self.assertLess(pa.refinement_chain_lengths[0],
                        MAX_REFINEMENT_CHAIN_LENGTH)

    def test_incoming_refines_counted(self) -> None:
        # leaf <-refines- predecessor
        leaf = _create_node(self.log)
        pred = _create_node(self.log)
        _create_edge(self.log, pred, leaf, EdgeKind.REFINES)
        tick_views()

        p = extract_discourse_pattern(leaf)
        # leaf has 1 incoming REFINES → chain count = 1
        self.assertEqual(p.refinement_chain_count, 1)


class TestContradictionResolution(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.log = self.p["log"]

    def test_unresolved_contradictions_low_ratio(self) -> None:
        a = _create_node(self.log)
        b = _create_node(self.log)
        c = _create_node(self.log)
        # a contradicts b, c — 2 contradictions, 0 supersedes
        _create_edge(self.log, a, b, EdgeKind.CONTRADICTS)
        _create_edge(self.log, a, c, EdgeKind.CONTRADICTS)
        tick_views()

        pa = extract_discourse_pattern(a)
        self.assertEqual(pa.contradiction_count, 2)
        self.assertEqual(pa.supersedes_count, 0)
        self.assertEqual(pa.contradiction_resolution_ratio, 0.0)

    def test_resolved_contradictions_high_ratio(self) -> None:
        a = _create_node(self.log)
        b = _create_node(self.log)
        # a contradicts b, AND a supersedes b — resolved
        _create_edge(self.log, a, b, EdgeKind.CONTRADICTS)
        _create_edge(self.log, a, b, EdgeKind.SUPERSEDES)
        tick_views()

        pa = extract_discourse_pattern(a)
        self.assertEqual(pa.contradiction_resolution_ratio, 1.0)


class TestSupportOpposeBalance(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()
        self.log = self.p["log"]

    def test_pure_support_positive_balance(self) -> None:
        a = _create_node(self.log)
        b = _create_node(self.log)
        c = _create_node(self.log)
        _create_edge(self.log, a, b, EdgeKind.SUPPORTS)
        _create_edge(self.log, a, c, EdgeKind.SUPPORTS)
        tick_views()

        pa = extract_discourse_pattern(a)
        self.assertEqual(pa.supports_count, 2)
        self.assertEqual(pa.support_vs_oppose_balance, 1.0)

    def test_pure_inhibit_negative_balance(self) -> None:
        a = _create_node(self.log)
        b = _create_node(self.log)
        _create_edge(self.log, a, b, EdgeKind.INHIBITS)
        tick_views()

        pa = extract_discourse_pattern(a)
        self.assertEqual(pa.inhibits_count, 1)
        self.assertEqual(pa.support_vs_oppose_balance, -1.0)

    def test_balanced_support_oppose(self) -> None:
        a = _create_node(self.log)
        b = _create_node(self.log)
        c = _create_node(self.log)
        _create_edge(self.log, a, b, EdgeKind.SUPPORTS)
        _create_edge(self.log, a, c, EdgeKind.INHIBITS)
        tick_views()

        pa = extract_discourse_pattern(a)
        self.assertEqual(pa.support_vs_oppose_balance, 0.0)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class TestCompareDiscoursePatterns(unittest.TestCase):
    def test_identical_patterns_score_one(self) -> None:
        p = DiscoursePattern(
            node_id="x",
            refinement_chain_count=2,
            refinement_chain_lengths=(2, 4),
            contradiction_count=3,
            supersedes_count=2,
            contradiction_resolution_ratio=2 / 3,
            supports_count=4,
            inhibits_count=1,
            support_vs_oppose_balance=0.6,
            total_discourse_edges=12,
        )
        scores = compare_discourse_patterns(p, p)
        self.assertEqual(scores["refinement_similarity"], 1.0)
        self.assertAlmostEqual(scores["contradiction_similarity"], 1.0)
        self.assertEqual(scores["support_similarity"], 1.0)
        self.assertEqual(scores["aggregate"], 1.0)

    def test_disjoint_patterns_score_low(self) -> None:
        local = DiscoursePattern(
            node_id="a",
            refinement_chain_lengths=(2, 3),  # short/medium chains
            contradiction_count=5, supersedes_count=0,  # unresolved
            contradiction_resolution_ratio=0.0,
            inhibits_count=10,
            support_vs_oppose_balance=-1.0,
        )
        remote = DiscoursePattern(
            node_id="b",
            refinement_chain_lengths=(),  # no chains
            contradiction_count=0, supersedes_count=0,  # no contradictions
            contradiction_resolution_ratio=0.0,
            supports_count=10,
            support_vs_oppose_balance=1.0,
        )
        scores = compare_discourse_patterns(local, remote)
        # Highly different: low aggregate
        self.assertLess(scores["aggregate"], 0.5)
        # Support balance: -1 vs +1 → similarity 0
        self.assertEqual(scores["support_similarity"], 0.0)

    def test_partial_match(self) -> None:
        local = DiscoursePattern(
            node_id="a",
            contradiction_count=4, supersedes_count=2,
            contradiction_resolution_ratio=0.5,
            support_vs_oppose_balance=0.5,
        )
        remote = DiscoursePattern(
            node_id="b",
            contradiction_count=4, supersedes_count=2,
            contradiction_resolution_ratio=0.5,
            support_vs_oppose_balance=-0.5,  # opposite balance
        )
        scores = compare_discourse_patterns(local, remote)
        # Contradictions match perfectly
        self.assertEqual(scores["contradiction_similarity"], 1.0)
        # Support balance differs by 1.0 out of max 2 → 0.5 similarity
        self.assertEqual(scores["support_similarity"], 0.5)


if __name__ == "__main__":
    unittest.main()
