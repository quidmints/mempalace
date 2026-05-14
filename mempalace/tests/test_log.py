"""Tests for the event log substrate (Part 2)."""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient, MockBackend
from mempalace.schema.events import (
    DerivationType,
    NodeCreated,
    NodePropertySet,
)
from mempalace.schema.identifiers import (
    make_event_id_log,
    make_theme_id,
)
from mempalace.tests.conftest import reset_module_state


class TestLogAppend(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.client = LogClient(backend=self.backend)

    def test_append_valid_event_assigns_offset(self) -> None:
        ev = NodeCreated(
            event_id=make_event_id_log(1_000),
            recorded_at=1_000,
            actor="test",
            node_id=make_theme_id(),
            node_kind="theme",
            properties={"name": "Test"},
        )
        result = self.client.append(ev)
        self.assertTrue(result.accepted)
        self.assertEqual(result.offset, 1)

    def test_append_assigns_recorded_at_when_zero(self) -> None:
        ev = NodeCreated(
            event_id=make_event_id_log(0),
            recorded_at=0,
            actor="test",
            node_id=make_theme_id(),
            node_kind="theme",
            properties={"name": "T"},
        )
        result = self.client.append(ev)
        self.assertTrue(result.accepted)
        self.assertGreater(ev.recorded_at, 0)

    def test_append_rejects_invalid_event(self) -> None:
        # node_id is malformed → validator rejects
        ev = NodeCreated(
            event_id=make_event_id_log(1_000),
            recorded_at=1_000,
            actor="test",
            node_id="not_a_valid_id_format",
            node_kind="theme",
            properties={"name": "Bad"},
        )
        result = self.client.append(ev)
        self.assertFalse(result.accepted)
        # The rejection itself is appended (record of the failure)
        rows = self.backend.read_range(0, self.backend.current_offset() + 1)
        kinds = {r[1] for r in rows}
        self.assertIn("append_rejected", kinds)


class TestLogRange(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.backend = MockBackend()
        self.client = LogClient(backend=self.backend)
        for i in range(5):
            ev = NodeCreated(
                event_id=make_event_id_log(1_000 + i),
                recorded_at=1_000 + i,
                actor="test",
                node_id=make_theme_id(ts_ms=1_000 + i),
                node_kind="theme",
                properties={"name": f"T{i}"},
            )
            self.client.append(ev)

    def test_read_range_inclusive_start_exclusive_end(self) -> None:
        rows = self.backend.read_range(2, 4)
        self.assertEqual(len(rows), 2)
        offsets = [r[0] for r in rows]
        self.assertEqual(offsets, [2, 3])

    def test_current_offset_advances(self) -> None:
        before = self.backend.current_offset()
        self.assertEqual(before, 5)
        ev = NodeCreated(
            event_id=make_event_id_log(2_000),
            recorded_at=2_000,
            actor="test",
            node_id=make_theme_id(ts_ms=2_000),
            node_kind="theme",
            properties={"name": "T6"},
        )
        self.client.append(ev)
        self.assertEqual(self.backend.current_offset(), 6)


if __name__ == "__main__":
    unittest.main()
