"""
Drawer amendment.

Substrate is append-only and immutable. When the user wants to "fix" a drawer
(corrected transcript after re-listening, additional context realized later,
recategorization), the amendment never rewrites the original drawer. Two
mechanisms are available:

  - **DrawerAmended event**: explicitly supersedes the previous content_hash
    with a new one. Used for transcript corrections where the *original
    capture* is being acknowledged as an imperfect record of the same
    underlying utterance. The previous content_hash is retained in the
    audit chain; views update to reflect the new hash but the original
    capture event is still visible in the log.

  - **InterpretationAssigned event**: changes a derived property of the
    drawer (interactional kind, goal_markers, social facet membership)
    without touching the substrate facets. Used for re-categorization
    after later context emerges.

Choosing between the two depends on what's being amended:
  - Verbatim text / acoustic / semantic facets       → DrawerAmended
  - Structural / social / interactional reclass.     → InterpretationAssigned

This file provides helper functions that emit the right event type given
what the caller wants to change.

Spec ref: Part 2 (substrate vs interpretation), Part 4 (drawer lifecycle)
"""

from __future__ import annotations

import time
from typing import Any

from ..log.client import LogClient, get_default_client
from ..schema.events import DrawerAmended, InterpretationAssigned
from ..schema.identifiers import make_event_id_log
from .collision import compute_content_hash


# =============================================================================
# Substrate amendment — emits DrawerAmended
# =============================================================================


def amend_transcript(
    *,
    drawer_id: str,
    new_transcript: str,
    previous_content_hash: str,
    reason: str,
    actor: str = "user",
    client: LogClient | None = None,
) -> str:
    """Amend a drawer's verbatim transcript.

    Use cases:
      - User re-listened to the audio and corrected a transcription error.
      - Speech-to-text v2 was run with better quality.

    Returns the new content_hash.

    Note: this does NOT regenerate the embedding or update the vector store.
    Re-embedding is the responsibility of the reconciliation sweeper, which
    sees the DrawerAmended event and queues an embedding refresh.
    """
    log = client or get_default_client()
    new_hash = compute_content_hash(new_transcript)
    now_ms = int(time.time() * 1000)
    event = DrawerAmended(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        drawer_id=drawer_id,
        new_content_hash=new_hash,
        previous_content_hash=previous_content_hash,
        reason=reason,
    )
    log.append(event)
    return new_hash


# =============================================================================
# Interpretation amendment — emits InterpretationAssigned
# =============================================================================


def reinterpret_field(
    *,
    drawer_id: str,
    field_name: str,
    new_value: Any,
    miner_pass_version: str = "user",
    confidence: float = 1.0,
    supersedes_event_id: str | None = None,
    actor: str = "user",
    client: LogClient | None = None,
) -> str:
    """Assign a new interpretation to a derived property of the drawer.

    `field_name` is one of the drawer's derived attributes:
      - "interactional"         → reclassify (memo → conversation, etc.)
      - "goal_markers"          → revise the goals visible in the drawer
      - "self_other_world"      → reclassify viewpoint
      - "direct_participants"   → social-facet revision
      - "subjects_of_discussion"
      - "implicit_references"
      - "audience"
      - "memory_type"           → autobiographical-knowledge classification

    Returns the event_id of the InterpretationAssigned event for chaining
    further supersedes references.

    The substrate (verbatim text, content_hash) is not touched.
    """
    log = client or get_default_client()
    now_ms = int(time.time() * 1000)
    event = InterpretationAssigned(
        event_id=make_event_id_log(now_ms),
        recorded_at=now_ms,
        actor=actor,
        node_id=drawer_id,
        field_name=field_name,
        new_value=new_value,
        supersedes_event_id=supersedes_event_id,
        miner_pass_version=miner_pass_version,
        confidence=confidence,
    )
    log.append(event)
    return event.event_id


__all__ = ["amend_transcript", "reinterpret_field"]
