"""Tests for resolve/ — classifier + formula registry + stack."""

from __future__ import annotations

import asyncio
import unittest

from mempalace.tests.conftest import reset_module_state


class TestResolvabilityClassifier(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_public_llm_resolvable(self) -> None:
        from mempalace.resolve import (
            ResolvabilityClass,
            ResolvabilityClassifier,
        )

        clf = ResolvabilityClassifier()
        res = clf.classify("What was the weather in Paris yesterday?")
        self.assertEqual(res.classification, ResolvabilityClass.PUBLIC_LLM_RESOLVABLE)

    def test_privacy_preserving_required(self) -> None:
        from mempalace.resolve import (
            ResolvabilityClass,
            ResolvabilityClassifier,
        )

        clf = ResolvabilityClassifier()
        res = clf.classify("Did I sound happier this week than last week?")
        self.assertEqual(res.classification, ResolvabilityClass.PRIVACY_PRESERVING_REQUIRED)

    def test_jury_only(self) -> None:
        from mempalace.resolve import (
            ResolvabilityClass,
            ResolvabilityClassifier,
        )

        clf = ResolvabilityClassifier()
        res = clf.classify("Should they have apologized?")
        self.assertEqual(res.classification, ResolvabilityClass.JURY_ONLY)

    def test_not_resolvable(self) -> None:
        from mempalace.resolve import (
            ResolvabilityClass,
            ResolvabilityClassifier,
        )

        clf = ResolvabilityClassifier()
        res = clf.classify("Does consciousness emerge from neural patterns?")
        self.assertEqual(res.classification, ResolvabilityClass.NOT_RESOLVABLE)


class TestFormulaRegistry(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_default_evaluators_register_22(self) -> None:
        from mempalace.resolve import (
            get_formula_registry,
            register_default_evaluators,
        )

        reg = get_formula_registry()
        n = register_default_evaluators(reg)
        # 19 base formula types + 3 baseline-market formulas
        self.assertEqual(n, 22)

    def test_evaluate_formula_yes_outcome(self) -> None:
        from mempalace.resolve import (
            EvidenceSummary,
            FormulaType,
            Outcome,
            ResolutionFormula,
            TagCondition,
            evaluate_formula,
        )

        formula = ResolutionFormula(
            formula_id="t1",
            formula_type=FormulaType.TAG_THRESHOLD,
            conditions=[TagCondition(tag_name="X", min_bps=7_000, min_count=1)],
            min_sessions=1, min_devices=1,
        )
        evidence = EvidenceSummary(
            tag_counts={"X": 5}, tag_bps={"X": 8_500},
            session_count=2, device_count=1,
        )
        outcome, bps, _ = evaluate_formula(formula, evidence)
        self.assertEqual(outcome, Outcome.YES)
        self.assertGreaterEqual(bps, 7_000)

    def test_veto_triggers_indeterminate(self) -> None:
        from mempalace.resolve import (
            EvidenceSummary,
            FormulaType,
            Outcome,
            ResolutionFormula,
            TagCondition,
            VetoCondition,
            evaluate_formula,
        )

        formula = ResolutionFormula(
            formula_id="t2",
            formula_type=FormulaType.TAG_THRESHOLD,
            conditions=[TagCondition(tag_name="X", min_bps=7_000, min_count=1)],
            veto_tags=[VetoCondition(tag_name="SPOOFED", max_ratio=0.10)],
        )
        evidence = EvidenceSummary(
            tag_counts={"X": 5}, tag_bps={"X": 9_000},
            tag_ratios={"SPOOFED": 0.30},
            session_count=2, device_count=1,
        )
        outcome, _, reason = evaluate_formula(formula, evidence)
        self.assertEqual(outcome, Outcome.INDETERMINATE)
        self.assertIn("veto", reason.lower())


class TestEncodeRoundtrip(unittest.TestCase):
    def test_encode_decode_roundtrip(self) -> None:
        from mempalace.resolve import (
            ATTESTATION_HASH_SIZE,
            decode_resolution,
            encode_resolution,
        )

        blob = encode_resolution(
            market_id="market_xyz",
            outcome=0,
            confidence_bps=8_500,
            method="deterministic",
            resolver_attestation_hash=b"\x42" * ATTESTATION_HASH_SIZE,
            resolution_at_ms=1_700_000_000_000,
            reason_summary="threshold met",
        )
        decoded = decode_resolution(blob)
        self.assertEqual(decoded.outcome, 0)
        self.assertEqual(decoded.confidence_bps, 8_500)
        self.assertEqual(decoded.method, "deterministic")
        self.assertEqual(decoded.reason_summary, "threshold met")


class TestResolutionStack(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        from mempalace.resolve import (
            get_formula_registry,
            register_default_evaluators,
        )
        register_default_evaluators(get_formula_registry())

    def test_compose_for_audio_formula(self) -> None:
        from mempalace.resolve import (
            FormulaType,
            ResolutionFormula,
            ResolutionStack,
        )
        from mempalace.stack.context import PrivacyMode

        formula = ResolutionFormula(
            formula_id="audio_1",
            formula_type=FormulaType.CONVERSATION,
        )
        plan = ResolutionStack.compose(
            formula, privacy_mode=PrivacyMode.EXTERNAL,
        )
        # Audio formulas pull in transcribe + classify + multimodal
        self.assertIn("transcribe", plan.chosen_inference_keys)
        self.assertIn("classify", plan.chosen_inference_keys)

    def test_privacy_gating_excludes_web_search_in_local_only(self) -> None:
        from mempalace.resolve import (
            FormulaType,
            ResolutionFormula,
            ResolutionStack,
        )
        from mempalace.stack.context import PrivacyMode

        formula = ResolutionFormula(
            formula_id="trend_1",
            formula_type=FormulaType.TREND,
            required_pipeline="web_search+multimodal",
        )
        plan_ext = ResolutionStack.compose(formula, privacy_mode=PrivacyMode.EXTERNAL)
        plan_loc = ResolutionStack.compose(formula, privacy_mode=PrivacyMode.LOCAL_ONLY)
        self.assertIn("web_search", plan_ext.chosen_inference_keys)
        self.assertNotIn("web_search", plan_loc.chosen_inference_keys)


if __name__ == "__main__":
    unittest.main()
