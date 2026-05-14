"""
mempalace.stack.voice — voice-stack module (Track 1A-B).

Implements VOICE_STACK_DESIGN.md as a sibling of `mempalace.stack.text`
patterns. Six steps composing into per-token + per-segment substrate
enrichment.

# Status

  - Track 1A (schema additions): done. See
    mempalace/schema/events.py and mempalace/schema/kinds.py.
  - Track 1B (stub steps + composition): done. This package.
  - Track 1C (real model integration): pending. Each step's
    `_run_sync` is currently a fixture-driven stub; real impls
    bind to Whisper / pyannote / etc.

# Imports

    from mempalace.stack.voice import (
        full_stack, class_1_stack, asr_only_stack,
        TokenFeatures, DrawerSegment, AffectDistribution,
    )

# Memo override discipline

ProsodyAffectStep honors `memo_overrides` from the StackContext —
a list of (start_ms, end_ms, override_dict) tuples. Tokens within an
override range get the memo's values stamped with
`produced_by_model_pass["affect"] = "memo_override"`. Per
HANDLES_DESIGN.md v2 §"Memos as override signals — segment-targeted",
memos are ground truth; inference defers.
"""

from .stack import (
    asr_only_stack,
    class_1_stack,
    full_stack,
)
from .steps import (
    ASRStep,
    AccentStep,
    DiarizationStep,
    ParalinguisticStep,
    ProsodyAffectStep,
    SpeakerMatchStep,
    VALID_EVENT_KINDS,
)
from .types import (
    AccentDistribution,
    AffectDistribution,
    DrawerSegment,
    ProsodyVector,
    TokenFeatures,
)

__all__ = [
    "ASRStep",
    "AccentDistribution",
    "AccentStep",
    "AffectDistribution",
    "DiarizationStep",
    "DrawerSegment",
    "ParalinguisticStep",
    "ProsodyAffectStep",
    "ProsodyVector",
    "SpeakerMatchStep",
    "TokenFeatures",
    "VALID_EVENT_KINDS",
    "asr_only_stack",
    "class_1_stack",
    "full_stack",
]
