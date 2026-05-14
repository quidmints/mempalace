"""Tests for Track 1B — voice stack stubs + composition.

Covers:
  - Each step's contract (manifest declares right inputs/outputs).
  - End-to-end fixture-driven full_stack execution produces the
    expected per-token + per-segment enrichments.
  - Memo override precedence: a memo on a span overrides
    prosody/affect inference for tokens in that span.
  - Three execution profiles (asr_only, class_1, full) compose
    correctly.
"""

from __future__ import annotations

import asyncio
import unittest

from mempalace.stack.context import PrivacyMode, StackContext
from mempalace.stack.voice import (
    ASRStep,
    AccentDistribution,
    AccentStep,
    AffectDistribution,
    DiarizationStep,
    DrawerSegment,
    ParalinguisticStep,
    ProsodyAffectStep,
    ProsodyVector,
    SpeakerMatchStep,
    TokenFeatures,
    asr_only_stack,
    class_1_stack,
    full_stack,
)


def _run(stack, ctx: StackContext):
    """Run an async Stack.execute synchronously for tests."""
    return asyncio.run(stack.execute(ctx))


# =============================================================================
# Per-step contracts
# =============================================================================


class TestStepManifests(unittest.TestCase):
    """Each step declares its input/output contract explicitly."""

    def test_asr_outputs_tokens(self) -> None:
        m = ASRStep().declares()
        self.assertEqual(m.name, "voice.asr")
        self.assertIn("tokens", m.outputs)
        self.assertTrue(m.requires_attestation)

    def test_diarization_consumes_and_produces_tokens(self) -> None:
        m = DiarizationStep().declares()
        self.assertIn("tokens", m.inputs_required)
        self.assertIn("tokens", m.outputs)

    def test_speaker_match_outputs_voice_match_candidates(self) -> None:
        m = SpeakerMatchStep().declares()
        self.assertIn("tokens", m.inputs_required)
        self.assertIn("voice_match_candidates", m.outputs)

    def test_prosody_affect_consumes_tokens(self) -> None:
        m = ProsodyAffectStep().declares()
        self.assertIn("tokens", m.inputs_required)
        self.assertIn("memo_overrides", m.inputs_optional)

    def test_accent_consumes_segments(self) -> None:
        m = AccentStep().declares()
        self.assertIn("segments", m.inputs_required)

    def test_paralinguistic_outputs_events(self) -> None:
        m = ParalinguisticStep().declares()
        self.assertIn("paralinguistic_events", m.outputs)


# =============================================================================
# Per-step behavior
# =============================================================================


class TestASRStep(unittest.TestCase):
    def test_empty_input_returns_empty_tokens(self) -> None:
        step = ASRStep()
        ctx = StackContext(inputs={}, privacy_mode=PrivacyMode.LOCAL_ONLY)
        result = asyncio.run(step.run(ctx))
        self.assertTrue(result.success)
        self.assertEqual(result.outputs["tokens"], [])

    def test_fixture_transcription_produces_tokens(self) -> None:
        step = ASRStep()
        ctx = StackContext(
            inputs={
                "fixture_transcription": [
                    ("hello", 0, 500),
                    ("world", 500, 1000),
                ]
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = asyncio.run(step.run(ctx))
        tokens = result.outputs["tokens"]
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].token, "hello")
        self.assertEqual(tokens[0].onset_ms, 0)
        self.assertEqual(tokens[1].token, "world")
        # provenance recorded
        self.assertEqual(tokens[0].produced_by_model_pass["tokens"], "stub-asr@v1")


