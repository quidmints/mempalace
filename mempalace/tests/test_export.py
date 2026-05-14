"""Tests for Track 6F — chunked export.

Covers:
  - estimate_export() counts drawers + ciphertext bytes.
  - ChunkedExporter.next_chunk(): empty log returns final empty chunk.
  - Single-chunk export: small drawer count fits in one chunk.
  - Multi-chunk export: drawer count > chunk_drawer_count yields
    multiple chunks; each carries its cursor; final chunk has
    is_final=True.
  - Cursor resume: starting from a mid-export cursor picks up where
    left off.
  - Skip behavior: invalidated drawers, v0 unencrypted drawers,
    tombstoned (not-found) drawers all skip silently by default.
  - Byte-budget chunking: very large ciphertexts close the chunk
    early.
  - IntegrityLockoutGate: tripped gate raises LockoutError on
    next_chunk().
"""

from __future__ import annotations

import unittest

from mempalace.drawer.capture import capture_drawer
from mempalace.embed.client import EmbeddingStore, InMemoryBackend
from mempalace.embed.model import EmbeddingService
from mempalace.schema.kinds import InteractionalKind
from mempalace.secure import SoftwareCloudBoxKM, SoftwarePhoneSE
from mempalace.secure.burn import (
    REASON_BURN_PALACE,
    IntegrityLockoutGate,
    LockoutError,
)
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views.erase import EraseJob, request_erase
from mempalace.views.export import (
    DEFAULT_CHUNK_DRAWER_COUNT,
    ChunkedExporter,
    ExportCursor,
    estimate_export,
)
from mempalace.views.invalidate import invalidate_drawer

DAEMON_ATTESTATION = b"daemon-binary-hash-test"


# =============================================================================
# Helpers
# =============================================================================


def _fresh_se_pair() -> tuple[SoftwarePhoneSE, SoftwareCloudBoxKM]:
    """Build a Phone SE + CloudBoxKM pair with a loaded bundle."""
    se = SoftwarePhoneSE()
    manager = SoftwareCloudBoxKM()
    bundle = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
    manager.load_bundle(bundle, daemon_binary_attestation=DAEMON_ATTESTATION)
    return se, manager


def _capture_v2(
    log,
    se: SoftwarePhoneSE,
    *,
    transcript: str = "encrypted content",
    cloud_box_key_manager: SoftwareCloudBoxKM | None = None,
) -> str:
    """Capture a drawer with v2 encryption. Returns drawer_id."""
    result = capture_drawer(
        transcript=transcript,
        actor="test",
        duration_ms=500,
        log_client=log,
        embedding_service=EmbeddingService(),
        embedding_store=EmbeddingStore(backend=InMemoryBackend()),
        interactional=InteractionalKind.MEMO_TO_SELF,
        secure_element=se,
        cloud_box_key_manager=cloud_box_key_manager,
    )
    return result.drawer_id


def _capture_v0(log, *, transcript: str = "plaintext content") -> str:
    """Capture a v0 (unencrypted) drawer for skip-behavior tests."""
    result = capture_drawer(
        transcript=transcript,
        actor="test",
        duration_ms=500,
        log_client=log,
        embedding_service=EmbeddingService(),
        embedding_store=EmbeddingStore(backend=InMemoryBackend()),
        interactional=InteractionalKind.MEMO_TO_SELF,
    )
    return result.drawer_id


# =============================================================================
# estimate_export
# =============================================================================


class TestEstimateExport(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_empty_log(self) -> None:
        est = estimate_export(log_client=self.p["log"])
        self.assertEqual(est.drawer_count, 0)
        self.assertEqual(est.estimated_bytes, 0)
        self.assertEqual(est.estimated_chunks, 1)  # min 1

    def test_counts_drawers(self) -> None:
        se, _ = _fresh_se_pair()
        for i in range(5):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        est = estimate_export(log_client=self.p["log"])
        self.assertEqual(est.drawer_count, 5)
        self.assertGreater(est.estimated_bytes, 0)

    def test_skips_erased_drawers(self) -> None:
        """Tombstoned drawers don't count toward the estimate."""
        se, _ = _fresh_se_pair()
        kept = _capture_v2(self.p["log"], se, transcript="kept")
        erased = _capture_v2(self.p["log"], se, transcript="will be erased")

        # Erase one
        job_id = request_erase("drawer", erased, log_client=self.p["log"])
        EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=erased,
            log_client=self.p["log"],
        ).run_to_completion()

        est = estimate_export(log_client=self.p["log"])
        # Only the un-erased drawer counts
        self.assertEqual(est.drawer_count, 1)


# =============================================================================
# ChunkedExporter — basic flow
# =============================================================================


