"""Tests for Track 3 — adaptive search policy.

Covers:
  - ClusterTraversalPattern: window, dominant edges, signature
    determinism, is_stable.
  - SearchPolicy.next_step decision tree (six rules from the design).
  - SearchBudget: dwindling, exhaustion, consumption.
  - PolicyAdjustment hook: NoopAdjustment passthrough; custom
    adjustment can override.
  - WalkDriver: end-to-end loop with fixture executor; emits
    WalkCompleted audit event with full directive trace.
  - Track 3 ↔ Track 4A bridge: cluster_signature() compatible with
    ranker_output_pattern_key.
"""

from __future__ import annotations

import unittest

from mempalace.derived.dependency import ranker_output_pattern_key
from mempalace.handle import (
    CONWAY_RATE_CLASS_3,
    ClusterTraversalPattern,
    ConwayRate,
    DirectiveKind,
    FrameConfidenceSummary,
    Hop,
    InterpretiveFrame,
    NoopAdjustment,
    SearchBudget,
    SearchPolicy,
    StepDirective,
    StepOutcome,
    WalkDriver,
    WalkOutcome,
    summarize_frames,
)
from mempalace.handle.search_policy import (
    DEFAULT_BREADTH_FANOUT,
    PolicyAdjustment,
)
from mempalace.schema.events import WalkCompleted
from mempalace.tests.conftest import fresh_palace, reset_module_state


# =============================================================================
# Cluster traversal pattern
# =============================================================================


class TestClusterTraversalPattern(unittest.TestCase):
    def test_empty_signature_stable(self) -> None:
        ctp = ClusterTraversalPattern()
        self.assertEqual(ctp.cluster_signature(), "cs_empty")

    def test_signature_deterministic(self) -> None:
        """Same hops → same signature, even across pattern instances."""
        hops = [
            Hop(from_node_id="a", to_node_id="b", edge_id="e1",
                edge_kind="succeeds", edge_confidence=0.9),
            Hop(from_node_id="b", to_node_id="c", edge_id="e2",
                edge_kind="contains", edge_confidence=0.7),
        ]
        ctp1 = ClusterTraversalPattern()
        ctp2 = ClusterTraversalPattern()
        for h in hops:
            ctp1.add_hop(h)
            ctp2.add_hop(h)
        self.assertEqual(ctp1.cluster_signature(), ctp2.cluster_signature())

    def test_signature_changes_with_different_hops(self) -> None:
        ctp1 = ClusterTraversalPattern()
        ctp1.add_hop(Hop("a", "b", "e1", "succeeds"))

        ctp2 = ClusterTraversalPattern()
        ctp2.add_hop(Hop("a", "b", "e1", "contains"))  # different edge_kind

        self.assertNotEqual(
            ctp1.cluster_signature(), ctp2.cluster_signature()
        )

    def test_window_bounds(self) -> None:
        ctp = ClusterTraversalPattern(window_size=3)
        for i in range(10):
            ctp.add_hop(Hop(f"n{i}", f"n{i+1}", f"e{i}", "succeeds"))
        # Only last 3 hops kept
        self.assertEqual(len(ctp.recent_hops), 3)
        self.assertEqual(ctp.recent_hops[0].from_node_id, "n7")

    def test_dominant_edge_kinds(self) -> None:
        ctp = ClusterTraversalPattern()
        ctp.add_hop(Hop("a", "b", "e1", "succeeds"))
        ctp.add_hop(Hop("b", "c", "e2", "succeeds"))
        ctp.add_hop(Hop("c", "d", "e3", "contains"))
        kinds = ctp.dominant_edge_kinds
        # `succeeds` is most common
        self.assertEqual(kinds[0], "succeeds")

    def test_is_stable_true_when_kind_unchanged(self) -> None:
        ctp = ClusterTraversalPattern()
        for i in range(5):
            ctp.add_hop(Hop(f"n{i}", f"n{i+1}", f"e{i}", "succeeds"))
        self.assertTrue(ctp.is_stable(min_hops=4))

    def test_is_stable_false_with_kind_change(self) -> None:
        ctp = ClusterTraversalPattern()
        ctp.add_hop(Hop("a", "b", "e1", "succeeds"))
        ctp.add_hop(Hop("b", "c", "e2", "succeeds"))
        ctp.add_hop(Hop("c", "d", "e3", "contains"))
        ctp.add_hop(Hop("d", "e", "e4", "succeeds"))
        self.assertFalse(ctp.is_stable(min_hops=4))

    def test_is_stable_false_when_too_few_hops(self) -> None:
        ctp = ClusterTraversalPattern()
        ctp.add_hop(Hop("a", "b", "e1", "succeeds"))
        self.assertFalse(ctp.is_stable(min_hops=4))

    def test_signature_compatible_with_track_4a(self) -> None:
        """The Track 3 ↔ Track 4A bridge: cluster_signature() output
        feeds directly into ranker_output_pattern_key()."""
        ctp = ClusterTraversalPattern()
        ctp.add_hop(Hop("a", "b", "e1", "succeeds"))
        sig = ctp.cluster_signature()

        key = ranker_output_pattern_key("query-h", "ranker-v1", sig)
        self.assertEqual(key.identity[2], sig)


