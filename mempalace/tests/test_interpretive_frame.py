"""Tests for Track 2's typed InterpretiveFrame.

Covers:
  - The five axis dataclasses construct cleanly with defaults.
  - InterpretiveFrame composes axes; populated_axes() reports correctly.
  - is_empty() distinguishes placeholder frames from filled ones.
  - default_rate_features_weight() produces sensible defaults for each
    Conway class.
  - The whole shape is hashable / equality-checkable per dataclass
    semantics (or correctly NOT hashable when mutable lists/dicts
    are present — the test confirms expected behavior).
"""

from __future__ import annotations

import unittest

from mempalace.handle import (
    CONWAY_RATE_CLASS_1,
    CONWAY_RATE_CLASS_2,
    CONWAY_RATE_CLASS_3,
    CoActivationPattern,
    ConwayRate,
    InterpretiveFrame,
    RefinementCues,
    SignatureRegion,
    VoiceFlavor,
    default_rate_features_weight,
)


# =============================================================================
# Axis dataclass shapes
# =============================================================================


class TestSignatureRegion(unittest.TestCase):
    def test_defaults_to_empty(self) -> None:
        sr = SignatureRegion()
        self.assertEqual(sr.centered_on_theme_ids, [])
        self.assertEqual(sr.target_position, {})
        self.assertIsNone(sr.target_velocity_band)
        self.assertEqual(sr.contradiction_zone, "any")

    def test_populated(self) -> None:
        sr = SignatureRegion(
            centered_on_theme_ids=["t1", "t2"],
            target_position={"t1": [0.1, 0.2, 0.3]},
            target_velocity_band=(0.1, 0.5),
            schema_fingerprints_required=["fp_a"],
            contradiction_zone="resolved",
            fork_distribution_target=[0.1, 0.2, 0.3, 0.2, 0.2],
        )
        self.assertEqual(sr.centered_on_theme_ids, ["t1", "t2"])
        self.assertEqual(sr.target_velocity_band, (0.1, 0.5))


class TestConwayRate(unittest.TestCase):
    def test_defaults_to_class_2(self) -> None:
        cr = ConwayRate()
        self.assertEqual(cr.target_rate, CONWAY_RATE_CLASS_2)
        self.assertEqual(cr.rate_confidence, 0.5)
        self.assertEqual(cr.rate_features_weight, {})

    def test_explicit_class_1(self) -> None:
        cr = ConwayRate(
            target_rate=CONWAY_RATE_CLASS_1,
            rate_confidence=0.9,
            rate_features_weight=default_rate_features_weight(
                CONWAY_RATE_CLASS_1
            ),
        )
        self.assertEqual(cr.target_rate, 1)
        self.assertGreater(
            cr.rate_features_weight["drawer_recency_score"], 1.0
        )


class TestCoActivationPattern(unittest.TestCase):
    def test_defaults(self) -> None:
        cap = CoActivationPattern()
        self.assertEqual(cap.seed_recurrence_cluster_ids, [])
        self.assertEqual(cap.co_active_node_kinds, {})
        self.assertEqual(cap.co_active_edge_kinds, {})

    def test_with_seeds(self) -> None:
        cap = CoActivationPattern(
            seed_recurrence_cluster_ids=["rc_1", "rc_2"],
            co_active_node_kinds={"theme": 0.8, "event": 0.6},
            co_active_edge_kinds={"derived_from": 0.7},
        )
        self.assertEqual(len(cap.seed_recurrence_cluster_ids), 2)
        self.assertEqual(cap.co_active_node_kinds["theme"], 0.8)


class TestRefinementCues(unittest.TestCase):
    def test_defaults(self) -> None:
        rc = RefinementCues()
        self.assertEqual(rc.more_like_node_ids, [])
        self.assertEqual(rc.less_like_node_ids, [])
        self.assertEqual(rc.stance_pulls, {})
        self.assertEqual(rc.voice_match_pulls, [])

    def test_voice_match_pulls_shape(self) -> None:
        """voice_match_pulls is list[(entity_id, confidence)]."""
        rc = RefinementCues(
            voice_match_pulls=[
                ("ent_alice_aaaaaaaa", 0.85),
                ("ent_bob_bbbbbbbb", 0.55),
            ],
        )
        self.assertEqual(len(rc.voice_match_pulls), 2)
        self.assertEqual(rc.voice_match_pulls[0][0], "ent_alice_aaaaaaaa")
        self.assertEqual(rc.voice_match_pulls[0][1], 0.85)


