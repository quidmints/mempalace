"""Tests for migrate/ — converter + invariants."""

from __future__ import annotations

import unittest

from mempalace.migrate import (
    Converter,
    InvariantReport,
    LegacyDrawer,
    LegacyPeriod,
    LegacyTheme,
    LegacyTriple,
    run_all,
    synth_drawer_event,
    synth_theme_event,
    synth_period_event,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state


class TestSynthEvents(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_synth_drawer_event_fields(self) -> None:
        ld = LegacyDrawer(
            drawer_id="legacy_drw_1",
            content="some drawer content",
            created_at_ms=1_500_000_000_000,
            duration_ms=2_500,
        )
        ev = synth_drawer_event(ld)
        self.assertEqual(ev.EVENT_KIND, "drawer_captured")
        self.assertEqual(ev.duration_ms, 2_500)
        self.assertEqual(len(ev.content_hash), 64)  # 32-byte blake2b hex
        self.assertTrue(ev.drawer_id.startswith("drw_"))

    def test_synth_drawer_event_with_explicit_id(self) -> None:
        ld = LegacyDrawer(drawer_id="x", content="y", created_at_ms=0)
        ev = synth_drawer_event(ld, drawer_id="drw_aa_bb_cc_dd")
        self.assertEqual(ev.drawer_id, "drw_aa_bb_cc_dd")

    def test_synth_theme_event(self) -> None:
        lt = LegacyTheme(theme_id="legacy_thm_1", name="Running")
        ev = synth_theme_event(lt)
        self.assertEqual(ev.node_kind, "theme")
        self.assertEqual(ev.properties["name"], "Running")
        self.assertEqual(ev.properties["legacy_id"], "legacy_thm_1")


class TestConverter(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()

    def test_convert_themes(self) -> None:
        themes = [
            LegacyTheme(theme_id="thm_a", name="Alpha"),
            LegacyTheme(theme_id="thm_b", name="Beta"),
        ]
        conv = Converter(log=self.p["log"])
        conv.convert_themes(themes)
        self.assertEqual(conv.report.themes_converted, 2)
        # Both legacy ids should be remapped to fresh new-format ids
        self.assertIn("thm_a", conv.report.id_remap)
        self.assertIn("thm_b", conv.report.id_remap)
        self.assertNotEqual(conv.report.id_remap["thm_a"], "thm_a")

    def test_convert_period_uses_remapped_theme_id(self) -> None:
        # Theme must exist before period
        themes = [LegacyTheme(theme_id="legacy_t", name="T")]
        periods = [LegacyPeriod(
            period_id="legacy_p", theme_id="legacy_t",
            name="P1", started_at_ms=1_000_000,
            ended_at_ms=2_000_000, sealed=True,
        )]
        conv = Converter(log=self.p["log"])
        conv.convert_themes(themes)
        conv.convert_periods(periods)
        self.assertEqual(conv.report.periods_converted, 1)
        self.assertIn("legacy_p", conv.report.id_remap)

    def test_empty_triple_rejected(self) -> None:
        conv = Converter(log=self.p["log"])
        bad = [LegacyTriple(subject="", predicate="x", object_="y")]
        conv.convert_triples(bad)
        self.assertEqual(conv.report.triples_converted, 0)
        self.assertEqual(conv.report.rejected_count, 1)

    def test_full_run_topological_order(self) -> None:
        conv = Converter(log=self.p["log"])
        # Graph entities need to exist for the triple
        a = self.p["graph"].create_entity(name="Alice")
        b = self.p["graph"].create_entity(name="Running")
        report = conv.run(
            themes=[LegacyTheme(theme_id="t1", name="Running")],
            periods=[LegacyPeriod(
                period_id="p1", theme_id="t1", name="P1",
                started_at_ms=1_000, ended_at_ms=2_000,
            )],
            drawers=[LegacyDrawer(
                drawer_id="d1", content="hi", created_at_ms=1_500,
            )],
            triples=[LegacyTriple(
                subject=a, predicate="enjoys", object_=b, confidence=0.9,
            )],
        )
        self.assertEqual(report.themes_converted, 1)
        self.assertEqual(report.periods_converted, 1)
        self.assertEqual(report.drawers_converted, 1)
        self.assertEqual(report.triples_converted, 1)


class TestInvariants(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()

    def test_run_all_returns_report_object(self) -> None:
        from mempalace.views.current import tick_views
        # Empty palace
        tick_views()
        report = run_all()
        self.assertIsInstance(report, InvariantReport)
        self.assertEqual(len(report.invariants_run), 6)
        # Empty palace → no violations
        self.assertEqual(len(report.violations), 0)
        self.assertTrue(report.ok)

    def test_run_all_with_subset(self) -> None:
        report = run_all(["I3.period_timing"])
        self.assertEqual(report.invariants_run, ["I3.period_timing"])


if __name__ == "__main__":
    unittest.main()
