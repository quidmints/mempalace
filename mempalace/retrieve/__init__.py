"""
mempalace.retrieve — handle protocol, scope expansion, candidate gather, fidelity.

Retrieval surfaces candidates by scope; rankers (in mempalace.rank) sort
them. The handle protocol is stateful per Conway's iterative-retrieval
pattern (R3 §9.2): allocate, resolve, refine, resolve again, close.
"""

from .fidelity import Fidelity, RenderedCandidate, render_all
from .gather import Candidate, GatherResult, gather
from .handle import (
    HandleManager,
    HandleState,
    RefinementSignal,
    get_handle_manager,
    mem_allocate,
    mem_close,
    mem_refine,
    mem_resolve,
)
from .scope import Scope, expand

__all__ = [
    "Candidate",
    "Fidelity",
    "GatherResult",
    "HandleManager",
    "HandleState",
    "RefinementSignal",
    "RenderedCandidate",
    "Scope",
    "expand",
    "gather",
    "get_handle_manager",
    "mem_allocate",
    "mem_close",
    "mem_refine",
    "mem_resolve",
    "render_all",
]
