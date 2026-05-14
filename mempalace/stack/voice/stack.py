"""
VoiceStack composition.

Per VOICE_STACK_DESIGN.md §"Stack composition", the six steps run in
order: ASR → diarization → speaker matching → prosody/affect →
accent → paralinguistic events. The first three feed each other
directly; the last three could parallelize but the linear ordering is
fine for the stub.

# Three execution profiles

Per VOICE_STACK_DESIGN.md §"What runs when":

  - **At capture** (online, latency-sensitive): only ASR.
  - **Class 1 miner pass** (near-real-time): ASR + diarization +
    speaker matching.
  - **Class 2/3 miner pass** (offline batched): the full stack.

This module provides three constructors (`asr_only_stack`,
`class_1_stack`, `full_stack`) so callers can pick the right depth
without rebuilding the stack manually.
"""

from __future__ import annotations

from ..stack import Stack
from .steps import (
    ASRStep,
    AccentStep,
    DiarizationStep,
    ParalinguisticStep,
    ProsodyAffectStep,
    SpeakerMatchStep,
)


def asr_only_stack() -> Stack:
    """Capture-time stack: just ASR. Latency-sensitive; everything
    else deferred to later miner passes."""
    return Stack(
        plan=[ASRStep()],
        name="voice.asr_only",
    )


def class_1_stack() -> Stack:
    """Near-real-time stack: ASR + diarization + speaker matching.
    Adds the structural enrichment Class 2 will need."""
    return Stack(
        plan=[
            ASRStep(),
            DiarizationStep(),
            SpeakerMatchStep(),
        ],
        name="voice.class_1",
    )


def full_stack() -> Stack:
    """Class 2/3 batch stack: every step. Run offline."""
    return Stack(
        plan=[
            ASRStep(),
            DiarizationStep(),
            SpeakerMatchStep(),
            ProsodyAffectStep(),
            AccentStep(),
            ParalinguisticStep(),
        ],
        name="voice.full",
    )


__all__ = ["asr_only_stack", "class_1_stack", "full_stack"]
