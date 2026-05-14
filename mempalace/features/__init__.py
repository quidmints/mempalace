"""
mempalace.features — typed feature catalog, compute, and persistence.

Features are named, typed values computed over master views and persisted
into a derived store. Rankers consume features by name; the registry makes
the contract explicit.
"""

from . import compute, persist, registry

__all__ = ["compute", "persist", "registry"]