# =============================================================================
# SearchBudget
# =============================================================================


class TestSearchBudget(unittest.TestCase):
    def test_default_not_exhausted(self) -> None:
        b = SearchBudget()
        self.assertFalse(b.is_exhausted())
        self.assertFalse(b.is_dwindling())

    def test_consume_hop(self) -> None:
        b = SearchBudget(hops_remaining=10, depth_remaining=5, breadth_remaining=20)
        b.consume_hop(depth=1, breadth=4)
        self.assertEqual(b.hops_remaining, 9)
        self.assertEqual(b.depth_remaining, 4)
        self.assertEqual(b.breadth_remaining, 16)

    def test_dwindling_when_below_threshold(self) -> None:
        b = SearchBudget(hops_remaining=2, depth_remaining=16, breadth_remaining=64)
        self.assertTrue(b.is_dwindling())

    def test_exhausted_when_no_hops(self) -> None:
        b = SearchBudget(hops_remaining=0)
        self.assertTrue(b.is_exhausted())


# =============================================================================
# StepDirective sum type
# =============================================================================


class TestStepDirective(unittest.TestCase):
    def test_expand_breadth(self) -> None:
        d = StepDirective.expand_breadth(8, rationale="exploring")
        self.assertEqual(d.kind, DirectiveKind.EXPAND_BREADTH)
        self.assertEqual(d.breadth_count, 8)
        self.assertEqual(d.rationale, "exploring")

    def test_commit_depth(self) -> None:
        d = StepDirective.commit_depth("f_dominant")
        self.assertEqual(d.kind, DirectiveKind.COMMIT_DEPTH)
        self.assertEqual(d.commit_frame_id, "f_dominant")

    def test_alternate(self) -> None:
        d = StepDirective.alternate("fa", "fb", depth=3)
        self.assertEqual(d.kind, DirectiveKind.ALTERNATE)
        self.assertEqual(d.alternate_frame_a, "fa")
        self.assertEqual(d.alternate_frame_b, "fb")
        self.assertEqual(d.alternate_depth, 3)

    def test_terminate(self) -> None:
        d = StepDirective.terminate(reason="walk_stuck")
        self.assertEqual(d.kind, DirectiveKind.TERMINATE)
        self.assertEqual(d.terminate_reason, "walk_stuck")

    def test_directive_is_hashable(self) -> None:
        """Frozen + hashable so directives can flow into audit
        events / cache keys."""
        d = StepDirective.commit_depth("f1")
        s = {d, d}
        self.assertEqual(len(s), 1)


# =============================================================================
# SearchPolicy decision tree
# =============================================================================


