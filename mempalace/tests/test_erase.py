"""Tests for Track 6D — erasure compaction job.

Covers:
  - request_erase appends a RequestErase event with a job_id.
  - EraseJob walks the log, identifies events referencing target,
    rewrites them to tombstone form.
  - Tombstoning preserves structural fields (drawer_id, content_hash,
    event_id, batch_id) and strips ciphertext / user-content fields.
  - Re-running a completed job is idempotent.
  - Job emits EraseProgress and EraseCompleted events.
  - On backend that doesn't support tombstoning, job emits EraseFailed.
  - Reference detection handles drawer / node / edge target_kinds.
"""

from __future__ import annotations

import unittest

from mempalace.drawer.capture import capture_drawer
from mempalace.embed.client import EmbeddingStore, InMemoryBackend
from mempalace.embed.model import EmbeddingService
from mempalace.schema.events import EdgeCreated, NodeCreated
from mempalace.schema.identifiers import (
    make_edge_id,
    make_entity_id,
    make_event_id_log,
)
from mempalace.schema.kinds import InteractionalKind
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.erase import (
    ERASURE_TOMBSTONE_MARKER,
    ERASURE_TOMBSTONE_REASON_FIELD,
    EraseJob,
    SnapshotEraser,
    _build_tombstone_payload,
    _references_target,
    request_erase,
)


# =============================================================================
# Reference detection
# =============================================================================


class TestReferenceDetection(unittest.TestCase):
    def test_drawer_id_matches(self) -> None:
        self.assertTrue(
            _references_target(
                {"drawer_id": "drw_x"}, "drawer", "drw_x"
            )
        )
        self.assertFalse(
            _references_target(
                {"drawer_id": "drw_y"}, "drawer", "drw_x"
            )
        )

    def test_drawer_in_derived_from_drawers(self) -> None:
        self.assertTrue(
            _references_target(
                {"derived_from_drawers": ["drw_a", "drw_x", "drw_b"]},
                "drawer",
                "drw_x",
            )
        )

    def test_drawer_as_edge_endpoint(self) -> None:
        """A drawer is also a node, so edges that touch it count."""
        self.assertTrue(
            _references_target(
                {"source_node_id": "drw_x", "target_node_id": "nde_y"},
                "drawer", "drw_x",
            )
        )
        self.assertTrue(
            _references_target(
                {"source_node_id": "nde_y", "target_node_id": "drw_x"},
                "drawer", "drw_x",
            )
        )

    def test_node_target_kind(self) -> None:
        self.assertTrue(
            _references_target(
                {"node_id": "nde_x"}, "node", "nde_x"
            )
        )
        self.assertTrue(
            _references_target(
                {"source_node_id": "nde_x", "target_node_id": "nde_y"},
                "node", "nde_x",
            )
        )

    def test_edge_target_kind(self) -> None:
        self.assertTrue(
            _references_target(
                {"edge_id": "edg_z"}, "edge", "edg_z"
            )
        )
        self.assertFalse(
            _references_target(
                {"edge_id": "edg_other"}, "edge", "edg_z"
            )
        )


# =============================================================================
# Tombstone payload construction
# =============================================================================


class TestTombstonePayload(unittest.TestCase):
    def test_strips_drawer_captured_fields(self) -> None:
        payload = {
            "drawer_id": "drw_x",
            "content_hash": "abc123",
            "verbatim_text": "secret content",
            "verbatim_ciphertext": b"\x00\x01\x02",
            "state_context": {"sleep_state": "alert"},
            "goal_markers": ["work"],
            "duration_ms": 5000,
        }
        new = _build_tombstone_payload("drawer_captured", payload, "drw_x")

        # Stripped
        self.assertEqual(new["verbatim_text"], "")
        self.assertEqual(new["verbatim_ciphertext"], b"")
        self.assertEqual(new["state_context"], {})
        self.assertEqual(new["goal_markers"], [])

        # Preserved
        self.assertEqual(new["drawer_id"], "drw_x")
        self.assertEqual(new["content_hash"], "abc123")
        self.assertEqual(new["duration_ms"], 5000)

        # Markers added
        self.assertTrue(new[ERASURE_TOMBSTONE_MARKER])
        self.assertEqual(new[ERASURE_TOMBSTONE_REASON_FIELD], "drw_x")

    def test_unrecognized_kind_only_marks(self) -> None:
        payload = {"some_field": "kept", "value": 42}
        new = _build_tombstone_payload("unknown_kind", payload, "tgt_x")
        # Fields preserved (no strip rules for unknown kind)
        self.assertEqual(new["some_field"], "kept")
        self.assertEqual(new["value"], 42)
        # Markers added
        self.assertTrue(new[ERASURE_TOMBSTONE_MARKER])

    def test_strips_node_property_set_value(self) -> None:
        payload = {
            "node_id": "nde_x",
            "property_name": "name",
            "new_value": "Sensitive Person Name",
        }
        new = _build_tombstone_payload("node_property_set", payload, "nde_x")
        # new_value is a string, so it's stripped to "" (type-matched empty)
        self.assertEqual(new["new_value"], "")
        self.assertEqual(new["node_id"], "nde_x")