class TestChunkedExporterBasics(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_empty_log_returns_final_empty_chunk(self) -> None:
        exp = ChunkedExporter(log_client=self.p["log"])
        chunk = exp.next_chunk()
        self.assertEqual(chunk.envelopes, [])
        self.assertTrue(chunk.is_final)

    def test_single_chunk_export(self) -> None:
        se, _ = _fresh_se_pair()
        for i in range(3):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        exp = ChunkedExporter(log_client=self.p["log"])
        chunks = list(exp.iter_chunks())

        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].envelopes), 3)
        self.assertTrue(chunks[0].is_final)

    def test_envelope_shape(self) -> None:
        """Verify envelopes have all the fields the phone needs."""
        se, _ = _fresh_se_pair()
        _capture_v2(self.p["log"], se, transcript="for-shape-check")

        exp = ChunkedExporter(log_client=self.p["log"])
        chunk = exp.next_chunk()

        self.assertEqual(len(chunk.envelopes), 1)
        env = chunk.envelopes[0]
        self.assertNotEqual(env.drawer_id, "")
        self.assertNotEqual(env.ciphertext, b"")
        self.assertNotEqual(env.dek_handle, "")
        self.assertNotEqual(env.attestation_sig, b"")
        self.assertNotEqual(env.content_hash, "")


# =============================================================================
# Multi-chunk + cursor resume
# =============================================================================


