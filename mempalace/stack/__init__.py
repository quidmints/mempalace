"""
mempalace.stack — the unified stacking framework.

Per R3 §1, six different consumers in the system are doing structurally
identical work (ranker stacking, inference stacking, miner pass stacking,
federation matching layers, composition, wake-up). The framework here
implements that pattern once; each domain specializes.
"""

from .aggregator import (
    AggregationKind,
    AggregationSpec,
    TrustedAggregator,
    aggregate,
)
from .attest import AttestedStep
from .context import (
    AttestationHandle,
    AttestationRecord,
    PrivacyMode,
    StackContext,
)
from .stack import Stack, StackResult, ValidationError
from .step import BaseStep, FunctionStep, Step, StepManifest, StepResult

__all__ = [
    "AggregationKind",
    "AggregationSpec",
    "AttestationHandle",
    "AttestationRecord",
    "AttestedStep",
    "BaseStep",
    "FunctionStep",
    "PrivacyMode",
    "Stack",
    "StackContext",
    "StackResult",
    "Step",
    "StepManifest",
    "StepResult",
    "TrustedAggregator",
    "ValidationError",
    "aggregate",
]
