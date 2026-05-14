"""Tests for drawer capture (Part 4)."""

from __future__ import annotations

import unittest

from mempalace.tests.conftest import fresh_palace


class TestDrawerCapture(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()

    def test_drawer_captured_event_appends(self) -> None:
        from mempalace.schema.events import DrawerCaptured
        from mempalace.schema.identifiers import (
            make_drawer_id,
            make_event_id_log,
        )
        import hashlib

        content = "hello world"
        ev = DrawerCaptured(
            event_id=make_event_id_log(1_000),
            recorded_at=1_000,
            actor="capture",
            drawer_id=make_drawer_id(ts_ms=1_000),
            content_hash=hashlib.blake2b(content.encode(), digest_size=32).hexdigest(),
            capture_recorded_at=1_000,
            duration_ms=2_500,
            interactional="memo_to_self",
            state_context={},
        )
        result = self.p["log"].append(ev)
        self.assertTrue(result.accepted)

    def test_collision_event_when_two_drawers_share_hash(self) -> None:
        # Hash-collision: same content_hash with different drawer_ids → emit
        # DrawerHashCollision (per R3 §5.3 — identical content is a glitch).
        from mempalace.schema.events import DrawerCaptured, DrawerHashCollision
        from mempalace.schema.identifiers import (
            make_drawer_id,
            make_event_id_log,
        )
        import hashlib

        content = "duplicate content"
        h = hashlib.blake2b(content.encode(), digest_size=32).hexdigest()
        d1 = make_drawer_id(ts_ms=1_000)
        d2 = make_drawer_id(ts_ms=1_001)

        for did, ts in ((d1, 1_000), (d2, 1_001)):
            ev = DrawerCaptured(
                event_id=make_event_id_log(ts),
                recorded_at=ts,
                actor="capture",
                drawer_id=did,
                content_hash=h,
                capture_recorded_at=ts,
                duration_ms=1_000,
                interactional="memo_to_self",
                state_context={},
            )
            self.p["log"].append(ev)

        # Collision event explicitly recorded — collision is a glitch, not silent dedup
        ev_col = DrawerHashCollision(
            event_id=make_event_id_log(1_002),
            recorded_at=1_002,
            actor="dedupe",
            content_hash=h,
            incoming_drawer_id=d2,
            existing_drawer_id=d1,
        )
        result = self.p["log"].append(ev_col)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