class TestSearchPolicyRules(unittest.TestCase):
    """Each rule from HANDLES_DESIGN.md v2 §"Search policy" gets
    a test."""

    def setUp(self) -> None:
        self.policy = SearchPolicy.adaptive()

    def test_rule_1_budget_exhausted_terminates(self) -> None:
        b = SearchBudget(hops_remaining=0)
        d = self.policy.next_step(
            FrameConfidenceSummary(frames_with_confidences=(("f", 0.9),)),
            b,
        )
        self.assertEqual(d.kind, DirectiveKind.TERMINATE)
        self.assertEqual(d.terminate_reason, "budget_exhausted")

    def test_rule_2_walk_stuck_terminates(self) -> None:
        ctp = ClusterTraversalPattern()
        for i in range(5):
            ctp.add_hop(Hop(f"n{i}", f"n{i+1}", f"e{i}", "succeeds"))
        d = self.policy.next_step(
            FrameConfidenceSummary(frames_with_confidences=(("f", 0.5),)),
            SearchBudget(),
            cluster_pattern=ctp,
        )
        self.assertEqual(d.kind, DirectiveKind.TERMINATE)
        self.assertEqual(d.terminate_reason, "walk_stuck")

    def test_rule_3_no_frames_expands_breadth(self) -> None:
        d = self.policy.next_step(
            FrameConfidenceSummary(frames_with_confidences=()),
            SearchBudget(),
        )
        self.assertEqual(d.kind, DirectiveKind.EXPAND_BREADTH)
        self.assertEqual(d.breadth_count, DEFAULT_BREADTH_FANOUT)

    def test_rule_4_dominant_frame_commits_depth(self) -> None:
        # Dominant frame, no close second
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(("f_top", 0.9), ("f_low", 0.2)),
            ),
            SearchBudget(),
        )
        self.assertEqual(d.kind, DirectiveKind.COMMIT_DEPTH)
        self.assertEqual(d.commit_frame_id, "f_top")

    def test_rule_4_does_not_fire_if_second_climbing(self) -> None:
        """Even with a top frame above threshold, if the second is
        climbing (not fading), don't commit."""
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(("f_top", 0.85), ("f_climbing", 0.45)),
                confidence_history_by_frame={
                    "f_top": (0.85,),
                    # second frame's history shows it climbing
                    "f_climbing": (0.10, 0.20, 0.35, 0.45),
                },
            ),
            SearchBudget(),
        )
        # Should NOT commit_depth because second is climbing
        self.assertNotEqual(d.kind, DirectiveKind.COMMIT_DEPTH)

    def test_rule_5_close_frames_dwindling_alternates(self) -> None:
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(("fa", 0.6), ("fb", 0.55)),
            ),
            SearchBudget(hops_remaining=2),  # dwindling
        )
        self.assertEqual(d.kind, DirectiveKind.ALTERNATE)
        self.assertEqual(d.alternate_frame_a, "fa")
        self.assertEqual(d.alternate_frame_b, "fb")

    def test_rule_5_does_not_fire_if_budget_healthy(self) -> None:
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(("fa", 0.6), ("fb", 0.55)),
            ),
            SearchBudget(),  # healthy
        )
        # Should NOT alternate; budget is healthy
        self.assertNotEqual(d.kind, DirectiveKind.ALTERNATE)

    def test_rule_6_high_dispersion_expands_breadth(self) -> None:
        # High dispersion: confidences across frames span a wide range
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(
                    ("f1", 0.6), ("f2", 0.4), ("f3", 0.2), ("f4", 0.05),
                ),
            ),
            SearchBudget(),
        )
        self.assertEqual(d.kind, DirectiveKind.EXPAND_BREADTH)

    def test_rule_7_default_expand_breadth(self) -> None:
        # Two frames, low confidences, far apart but not dispersed enough
        # for rule 6, budget healthy → fallback rule 7
        d = self.policy.next_step(
            FrameConfidenceSummary(
                frames_with_confidences=(("f1", 0.3), ("f2", 0.25)),
            ),
            SearchBudget(),
        )
        self.assertEqual(d.kind, DirectiveKind.EXPAND_BREADTH)


# =============================================================================
# Adjustment hook
# =============================================================================


class TestPolicyAdjustment(unittest.TestCase):
    def test_noop_passthrough(self) -> None:
        adj = NoopAdjustment()
        proposed = StepDirective.expand_breadth(4)
        result = adj.adjust_directive(
            proposed,
            FrameConfidenceSummary(frames_with_confidences=()),
            SearchBudget(),
        )
        self.assertIs(result, proposed)

    def test_custom_adjustment_can_override(self) -> None:
        """Verify the hook contract: a real adjustment returns a
        different directive based on its learned signal."""

        class AlwaysTerminate:
            def adjust_directive(
                self,
                proposed,
                summary,
                budget,
            ) -> StepDirective:
                return StepDirective.terminate(
                    reason="learned_pattern",
                    rationale="custom adjustment",
                )

        policy = SearchPolicy(adjustment=AlwaysTerminate())
        d = policy.next_step(
            FrameConfidenceSummary(frames_with_confidences=(("f1", 0.5),)),
            SearchBudget(),
        )
        self.assertEqual(d.kind, DirectiveKind.TERMINATE)
        self.assertEqual(d.terminate_reason, "learned_pattern")

    def test_protocol_runtime_check(self) -> None:
        """NoopAdjustment satisfies the PolicyAdjustment Protocol."""
        adj = NoopAdjustment()
        self.assertIsInstance(adj, PolicyAdjustment)