# =============================================================================
# request_erase
# =============================================================================


class TestRequestErase(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_appends_request_event(self) -> None:
        job_id = request_erase("drawer", "drw_target", log_client=self.p["log"])
        self.assertTrue(job_id.startswith("erj_"))

        # Find the event
        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        request_evts = [
            (off, kind, payload)
            for off, kind, payload in rows
            if kind == "request_erase"
        ]
        self.assertEqual(len(request_evts), 1)
        _o, _k, payload = request_evts[0]
        self.assertEqual(payload["target_kind"], "drawer")
        self.assertEqual(payload["target_id"], "drw_target")
        self.assertEqual(payload["erasure_job_id"], job_id)


# =============================================================================
# EraseJob — end-to-end
# =============================================================================


class TestEraseJobBasics(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def _capture_drawer(self, *, transcript: str = "test content") -> str:
        result = capture_drawer(
            transcript=transcript,
            actor="test",
            duration_ms=500,
            log_client=self.p["log"],
            embedding_service=EmbeddingService(),
            embedding_store=EmbeddingStore(backend=InMemoryBackend()),
            interactional=InteractionalKind.MEMO_TO_SELF,
        )
        return result.drawer_id

    def test_job_completes(self) -> None:
        drawer_id = self._capture_drawer()
        job_id = request_erase("drawer", drawer_id, log_client=self.p["log"])

        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_id,
            log_client=self.p["log"],
        )
        job.run_to_completion()

        self.assertEqual(job.phase, "complete")
        self.assertGreater(job.scanned, 0)
        self.assertGreater(job.rewritten, 0)

    def test_tombstones_drawer_event(self) -> None:
        drawer_id = self._capture_drawer(transcript="SECRET TEXT")
        job_id = request_erase("drawer", drawer_id, log_client=self.p["log"])
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_id,
            log_client=self.p["log"],
        )
        job.run_to_completion()

        # Find the drawer_captured event in the log; it should be tombstoned
        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        drawer_evts = [
            (off, kind, payload)
            for off, kind, payload in rows
            if kind == "drawer_captured" and payload.get("drawer_id") == drawer_id
        ]
        self.assertEqual(len(drawer_evts), 1)
        _o, _k, payload = drawer_evts[0]

        self.assertTrue(payload.get(ERASURE_TOMBSTONE_MARKER))
        # Structural fields preserved
        self.assertEqual(payload["drawer_id"], drawer_id)
        self.assertNotEqual(payload["content_hash"], "")
        # User-content stripped
        self.assertEqual(payload.get("state_context"), {})
        self.assertEqual(payload.get("goal_markers"), [])

    def test_emits_completion_event(self) -> None:
        drawer_id = self._capture_drawer()
        job_id = request_erase("drawer", drawer_id, log_client=self.p["log"])
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_id,
            log_client=self.p["log"],
        )
        job.run_to_completion()

        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))
        completion_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "erase_completed" and payload.get("erasure_job_id") == job_id
        ]
        self.assertEqual(len(completion_evts), 1)
        evt = completion_evts[0]
        self.assertEqual(evt["target_kind"], "drawer")
        self.assertEqual(evt["target_id"], drawer_id)
        self.assertGreaterEqual(evt["events_tombstoned"], 1)

    def test_idempotent_re_run(self) -> None:
        """Running the job twice produces the same end state."""
        drawer_id = self._capture_drawer()
        job_id = request_erase("drawer", drawer_id, log_client=self.p["log"])

        job1 = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_id,
            log_client=self.p["log"],
        )
        job1.run_to_completion()

        # Build a fresh job for same target (simulates restart)
        job2 = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_id,
            log_client=self.p["log"],
        )
        job2.run_to_completion()

        # Second run should rewrite zero events because everything's
        # already tombstoned.
        self.assertEqual(job2.rewritten, 0)
        self.assertEqual(job2.phase, "complete")

    def test_does_not_tombstone_unrelated_drawers(self) -> None:
        """Erasing one drawer shouldn't touch another."""
        drawer_a = self._capture_drawer(transcript="content A")
        drawer_b = self._capture_drawer(transcript="content B")

        job_id = request_erase("drawer", drawer_a, log_client=self.p["log"])
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=drawer_a,
            log_client=self.p["log"],
        )
        job.run_to_completion()

        # drawer_a tombstoned, drawer_b intact
        log = self.p["log"]
        rows = list(log.read_range(0, log.current_offset() + 1))

        for _o, kind, payload in rows:
            if kind != "drawer_captured":
                continue
            did = payload.get("drawer_id")
            if did == drawer_a:
                self.assertTrue(payload.get(ERASURE_TOMBSTONE_MARKER))
            elif did == drawer_b:
                self.assertFalse(payload.get(ERASURE_TOMBSTONE_MARKER))