class TestVoiceFlavor(unittest.TestCase):
    def test_defaults(self) -> None:
        vf = VoiceFlavor()
        self.assertEqual(vf.target_speaker_entities, [])
        self.assertEqual(vf.target_affect_distribution, {})
        self.assertIsNone(vf.prosody_target)
        self.assertEqual(vf.confidence, 0.5)

    def test_prosody_target_shape(self) -> None:
        vf = VoiceFlavor(
            prosody_target={"pitch_hz": (180.0, 260.0), "energy": (0.5, 0.9)},
        )
        self.assertIn("pitch_hz", vf.prosody_target)
        self.assertEqual(vf.prosody_target["pitch_hz"], (180.0, 260.0))


# =============================================================================
# InterpretiveFrame composition
# =============================================================================


class TestInterpretiveFrame(unittest.TestCase):
    def test_empty_frame(self) -> None:
        f = InterpretiveFrame(frame_id="f1", confidence=0.5)
        self.assertEqual(f.populated_axes(), [])
        self.assertTrue(f.is_empty())

    def test_single_axis_populated(self) -> None:
        f = InterpretiveFrame(
            frame_id="f1",
            confidence=0.7,
            signature_region=SignatureRegion(centered_on_theme_ids=["t1"]),
        )
        self.assertEqual(f.populated_axes(), ["signature_region"])
        self.assertFalse(f.is_empty())

    def test_all_axes_populated(self) -> None:
        f = InterpretiveFrame(
            frame_id="f_full",
            confidence=0.9,
            description="all-axes test frame",
            derived_from_refinements=[0, 1],
            signature_region=SignatureRegion(),
            conway_rate=ConwayRate(),
            co_activation_pattern=CoActivationPattern(),
            refinement_cues=RefinementCues(),
            voice_flavor=VoiceFlavor(),
        )
        axes = f.populated_axes()
        self.assertEqual(
            set(axes),
            {
                "signature_region",
                "conway_rate",
                "co_activation_pattern",
                "refinement_cues",
                "voice_flavor",
            },
        )
        self.assertFalse(f.is_empty())

    def test_partial_population(self) -> None:
        """A typical frame: voice + refinement, but no signature
        region yet."""
        f = InterpretiveFrame(
            frame_id="f_voice",
            confidence=0.6,
            description="voice-driven cue",
            voice_flavor=VoiceFlavor(
                target_speaker_entities=["ent_alice_xxxxxxxx"],
                confidence=0.85,
            ),
            refinement_cues=RefinementCues(
                more_like_node_ids=["nde_seed_xxxxxxxx"],
            ),
        )
        self.assertEqual(
            set(f.populated_axes()),
            {"voice_flavor", "refinement_cues"},
        )
        self.assertEqual(
            f.voice_flavor.target_speaker_entities,
            ["ent_alice_xxxxxxxx"],
        )

    def test_derived_from_refinements_tracks_indices(self) -> None:
        f = InterpretiveFrame(
            frame_id="f1",
            confidence=0.5,
            derived_from_refinements=[0, 2, 4],
        )
        self.assertEqual(f.derived_from_refinements, [0, 2, 4])

    def test_frame_equality(self) -> None:
        """Two frames with identical contents compare equal."""
        f1 = InterpretiveFrame(
            frame_id="f1",
            confidence=0.7,
            conway_rate=ConwayRate(target_rate=CONWAY_RATE_CLASS_3),
        )
        f2 = InterpretiveFrame(
            frame_id="f1",
            confidence=0.7,
            conway_rate=ConwayRate(target_rate=CONWAY_RATE_CLASS_3),
        )
        self.assertEqual(f1, f2)

    def test_frame_inequality(self) -> None:
        f1 = InterpretiveFrame(frame_id="f1", confidence=0.7)
        f2 = InterpretiveFrame(frame_id="f2", confidence=0.7)
        self.assertNotEqual(f1, f2)


# =============================================================================
# default_rate_features_weight
# =============================================================================


class TestDefaultRateFeaturesWeight(unittest.TestCase):
    def test_class_1_boosts_recency(self) -> None:
        w = default_rate_features_weight(CONWAY_RATE_CLASS_1)
        self.assertGreater(w["drawer_recency_score"], 1.0)
        self.assertGreater(w["drawer_heat"], 1.0)
        # Class 1 should DEPRESS canonicality
        self.assertLess(w["theme_canonicality"], 1.0)

    def test_class_2_balanced(self) -> None:
        w = default_rate_features_weight(CONWAY_RATE_CLASS_2)
        # Class 2 should have neutral weights for the basic features
        self.assertEqual(w["drawer_recency_score"], 1.0)
        self.assertEqual(w["theme_canonicality"], 1.0)
        # And boost period coupling + fork significance
        self.assertGreater(w["event_fork_significance"], 1.0)

    def test_class_3_boosts_canonicality(self) -> None:
        w = default_rate_features_weight(CONWAY_RATE_CLASS_3)
        self.assertGreater(w["theme_canonicality"], 1.0)
        self.assertGreater(w["assertion_substrate_faithfulness"], 1.0)
        # And depress recency
        self.assertLess(w["drawer_recency_score"], 1.0)

    def test_invalid_rate_returns_empty(self) -> None:
        """Unknown rates should fall back to neutral (no weights),
        not raise."""
        self.assertEqual(default_rate_features_weight(99), {})
        self.assertEqual(default_rate_features_weight(0), {})

    def test_class_1_and_3_disagree_on_recency(self) -> None:
        """Sanity check: the rates should genuinely differ."""
        w1 = default_rate_features_weight(CONWAY_RATE_CLASS_1)
        w3 = default_rate_features_weight(CONWAY_RATE_CLASS_3)
        self.assertGreater(
            w1["drawer_recency_score"],
            w3["drawer_recency_score"],
        )
        self.assertGreater(
            w3["theme_canonicality"],
            w1["theme_canonicality"],
        )


