"""
Tests for the `mempalace.Palace` facade — the package entrypoint
that bundles subsystems.

Coverage:
  - `from mempalace import Palace` works (package entry exists)
  - Palace.create() instantiates without errors
  - All subsystem references are populated
  - capture() works through the facade
  - tick() works
  - close() is idempotent
  - context manager usage works
"""

from __future__ import annotations

import unittest

import mempalace
from mempalace import Palace, PalaceConfig
from mempalace.tests.conftest import fresh_palace, reset_module_state


class TestPackageEntry(unittest.TestCase):
    def test_palace_importable_from_package(self) -> None:
        # If __init__.py is missing, this would fail at import time.
        self.assertTrue(hasattr(mempalace, "Palace"))
        self.assertTrue(hasattr(mempalace, "PalaceConfig"))
        self.assertEqual(mempalace.Palace, Palace)

    def test_version_present(self) -> None:
        self.assertTrue(hasattr(mempalace, "__version__"))
        self.assertTrue(mempalace.__version__.startswith("5."))


class TestPalaceConstruction(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()

    def test_create_in_memory(self) -> None:
        palace = Palace.create()
        try:
            self.assertIsNotNone(palace.log)
            self.assertIsNotNone(palace.graph)
            self.assertIsNotNone(palace.canonicalizer)
            self.assertIsNotNone(palace.handle_manager)
            self.assertIsNotNone(palace.phone_off)
        finally:
            palace.close()

    def test_phone_off_can_be_disabled(self) -> None:
        palace = Palace.create(enable_phone_off_state_machine=False)
        try:
            self.assertIsNone(palace.phone_off)
        finally:
            palace.close()

    def test_open_requires_palace_dir(self) -> None:
        with self.assertRaises(ValueError):
            Palace.open(palace_dir="")


class TestSubsystemAccess(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.palace = Palace.create()

    def tearDown(self) -> None:
        self.palace.close()

    def test_federate_module(self) -> None:
        from mempalace import federate
        self.assertIs(self.palace.federate, federate)

    def test_miner_module(self) -> None:
        from mempalace import miner
        self.assertIs(self.palace.miner, miner)

    def test_switchboard_module(self) -> None:
        from mempalace import switchboard
        self.assertIs(self.palace.switchboard, switchboard)

    def test_secure_module(self) -> None:
        from mempalace import secure
        self.assertIs(self.palace.secure, secure)

    def test_query_queue(self) -> None:
        from mempalace.query.bidirectional import PendingQueryQueue
        self.assertIsInstance(self.palace.query_q, PendingQueryQueue)


class TestPublicVerbs(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()
        self.palace = Palace.create()

    def tearDown(self) -> None:
        self.palace.close()

    def test_capture(self) -> None:
        result = self.palace.capture(transcript="something happened")
        self.assertIsNotNone(result.drawer_id)
        self.assertTrue(result.drawer_id.startswith("drw_"))

    def test_tick_returns_count(self) -> None:
        self.palace.capture(transcript="event 1")
        self.palace.capture(transcript="event 2")
        delivered = self.palace.tick()
        # tick_views may have already processed some during capture's
        # internal flow; we just check it returns a non-negative int.
        self.assertGreaterEqual(delivered, 0)

    def test_assert_returns_assertion_id(self) -> None:
        from mempalace.schema.events import NodeCreated
        from mempalace.schema.identifiers import (
            SELF_ENTITY_ID, make_entity_id, make_event_id_log,
        )
        # Bootstrap two entities for the assertion to reference
        self.palace.log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1, actor="t",
            node_id=SELF_ENTITY_ID, node_kind="entity", properties={},
        ))
        trait_id = make_entity_id()
        self.palace.log.append(NodeCreated(
            event_id=make_event_id_log(), recorded_at=1, actor="t",
            node_id=trait_id, node_kind="entity",
            properties={"name": "curious"},
        ))
        self.palace.tick()

        aid = self.palace.assert_(
            subject_id=SELF_ENTITY_ID,
            predicate="has_trait",
            object_id=trait_id,
        )
        self.assertTrue(aid.startswith("ast_"))


class TestLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        fresh_palace()

    def test_close_idempotent(self) -> None:
        palace = Palace.create()
        palace.close()
        palace.close()  # second close should not raise

    def test_context_manager(self) -> None:
        with Palace.create() as palace:
            self.assertIsNotNone(palace.log)
        # palace is closed after the with block; close is internal


if __name__ == "__main__":
    unittest.main()