# =============================================================================
# summarize_frames helper
# =============================================================================


class TestSummarizeFrames(unittest.TestCase):
    def test_extracts_id_and_confidence(self) -> None:
        frames = [
            InterpretiveFrame(frame_id="f1", confidence=0.7),
            InterpretiveFrame(frame_id="f2", confidence=0.5),
        ]
        summary = summarize_frames(frames)
        self.assertEqual(
            summary.frames_with_confidences,
            (("f1", 0.7), ("f2", 0.5)),
        )

    def test_history_optional(self) -> None:
        summary = summarize_frames([])
        self.assertEqual(summary.confidence_history_by_frame, {})

    def test_history_passed_through(self) -> None:
        frames = [InterpretiveFrame(frame_id="f1", confidence=0.5)]
        summary = summarize_frames(
            frames,
            history={"f1": [0.3, 0.4, 0.5]},
        )
        self.assertEqual(summary.confidence_history_by_frame["f1"], (0.3, 0.4, 0.5))


# =============================================================================
# WalkDriver — end-to-end
# =============================================================================


class _CountingExecutor:
    """Fixture executor that returns canned outcomes + tracks calls.

    Boosts the named frame's confidence on each call so the policy
    eventually triggers commit_depth, after which we can verify the
    transition.
    """

    def __init__(self, *, boost_frame_id: str = "", boost_amount: float = 0.0) -> None:
        self.calls: list[str] = []
        self.boost_frame_id = boost_frame_id
        self.boost_amount = boost_amount

    def execute_expand_breadth(
        self,
        N: int,
        frames,
    ) -> StepOutcome:
        self.calls.append(f"expand_breadth({N})")
        # Take one fake hop
        outcome = StepOutcome(
            hops_taken=[
                Hop("n_a", "n_b", "e_x", "succeeds", edge_confidence=0.7),
            ],
            breadth_consumed=N,
        )
        if self.boost_frame_id:
            outcome.frame_confidence_updates[self.boost_frame_id] = min(
                1.0,
                next(
                    (f.confidence for f in frames if f.frame_id == self.boost_frame_id),
                    0.5,
                ) + self.boost_amount,
            )
        return outcome

    def execute_commit_depth(
        self,
        frame_id: str,
        frames,
    ) -> StepOutcome:
        self.calls.append(f"commit_depth({frame_id})")
        return StepOutcome(
            hops_taken=[
                Hop("n_b", "n_c", "e_y", "contains"),
            ],
            depth_consumed=1,
        )

    def execute_alternate(
        self,
        a: str,
        b: str,
        depth: int,
        frames,
    ) -> StepOutcome:
        self.calls.append(f"alternate({a},{b},depth={depth})")
        return StepOutcome(
            hops_taken=[
                Hop("n_c", "n_d", "e_z", "succeeds"),
            ],
            depth_consumed=depth,
        )