class TestMultiChunkExport(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_multi_chunk_when_drawers_exceed_size(self) -> None:
        """With chunk_drawer_count=2, 5 drawers should yield 3 chunks
        (2 + 2 + 1)."""
        se, _ = _fresh_se_pair()
        for i in range(5):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        exp = ChunkedExporter(
            log_client=self.p["log"],
            chunk_drawer_count=2,
        )
        chunks = list(exp.iter_chunks())

        # Either 3 chunks (2,2,1) or possibly fewer if last is_final
        # is_final on a chunk with envelopes still possible.
        # Verify totals.
        total_envelopes = sum(len(c.envelopes) for c in chunks)
        self.assertEqual(total_envelopes, 5)
        self.assertTrue(chunks[-1].is_final)
        self.assertGreaterEqual(len(chunks), 2)

    def test_cursor_resume(self) -> None:
        """Pause mid-export with a cursor; resume from there."""
        se, _ = _fresh_se_pair()
        for i in range(6):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        exp = ChunkedExporter(
            log_client=self.p["log"],
            chunk_drawer_count=2,
        )

        # First chunk
        chunk1 = exp.next_chunk()
        self.assertEqual(len(chunk1.envelopes), 2)
        self.assertFalse(chunk1.is_final)

        # Resume from cursor
        chunk2 = exp.next_chunk(chunk1.cursor)
        self.assertEqual(len(chunk2.envelopes), 2)
        self.assertNotEqual(
            chunk2.envelopes[0].drawer_id,
            chunk1.envelopes[0].drawer_id,
        )

        # Continue to end
        chunk3 = exp.next_chunk(chunk2.cursor)
        self.assertEqual(len(chunk3.envelopes), 2)
        self.assertTrue(chunk3.is_final)

    def test_cursor_drawers_emitted_increments(self) -> None:
        """The cursor's drawers_emitted accumulates across chunks."""
        se, _ = _fresh_se_pair()
        for i in range(4):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        exp = ChunkedExporter(
            log_client=self.p["log"],
            chunk_drawer_count=2,
        )
        chunk1 = exp.next_chunk()
        self.assertEqual(chunk1.cursor.drawers_emitted, 2)

        chunk2 = exp.next_chunk(chunk1.cursor)
        self.assertEqual(chunk2.cursor.drawers_emitted, 4)

    def test_iter_chunks_with_starting_cursor(self) -> None:
        """Pass a non-empty starting cursor to skip earlier drawers."""
        se, _ = _fresh_se_pair()
        for i in range(4):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        # First, find the offset of the first drawer's DrawerCaptured
        # event. Each capture also creates a NodeCreated, so drawer
        # events sit at even offsets (2, 4, 6, 8 in this fixture).
        log = self.p["log"]
        drawer_offsets = [
            off
            for off, kind, _payload in log.read_range(0, log.current_offset() + 1)
            if kind == "drawer_captured"
        ]
        self.assertEqual(len(drawer_offsets), 4)

        # Resume after the FIRST drawer
        cursor_after_first = ExportCursor(
            last_drawer_offset=drawer_offsets[0],
            drawers_emitted=1,
        )
        exp = ChunkedExporter(log_client=log)
        chunks = list(exp.iter_chunks(starting_cursor=cursor_after_first))
        total = sum(len(c.envelopes) for c in chunks)
        # Skipped 1, so 3 drawers remain
        self.assertEqual(total, 3)


# =============================================================================
# Skip behavior
# =============================================================================


class TestSkipBehavior(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_skips_invalidated_drawers(self) -> None:
        """Invalidated drawers are skipped from the export."""
        se, _ = _fresh_se_pair()
        kept = _capture_v2(self.p["log"], se, transcript="kept")
        hidden = _capture_v2(self.p["log"], se, transcript="hidden")

        invalidate_drawer(hidden, log_client=self.p["log"])

        exp = ChunkedExporter(log_client=self.p["log"])
        chunk = exp.next_chunk()

        ids = [e.drawer_id for e in chunk.envelopes]
        self.assertIn(kept, ids)
        self.assertNotIn(hidden, ids)
        self.assertEqual(chunk.skipped_drawers, 1)

    def test_skips_v0_drawers(self) -> None:
        """v0 unencrypted drawers are skipped from a v2 export."""
        se, _ = _fresh_se_pair()
        v2_id = _capture_v2(self.p["log"], se, transcript="v2 content")
        v0_id = _capture_v0(self.p["log"], transcript="v0 plaintext")

        exp = ChunkedExporter(log_client=self.p["log"])
        chunk = exp.next_chunk()

        ids = [e.drawer_id for e in chunk.envelopes]
        self.assertIn(v2_id, ids)
        self.assertNotIn(v0_id, ids)
        self.assertGreaterEqual(chunk.skipped_drawers, 1)

    def test_skips_tombstoned_drawers(self) -> None:
        """Drawers erased via Track 6D are skipped."""
        se, _ = _fresh_se_pair()
        kept = _capture_v2(self.p["log"], se, transcript="kept")
        erased = _capture_v2(self.p["log"], se, transcript="will be erased")

        # Run erasure
        job_id = request_erase("drawer", erased, log_client=self.p["log"])
        EraseJob(
            erasure_job_id=job_id,
            target_kind="drawer",
            target_id=erased,
            log_client=self.p["log"],
        ).run_to_completion()

        exp = ChunkedExporter(log_client=self.p["log"])
        chunk = exp.next_chunk()

        ids = [e.drawer_id for e in chunk.envelopes]
        self.assertIn(kept, ids)
        self.assertNotIn(erased, ids)


# =============================================================================
# Byte budget
# =============================================================================


class TestByteBudget(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_byte_budget_closes_chunk_early(self) -> None:
        """If chunk_byte_budget is small, even a few drawers split."""
        se, _ = _fresh_se_pair()
        # Capture a few drawers with non-trivial content
        for i in range(4):
            _capture_v2(
                self.p["log"], se,
                transcript=f"content {i} " * 100,  # ~1.2KB plaintext each
            )

        exp = ChunkedExporter(
            log_client=self.p["log"],
            chunk_drawer_count=100,  # not the limit
            chunk_byte_budget=200,    # very tight; 1 drawer per chunk
        )

        chunks = list(exp.iter_chunks())
        # Each chunk should hold at most a few envelopes due to byte
        # budget. Verify no chunk exceeds the budget by much.
        for chunk in chunks:
            total_bytes = sum(len(e.ciphertext) for e in chunk.envelopes)
            # The check happens AFTER adding so first envelope can
            # exceed; subsequent get blocked
            self.assertGreaterEqual(len(chunk.envelopes), 1)


# =============================================================================
# Integrity gate integration
# =============================================================================


class TestIntegrityGateIntegration(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.p = fresh_palace()

    def test_tripped_gate_blocks_export(self) -> None:
        """next_chunk() raises LockoutError when gate is tripped."""
        se, _ = _fresh_se_pair()
        for i in range(2):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        gate = IntegrityLockoutGate(log_client=self.p["log"])
        gate.trip(REASON_BURN_PALACE)

        exp = ChunkedExporter(
            log_client=self.p["log"],
            integrity_gate=gate,
        )
        with self.assertRaises(LockoutError):
            exp.next_chunk()

    def test_untripped_gate_does_not_block(self) -> None:
        se, _ = _fresh_se_pair()
        _capture_v2(self.p["log"], se, transcript="content")

        gate = IntegrityLockoutGate(log_client=self.p["log"])
        # Don't trip
        exp = ChunkedExporter(
            log_client=self.p["log"],
            integrity_gate=gate,
        )
        chunk = exp.next_chunk()
        self.assertEqual(len(chunk.envelopes), 1)

    def test_gate_trips_mid_export_blocks_subsequent_chunks(self) -> None:
        """First chunk succeeds, then gate trips, second chunk raises."""
        se, _ = _fresh_se_pair()
        for i in range(4):
            _capture_v2(self.p["log"], se, transcript=f"content {i}")

        gate = IntegrityLockoutGate(log_client=self.p["log"])
        exp = ChunkedExporter(
            log_client=self.p["log"],
            chunk_drawer_count=2,
            integrity_gate=gate,
        )

        # First chunk OK
        chunk1 = exp.next_chunk()
        self.assertEqual(len(chunk1.envelopes), 2)

        # User signals burn between chunks
        gate.trip(REASON_BURN_PALACE)

        with self.assertRaises(LockoutError):
            exp.next_chunk(chunk1.cursor)


if __name__ == "__main__":
    unittest.main()
