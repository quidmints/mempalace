"""
mempalace.federate.layers — layered triangulation matching.

Per R3 §9.5: matching runs as a sequence of three increasingly-revealing
layers. Each layer's gate must pass before the next runs.

Layer 1 (structural)   — minhash + schema overlap + velocity. Public.
Layer 2 (derivation)   — CCGraph similarity over derivation chains.
Layer 3 (substrate)    — verbatim/semantic/paralinguistic. Sandbox only.

Spec ref: R3 §9.5, Part 9.2.
"""

from .derivation import (
    AssertionOverlap,
    CCGraphSketch,
    DerivationGraphSimilarity,
    DerivationLayer,
    RPathOverlap,
)
from .structural import StructuralLayerOutputs, StructuralMatchingLayer
from .substrate import (
    ParalinguisticSimilarity,
    SemanticSimilarity,
    SubstrateLayer,
    VerbatimSimilarity,
)

__all__ = [
    "AssertionOverlap",
    "CCGraphSketch",
    "DerivationGraphSimilarity",
    "DerivationLayer",
    "ParalinguisticSimilarity",
    "RPathOverlap",
    "SemanticSimilarity",
    "StructuralLayerOutputs",
    "StructuralMatchingLayer",
    "SubstrateLayer",
    "VerbatimSimilarity",
]