# =============================================================================
# Edge erasure
# =============================================================================


class TestEdgeErasure(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_erase_edge(self) -> None:
        log = self.p["log"]

        # Build two nodes and an edge
        a = make_entity_id()
        b = make_entity_id()
        for n in (a, b):
            log.append(NodeCreated(
                event_id=make_event_id_log(),
                recorded_at=1000,
                actor="test",
                node_id=n,
                node_kind="entity",
                properties={"name": f"node {n[-4:]}"},
            ))
        edge_id = make_edge_id()
        log.append(EdgeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            edge_id=edge_id,
            edge_kind="asserted_subject",
            source_node_id=a,
            target_node_id=b,
            properties={"role": "subject"},
        ))

        # Erase the edge
        job_id = request_erase("edge", edge_id, log_client=log)
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="edge",
            target_id=edge_id,
            log_client=log,
        )
        job.run_to_completion()

        # The edge_created event should be tombstoned
        rows = list(log.read_range(0, log.current_offset() + 1))
        edge_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "edge_created" and payload.get("edge_id") == edge_id
        ]
        self.assertEqual(len(edge_evts), 1)
        evt = edge_evts[0]
        self.assertTrue(evt.get(ERASURE_TOMBSTONE_MARKER))
        self.assertEqual(evt.get("properties"), {})  # stripped


# =============================================================================
# Snapshot eraser hook
# =============================================================================


class _CountingSnapshotEraser:
    """Test fixture: counts calls + how many snapshots it claims to
    have rewritten."""

    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.calls: list[tuple[str, str]] = []

    def erase_target_from_snapshots(
        self, target_kind: str, target_id: str,
    ) -> int:
        self.calls.append((target_kind, target_id))
        return self.count


class TestSnapshotEraserHook(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_calls_snapshot_eraser(self) -> None:
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id="nde_target",
            node_kind="entity",
            properties={"name": "target"},
        ))

        eraser = _CountingSnapshotEraser(count=2)
        job_id = request_erase("node", "nde_target", log_client=log)
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="node",
            target_id="nde_target",
            log_client=log,
            snapshot_eraser=eraser,
        )
        job.run_to_completion()

        self.assertEqual(eraser.calls, [("node", "nde_target")])

        # The completion event should report the snapshot count
        rows = list(log.read_range(0, log.current_offset() + 1))
        completion_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "erase_completed"
        ]
        self.assertEqual(len(completion_evts), 1)
        self.assertEqual(completion_evts[0]["snapshots_rewritten"], 2)


# =============================================================================
# Failure path
# =============================================================================


class _FailingSnapshotEraser:
    def erase_target_from_snapshots(self, *args, **kwargs):
        raise RuntimeError("snapshot eraser blew up")


class TestEraseFailure(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_failure_emits_erase_failed(self) -> None:
        log = self.p["log"]
        log.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id="nde_will_fail",
            node_kind="entity",
            properties={},
        ))

        job_id = request_erase("node", "nde_will_fail", log_client=log)
        job = EraseJob(
            erasure_job_id=job_id,
            target_kind="node",
            target_id="nde_will_fail",
            log_client=log,
            snapshot_eraser=_FailingSnapshotEraser(),
        )
        job.run_to_completion()

        self.assertEqual(job.phase, "failed")

        # EraseFailed in log
        rows = list(log.read_range(0, log.current_offset() + 1))
        failed_evts = [
            payload
            for _o, kind, payload in rows
            if kind == "erase_failed"
        ]
        self.assertEqual(len(failed_evts), 1)
        self.assertIn("snapshot eraser blew up", failed_evts[0]["failure_reason"])


if __name__ == "__main__":
    unittest.main()