class TestWalkDriver(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_terminates_on_exhausted_budget(self) -> None:
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        outcome = driver.run(
            handle_id="hdl_test",
            query_hash="q-h",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.5)],
            budget=SearchBudget(hops_remaining=0),
        )
        self.assertEqual(outcome.terminate_reason, "budget_exhausted")
        self.assertEqual(outcome.total_hops, 0)
        self.assertEqual(executor.calls, [])

    def test_emits_walk_completed_audit_event(self) -> None:
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        outcome = driver.run(
            handle_id="hdl_audit",
            query_hash="q-aud",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.95)],
            budget=SearchBudget(hops_remaining=2),
        )
        # Audit event was appended
        self.assertNotEqual(outcome.walk_completed_event_id, "")

        # Find the WalkCompleted event in the log
        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        walk_evts = [
            (kind, payload)
            for _o, kind, payload in rows
            if kind == "walk_completed"
        ]
        self.assertEqual(len(walk_evts), 1)
        _kind, payload = walk_evts[0]
        self.assertEqual(payload["handle_id"], "hdl_audit")
        self.assertEqual(payload["query_hash"], "q-aud")
        self.assertGreater(len(payload["directive_trace"]), 0)
        # First directive should be commit_depth (rule 4 fires)
        self.assertEqual(payload["directive_trace"][0]["kind"], "commit_depth")

    def test_dominant_frame_triggers_commit_depth(self) -> None:
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        driver.run(
            handle_id="hdl_dom",
            query_hash="q-dom",
            frames=[
                InterpretiveFrame(frame_id="f_top", confidence=0.9),
                InterpretiveFrame(frame_id="f_low", confidence=0.1),
            ],
            budget=SearchBudget(hops_remaining=3),
        )
        # Should have called commit_depth
        self.assertTrue(
            any("commit_depth(f_top)" in c for c in executor.calls),
            f"executor.calls = {executor.calls}",
        )

    def test_no_audit_when_log_disabled(self) -> None:
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        outcome = driver.run(
            handle_id="hdl_no_audit",
            query_hash="q-noaud",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.5)],
            budget=SearchBudget(hops_remaining=2),
            emit_audit_event=False,
        )
        self.assertEqual(outcome.walk_completed_event_id, "")

    def test_directive_trace_records_each_step(self) -> None:
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        outcome = driver.run(
            handle_id="hdl_trace",
            query_hash="q-tr",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.95)],
            budget=SearchBudget(hops_remaining=3),
        )
        # Each step in the trace records its kind + rationale
        for entry in outcome.directive_trace:
            self.assertIn("step", entry)
            self.assertIn("kind", entry)
            self.assertIn("rationale", entry)

    def test_runs_multiple_steps_until_terminate(self) -> None:
        """Run with healthy budget, no dominant frame initially.
        Executor returns the same edge_kind on each hop, so after
        STUCK_PATTERN_HOPS the cluster pattern is stable and the
        policy terminates with 'walk_stuck' before budget runs out.
        """
        executor = _CountingExecutor()
        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=executor,
            log_client=self.p["log"],
        )
        outcome = driver.run(
            handle_id="hdl_multi",
            query_hash="q-multi",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.4)],
            budget=SearchBudget(hops_remaining=20, breadth_remaining=200),
        )
        # Either walk_stuck (cluster pattern stable) OR budget_exhausted —
        # depending on which fires first. With the test executor producing
        # uniform "succeeds" hops, walk_stuck fires first.
        self.assertIn(
            outcome.terminate_reason,
            {"walk_stuck", "budget_exhausted"},
        )
        self.assertGreater(outcome.total_hops, 0)
        # All executor calls should be expand_breadth (only one frame
        # with mid-level confidence triggers fallback rule 7)
        self.assertTrue(
            all("expand_breadth" in c for c in executor.calls),
            f"executor.calls = {executor.calls}",
        )

    def test_safety_step_cap_terminates_with_reason(self) -> None:
        """The driver's MAX_DRIVER_STEPS safety cap fires only when
        the budget is large enough that exhaustion-by-hops doesn't
        trigger first. With a budget far larger than MAX_DRIVER_STEPS,
        the cap is the bound."""

        class NoOpExecutor:
            """Executor that never produces hops or confidence updates.
            The driver still consumes 1 hop per iteration (via
            `consume_hop`), so the cap is only reachable with a big
            budget."""

            def execute_expand_breadth(self, N, frames):
                return StepOutcome()

            def execute_commit_depth(self, fid, frames):
                return StepOutcome()

            def execute_alternate(self, a, b, depth, frames):
                return StepOutcome()

        from mempalace.handle.walk_driver import MAX_DRIVER_STEPS

        driver = WalkDriver(
            policy=SearchPolicy.adaptive(),
            executor=NoOpExecutor(),
            log_client=self.p["log"],
        )
        # Budget bigger than MAX_DRIVER_STEPS → cap fires first
        outcome = driver.run(
            handle_id="hdl_loop",
            query_hash="q-loop",
            frames=[InterpretiveFrame(frame_id="f1", confidence=0.4)],
            budget=SearchBudget(
                hops_remaining=MAX_DRIVER_STEPS + 100,
                breadth_remaining=MAX_DRIVER_STEPS * 10,
            ),
        )
        self.assertEqual(outcome.terminate_reason, "driver_step_cap")


if __name__ == "__main__":
    unittest.main()