class TestDiarizationStep(unittest.TestCase):
    def test_assigns_default_speaker_when_no_fixture(self) -> None:
        step = DiarizationStep()
        tokens = [
            TokenFeatures(token="a", onset_ms=0, offset_ms=100),
            TokenFeatures(token="b", onset_ms=100, offset_ms=200),
        ]
        ctx = StackContext(
            inputs={"tokens": tokens},
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = asyncio.run(step.run(ctx))
        self.assertTrue(result.success)
        for tk in tokens:
            self.assertEqual(tk.speaker_label, "s0")
            self.assertIsNotNone(tk.speaker_label_confidence)
            self.assertEqual(
                tk.produced_by_model_pass["speaker_label"],
                "stub-diarization@v1",
            )

    def test_fixture_speaker_labels_applied(self) -> None:
        step = DiarizationStep()
        tokens = [
            TokenFeatures(token="x", onset_ms=0, offset_ms=100),
            TokenFeatures(token="y", onset_ms=100, offset_ms=200),
            TokenFeatures(token="z", onset_ms=200, offset_ms=300),
        ]
        ctx = StackContext(
            inputs={
                "tokens": tokens,
                "fixture_speaker_labels": {
                    0: ("s0", 0.95),
                    100: ("s1", 0.85),
                    200: ("s0", 0.92),
                },
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        asyncio.run(step.run(ctx))
        self.assertEqual(tokens[0].speaker_label, "s0")
        self.assertEqual(tokens[1].speaker_label, "s1")
        self.assertEqual(tokens[2].speaker_label, "s0")


class TestSpeakerMatchStep(unittest.TestCase):
    def test_matches_via_fixture(self) -> None:
        step = SpeakerMatchStep()
        tokens = [
            TokenFeatures(token="hi", onset_ms=0, offset_ms=100, speaker_label="s0"),
            TokenFeatures(token="hi", onset_ms=100, offset_ms=200, speaker_label="s0"),
        ]
        ctx = StackContext(
            inputs={
                "tokens": tokens,
                "fixture_speaker_to_entity": {
                    "s0": [("ent_alice_xxxxxxxx", 0.85)],
                },
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = asyncio.run(step.run(ctx))
        candidates = result.outputs["voice_match_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["target_entity_id"], "ent_alice_xxxxxxxx")
        self.assertEqual(candidates[0]["confidence"], 0.85)


class TestProsodyAffectStep(unittest.TestCase):
    def test_fills_prosody_and_affect(self) -> None:
        step = ProsodyAffectStep()
        tokens = [
            TokenFeatures(token="wow", onset_ms=0, offset_ms=200),
        ]
        ctx = StackContext(
            inputs={
                "tokens": tokens,
                "fixture_prosody": {
                    0: ProsodyVector(pitch_hz=220.0, energy=0.8, speech_rate=4.0),
                },
                "fixture_affect": {
                    0: AffectDistribution(
                        categories={"excited": 0.7, "neutral": 0.2}
                    ),
                },
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        asyncio.run(step.run(ctx))
        self.assertEqual(tokens[0].prosody.pitch_hz, 220.0)
        self.assertEqual(tokens[0].affect.categories["excited"], 0.7)
        self.assertEqual(
            tokens[0].produced_by_model_pass["affect"],
            "stub-prosody-affect@v1",
        )

    def test_memo_override_replaces_inference(self) -> None:
        """A memo on a span overrides prosody/affect for tokens in that span.
        The override is recorded in produced_by_model_pass so dependency
        tracking sees it."""
        step = ProsodyAffectStep()
        tokens = [
            TokenFeatures(token="why", onset_ms=12000, offset_ms=12300),
            TokenFeatures(token="me", onset_ms=12300, offset_ms=12500),
        ]
        # Fixture would say "angry"; memo says it was theatrical
        memo_affect = AffectDistribution(
            categories={"theatrical": 0.95, "amused": 0.05}
        )
        ctx = StackContext(
            inputs={
                "tokens": tokens,
                "fixture_affect": {
                    12000: AffectDistribution(categories={"angry": 0.8}),
                    12300: AffectDistribution(categories={"angry": 0.7}),
                },
                "memo_overrides": [
                    (12000, 18000, {"affect": memo_affect}),
                ],
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        asyncio.run(step.run(ctx))
        for tk in tokens:
            # Memo wins
            self.assertEqual(tk.affect.categories["theatrical"], 0.95)
            self.assertNotIn("angry", tk.affect.categories)
            # Provenance records the override
            self.assertEqual(tk.produced_by_model_pass["affect"], "memo_override")


class TestAccentStep(unittest.TestCase):
    def test_fills_segment_accent(self) -> None:
        step = AccentStep()
        seg = DrawerSegment(
            segment_id="seg_x",
            drawer_id="drw_y",
            start_ms=0,
            end_ms=5000,
        )
        ctx = StackContext(
            inputs={
                "segments": [seg],
                "fixture_segment_accent": {
                    "seg_x": AccentDistribution(
                        categories={"north_american_general": 0.7,
                                    "british_received": 0.1}
                    ),
                },
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        asyncio.run(step.run(ctx))
        self.assertIsNotNone(seg.accent_distribution)
        self.assertEqual(
            seg.accent_distribution.categories["north_american_general"], 0.7
        )


class TestParalinguisticStep(unittest.TestCase):
    def test_filters_to_known_event_kinds(self) -> None:
        step = ParalinguisticStep()
        ctx = StackContext(
            inputs={
                "tokens": [],
                "fixture_paralinguistic_events": [
                    {"event_kind": "laughter", "onset_ms": 1000, "offset_ms": 1500,
                     "confidence": 0.8, "segment_id": "seg_x"},
                    {"event_kind": "code_switch", "onset_ms": 2000, "offset_ms": 2200,
                     "confidence": 0.7, "segment_id": "seg_x"},
                    # Unknown kind — should be filtered out
                    {"event_kind": "applause", "onset_ms": 3000, "offset_ms": 3100,
                     "confidence": 0.9, "segment_id": "seg_x"},
                ],
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = asyncio.run(step.run(ctx))
        events = result.outputs["paralinguistic_events"]
        kinds = [e["event_kind"] for e in events]
        self.assertIn("laughter", kinds)
        self.assertIn("code_switch", kinds)
        self.assertNotIn("applause", kinds)


# =============================================================================
# End-to-end stack composition
# =============================================================================


class TestStackComposition(unittest.TestCase):
    def test_asr_only_stack_runs(self) -> None:
        stack = asr_only_stack()
        self.assertEqual(stack.name, "voice.asr_only")
        self.assertEqual(len(stack.plan), 1)

        ctx = StackContext(
            inputs={
                "fixture_transcription": [("hi", 0, 200), ("there", 200, 500)]
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = _run(stack, ctx)
        self.assertTrue(result.success, msg=f"failed: {result.error}")
        self.assertEqual(len(result.outputs["tokens"]), 2)

    def test_class_1_stack_produces_diarized_matched_tokens(self) -> None:
        stack = class_1_stack()
        ctx = StackContext(
            inputs={
                "fixture_transcription": [
                    ("hello", 0, 500),
                    ("there", 500, 1000),
                ],
                "fixture_speaker_labels": {
                    0: ("s0", 0.9),
                    500: ("s1", 0.85),
                },
                "fixture_speaker_to_entity": {
                    "s0": [("ent_alice_aaaaaaaa", 0.8)],
                    "s1": [("ent_bob_bbbbbbbb", 0.75)],
                },
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = _run(stack, ctx)
        self.assertTrue(result.success, msg=f"failed: {result.error}")
        tokens = result.outputs["tokens"]
        self.assertEqual(tokens[0].speaker_label, "s0")
        self.assertEqual(tokens[1].speaker_label, "s1")
        # Speaker matching collapses to whole-drawer when no segments;
        # only the dominant speaker (here a tie — first wins by max())
        candidates = result.outputs["voice_match_candidates"]
        self.assertGreater(len(candidates), 0)

    def test_full_stack_runs_end_to_end(self) -> None:
        stack = full_stack()
        seg = DrawerSegment(
            segment_id="seg_full_1",
            drawer_id="drw_full_x",
            start_ms=0,
            end_ms=1000,
            dominant_speaker_label="s0",
        )
        ctx = StackContext(
            inputs={
                "fixture_transcription": [
                    ("yes", 0, 300),
                    ("absolutely", 300, 1000),
                ],
                "segments": [seg],
                "fixture_speaker_labels": {
                    0: ("s0", 0.9),
                    300: ("s0", 0.85),
                },
                "fixture_speaker_to_entity": {
                    "s0": [("ent_alice_aaaaaaaa", 0.8)],
                },
                "fixture_prosody": {
                    0: ProsodyVector(pitch_hz=180.0, energy=0.6),
                    300: ProsodyVector(pitch_hz=200.0, energy=0.8),
                },
                "fixture_affect": {
                    0: AffectDistribution(categories={"excited": 0.7}),
                    300: AffectDistribution(categories={"excited": 0.85}),
                },
                "fixture_segment_accent": {
                    "seg_full_1": AccentDistribution(
                        categories={"north_american_general": 0.7}
                    ),
                },
                "fixture_paralinguistic_events": [
                    {"event_kind": "laughter", "onset_ms": 800,
                     "offset_ms": 950, "confidence": 0.7,
                     "segment_id": "seg_full_1"},
                ],
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = _run(stack, ctx)
        self.assertTrue(result.success, msg=f"failed: {result.error}")

        tokens = result.outputs["tokens"]
        self.assertEqual(len(tokens), 2)
        # All enrichments populated
        for tk in tokens:
            self.assertIsNotNone(tk.speaker_label)
            self.assertIsNotNone(tk.prosody)
            self.assertIsNotNone(tk.affect)
        # All provenance recorded
        for tk in tokens:
            self.assertIn("tokens", tk.produced_by_model_pass)
            self.assertIn("speaker_label", tk.produced_by_model_pass)
            self.assertIn("prosody", tk.produced_by_model_pass)
            self.assertIn("affect", tk.produced_by_model_pass)

        # Segment got accent
        segments = result.outputs["segments"]
        self.assertEqual(len(segments), 1)
        self.assertIsNotNone(segments[0].accent_distribution)

        # Paralinguistic event flowed through
        events = result.outputs["paralinguistic_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_kind"], "laughter")


class TestMemoOverrideEndToEnd(unittest.TestCase):
    """The override flows through the full stack."""

    def test_memo_overrides_through_full_stack(self) -> None:
        stack = full_stack()
        seg = DrawerSegment(
            segment_id="seg_memo_1",
            drawer_id="drw_memo_x",
            start_ms=0,
            end_ms=20000,
            dominant_speaker_label="s0",
        )
        # Memo says "the angry tone in seconds 12-18 was theatrical"
        memo_affect = AffectDistribution(
            categories={"theatrical": 0.95, "amused": 0.05}
        )
        ctx = StackContext(
            inputs={
                "fixture_transcription": [
                    ("normal", 5000, 5500),
                    ("WHY", 12000, 12300),
                    ("ME", 12300, 12700),
                ],
                "segments": [seg],
                "fixture_affect": {
                    5000: AffectDistribution(categories={"neutral": 0.9}),
                    12000: AffectDistribution(categories={"angry": 0.85}),
                    12300: AffectDistribution(categories={"angry": 0.8}),
                },
                "memo_overrides": [
                    (12000, 18000, {"affect": memo_affect}),
                ],
            },
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
        result = _run(stack, ctx)
        self.assertTrue(result.success, msg=f"failed: {result.error}")

        tokens = result.outputs["tokens"]
        # Token outside override range keeps inferred affect
        self.assertEqual(tokens[0].onset_ms, 5000)
        self.assertEqual(tokens[0].affect.categories["neutral"], 0.9)
        self.assertEqual(
            tokens[0].produced_by_model_pass["affect"],
            "stub-prosody-affect@v1",
        )
        # Tokens inside override range get memo's affect
        for tk in tokens[1:]:
            self.assertEqual(tk.affect.categories["theatrical"], 0.95)
            self.assertEqual(
                tk.produced_by_model_pass["affect"], "memo_override"
            )


if __name__ == "__main__":
    unittest.main()
