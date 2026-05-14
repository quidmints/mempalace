"""Track 1A — voice-schema additions.

Verifies the new schema additions:
  - VoiceStepCompleted, TokenFeaturesWritten, SegmentCreated events.
  - drawer_has_segment, voice_matches_reference,
    paralinguistic_event_at, interpretation_memo_for,
    interpretation_memo_for_segment edge kinds.

The events are registered in EVENT_KIND_TO_CLASS and serialize
through the log roundtrip cleanly. Edges of the new kinds flow
through the existing log without special handling.
"""

from __future__ import annotations

import unittest

from mempalace.log.client import LogClient
from mempalace.schema.events import (
    EVENT_KIND_TO_CLASS,
    EdgeCreated,
    SegmentCreated,
    TokenFeaturesWritten,
    VoiceStepCompleted,
)
from mempalace.schema.identifiers import (
    make_drawer_id,
    make_edge_id,
    make_entity_id,
    make_event_id_log,
    make_id,
)
from mempalace.schema.kinds import EdgeKind
from mempalace.tests.conftest import reset_module_state


class TestVoiceEventRegistration(unittest.TestCase):
    """The three new event types are registered."""

    def test_voice_step_completed_registered(self) -> None:
        self.assertIn("voice_step_completed", EVENT_KIND_TO_CLASS)
        self.assertIs(
            EVENT_KIND_TO_CLASS["voice_step_completed"], VoiceStepCompleted
        )

    def test_token_features_written_registered(self) -> None:
        self.assertIn("token_features_written", EVENT_KIND_TO_CLASS)
        self.assertIs(
            EVENT_KIND_TO_CLASS["token_features_written"], TokenFeaturesWritten
        )

    def test_segment_created_registered(self) -> None:
        self.assertIn("segment_created", EVENT_KIND_TO_CLASS)
        self.assertIs(EVENT_KIND_TO_CLASS["segment_created"], SegmentCreated)


class TestVoiceEventLogRoundtrip(unittest.TestCase):
    """Voice events append to the log and read back cleanly."""

    def setUp(self) -> None:
        reset_module_state()

    def test_voice_step_completed_roundtrip(self) -> None:
        log = LogClient()
        drawer_id = make_drawer_id()
        evt = VoiceStepCompleted(
            event_id=make_event_id_log(1000),
            recorded_at=1000,
            actor="voice.asr",
            drawer_id=drawer_id,
            step_id="voice.asr",
            model_pass_version="whisper-large-v3@2026-01-15",
            output_summary={"token_count": 142, "language": "en"},
            completed_at_ms=1000,
        )
        log.append(evt)
        # Re-read
        end = log.current_offset() + 1
        rows = list(log.read_range(0, end))
        kinds = [k for _o, k, _p in rows]
        self.assertIn("voice_step_completed", kinds)

        # Check payload survived
        for _o, kind, payload in rows:
            if kind != "voice_step_completed":
                continue
            self.assertEqual(payload["drawer_id"], drawer_id)
            self.assertEqual(payload["step_id"], "voice.asr")
            self.assertEqual(payload["output_summary"]["token_count"], 142)

    def test_token_features_written_roundtrip(self) -> None:
        log = LogClient()
        drawer_id = make_drawer_id()
        evt = TokenFeaturesWritten(
            event_id=make_event_id_log(1000),
            recorded_at=1000,
            actor="voice.asr",
            drawer_id=drawer_id,
            token_count=50,
            features_blob_ref=f"blob://features/{drawer_id}/v1",
            produced_by_model_passes={
                "tokens": "voice.asr@whisper-large-v3",
                "speaker_label": "voice.diarization@pyannote-3",
            },
            written_at_ms=1000,
        )
        log.append(evt)
        end = log.current_offset() + 1
        rows = list(log.read_range(0, end))
        for _o, kind, payload in rows:
            if kind != "token_features_written":
                continue
            self.assertEqual(payload["token_count"], 50)
            self.assertEqual(
                payload["produced_by_model_passes"]["tokens"],
                "voice.asr@whisper-large-v3",
            )

    def test_segment_created_roundtrip(self) -> None:
        log = LogClient()
        segment_id = make_id("seg")
        drawer_id = make_drawer_id()
        evt = SegmentCreated(
            event_id=make_event_id_log(1000),
            recorded_at=1000,
            actor="voice.diarization",
            segment_id=segment_id,
            drawer_id=drawer_id,
            start_ms=0,
            end_ms=5000,
            created_at_ms=1000,
        )
        log.append(evt)
        end = log.current_offset() + 1
        rows = list(log.read_range(0, end))
        for _o, kind, payload in rows:
            if kind != "segment_created":
                continue
            self.assertEqual(payload["segment_id"], segment_id)
            self.assertEqual(payload["start_ms"], 0)
            self.assertEqual(payload["end_ms"], 5000)


