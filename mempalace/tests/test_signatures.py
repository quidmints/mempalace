"""Tests for the signature store (Part 12, R3 §8)."""

from __future__ import annotations

import unittest

from mempalace.signatures.baseline import (
    MIN_BASELINE_WINDOW_DAYS,
    MIN_BASELINE_WINDOW_MS,
)
from mempalace.signatures.store import (
    SignatureSnapshot,
    SignatureStore,
)
from mempalace.signatures.triage_indicator import TriageIndicator
from mempalace.tests.conftest import reset_module_state


class TestSignatureStore(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.store = SignatureStore()

    def test_put_and_get(self) -> None:
        snap = SignatureSnapshot(
            snapshot_id="sig_001",
            period_id="prd_001",
            captured_at_ms=1_000,
        )
        self.store.put(snap)
        got = self.store.get("prd_001")
        self.assertIsNotNone(got)
        self.assertEqual(got.snapshot_id, "sig_001")

    def test_chronological_returns_in_time_order(self) -> None:
        snaps = [
            SignatureSnapshot(snapshot_id=f"s{i}", period_id=f"p{i}",
                              captured_at_ms=1_000 + i)
            for i in range(3)
        ]
        # insert out of order
        self.store.put(snaps[2])
        self.store.put(snaps[0])
        self.store.put(snaps[1])
        chrono = self.store.chronological()
        timestamps = [s.captured_at_ms for s in chrono]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_latest_returns_most_recent(self) -> None:
        for i in range(5):
            self.store.put(SignatureSnapshot(
                snapshot_id=f"s{i}", period_id=f"p{i}",
                captured_at_ms=1_000 + i,
            ))
        latest = self.store.latest()
        self.assertEqual(latest.captured_at_ms, 1_004)


class TestBaselineWindow(unittest.TestCase):
    def test_minimum_window_90_days(self) -> None:
        # Per R3 §5.4: behavior-vs-baseline markets require >=90 days
        # of consistent embedding history.
        self.assertEqual(MIN_BASELINE_WINDOW_DAYS, 90)
        self.assertEqual(
            MIN_BASELINE_WINDOW_MS, 90 * 24 * 3600 * 1000,
        )


class TestTriageIndicator(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_score_in_range(self) -> None:
        ti = TriageIndicator()
        local = SignatureSnapshot(
            snapshot_id="loc",
            period_id="p_local",
            captured_at_ms=1_000,
        )
        foreign = SignatureSnapshot(
            snapshot_id="frn",
            period_id="p_foreign",
            captured_at_ms=2_000,
        )
        score = ti.score(local, foreign)
        self.assertGreaterEqual(score.pair_score, 0.0)
        self.assertLessEqual(score.pair_score, 1.0)

    def test_false_positive_capped_after_repeated_hits(self) -> None:
        ti = TriageIndicator()
        for i in range(50):
            ti.record_false_positive(
                per_axis_similarity={"theme_velocity": 0.9, "schema_drift": 0.8},
                now_ms=i * 1_000,
            )
        # Weights should remain non-negative; floor enforces 0.1× original
        for axis, w in ti._weights.items():
            self.assertGreaterEqual(w, 0.0)


if __name__ == "__main__":
    unittest.main()