# =============================================================================
# Frame composition realistic scenarios
# =============================================================================


class TestRealisticFrameScenarios(unittest.TestCase):
    """Scenarios from SUBSTRATE_SIGNAL_ANALYSIS.md §3 — verify the
    typed shape can express the example frames the analysis described."""

    def test_recent_event_frame_class_1(self) -> None:
        """Frame from §3 Axis 2: 'a query about a recent event'."""
        f = InterpretiveFrame(
            frame_id="f_recent",
            confidence=0.8,
            description="Recent event — Class 1 rate",
            conway_rate=ConwayRate(
                target_rate=CONWAY_RATE_CLASS_1,
                rate_confidence=0.85,
                rate_features_weight=default_rate_features_weight(
                    CONWAY_RATE_CLASS_1
                ),
            ),
            signature_region=SignatureRegion(
                target_velocity_band=(0.5, 1.0),  # high velocity
            ),
        )
        self.assertEqual(f.conway_rate.target_rate, 1)
        self.assertEqual(
            f.signature_region.target_velocity_band, (0.5, 1.0)
        )

    def test_thematic_running_query_class_3(self) -> None:
        """Frame from §3: 'long-running themes'."""
        f = InterpretiveFrame(
            frame_id="f_thematic",
            confidence=0.75,
            description="Long-running theme — Class 3",
            conway_rate=ConwayRate(
                target_rate=CONWAY_RATE_CLASS_3,
                rate_confidence=0.9,
                rate_features_weight=default_rate_features_weight(
                    CONWAY_RATE_CLASS_3
                ),
            ),
            signature_region=SignatureRegion(
                schema_fingerprints_required=["fp_running"],
                contradiction_zone="resolved",
            ),
        )
        self.assertEqual(f.conway_rate.target_rate, 3)
        self.assertEqual(
            f.signature_region.contradiction_zone, "resolved"
        )

    def test_voice_driven_speaker_disambiguation(self) -> None:
        """Frame from §3 Axis 5: voice cue identifies who the user
        meant when two entities share a name."""
        f = InterpretiveFrame(
            frame_id="f_voice_ambig",
            confidence=0.7,
            description="Speaker-disambiguating frame from voice cue",
            voice_flavor=VoiceFlavor(
                target_speaker_entities=["ent_alice_aaaaaaaa"],
                target_affect_distribution={"amused": 0.6, "neutral": 0.4},
                confidence=0.8,
            ),
            refinement_cues=RefinementCues(
                voice_match_pulls=[("ent_alice_aaaaaaaa", 0.85)],
            ),
        )
        self.assertIn("voice_flavor", f.populated_axes())
        self.assertIn("refinement_cues", f.populated_axes())
        self.assertEqual(
            f.voice_flavor.target_speaker_entities,
            ["ent_alice_aaaaaaaa"],
        )

    def test_co_activation_grounded_frame(self) -> None:
        """Frame from §3 Axis 3: grounded in a recurrence cluster
        the miner produced."""
        f = InterpretiveFrame(
            frame_id="f_coact",
            confidence=0.65,
            description="Inheriting from miner-identified cluster",
            co_activation_pattern=CoActivationPattern(
                seed_recurrence_cluster_ids=["rc_morning_routine"],
                co_active_node_kinds={
                    "event": 0.8,
                    "period": 0.6,
                },
                co_active_edge_kinds={
                    "succeeds": 0.7,
                    "located_at": 0.5,
                },
            ),
        )
        self.assertEqual(
            f.co_activation_pattern.seed_recurrence_cluster_ids,
            ["rc_morning_routine"],
        )
        self.assertEqual(
            f.co_activation_pattern.co_active_node_kinds["event"], 0.8
        )


if __name__ == "__main__":
    unittest.main()