class TestVoiceEdgeKinds(unittest.TestCase):
    """The new edge kinds exist and pass through the log."""

    def test_drawer_has_segment_kind_exists(self) -> None:
        self.assertEqual(EdgeKind.DRAWER_HAS_SEGMENT.value, "drawer_has_segment")

    def test_voice_matches_reference_kind_exists(self) -> None:
        self.assertEqual(
            EdgeKind.VOICE_MATCHES_REFERENCE.value, "voice_matches_reference"
        )

    def test_paralinguistic_event_at_kind_exists(self) -> None:
        self.assertEqual(
            EdgeKind.PARALINGUISTIC_EVENT_AT.value, "paralinguistic_event_at"
        )

    def test_interpretation_memo_for_kind_exists(self) -> None:
        self.assertEqual(
            EdgeKind.INTERPRETATION_MEMO_FOR.value, "interpretation_memo_for"
        )

    def test_interpretation_memo_for_segment_kind_exists(self) -> None:
        self.assertEqual(
            EdgeKind.INTERPRETATION_MEMO_FOR_SEGMENT.value,
            "interpretation_memo_for_segment",
        )

    def test_drawer_has_segment_edge_logs_correctly(self) -> None:
        """A drawer_has_segment edge appends + reads through the
        existing edge_created event mechanism."""
        reset_module_state()
        log = LogClient()
        drawer_id = make_drawer_id()
        segment_id = make_id("seg")
        edge = EdgeCreated(
            event_id=make_event_id_log(1000),
            recorded_at=1000,
            actor="voice.diarization",
            edge_id=make_edge_id(),
            edge_kind=EdgeKind.DRAWER_HAS_SEGMENT.value,
            source_node_id=drawer_id,
            target_node_id=segment_id,
        )
        result = log.append(edge)
        self.assertTrue(result.accepted, msg=f"validation failed: {result.validation}")
        end = log.current_offset() + 1
        rows = list(log.read_range(0, end))
        for _o, kind, payload in rows:
            if kind != "edge_created":
                continue
            self.assertEqual(payload["edge_kind"], "drawer_has_segment")

    def test_voice_matches_reference_edge_logs_correctly(self) -> None:
        reset_module_state()
        log = LogClient()
        segment_id = make_id("seg")
        entity_id = make_entity_id()
        edge = EdgeCreated(
            event_id=make_event_id_log(1001),
            recorded_at=1001,
            actor="voice.speaker_match",
            edge_id=make_edge_id(),
            edge_kind=EdgeKind.VOICE_MATCHES_REFERENCE.value,
            source_node_id=segment_id,
            target_node_id=entity_id,
        )
        result = log.append(edge)
        self.assertTrue(result.accepted, msg=f"validation failed: {result.validation}")
        end = log.current_offset() + 1
        rows = list(log.read_range(0, end))
        found = False
        for _o, kind, payload in rows:
            if kind == "edge_created" and payload["edge_kind"] == (
                "voice_matches_reference"
            ):
                self.assertEqual(payload["target_node_id"], entity_id)
                found = True
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
