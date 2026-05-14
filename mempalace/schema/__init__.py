"""
mempalace.schema — type definitions, event taxonomy, validators.

This package is the single source of truth for the system's data shape. Every
node kind, edge kind, event kind, facet, and stance is defined here. No
runtime logic beyond validation; consumers are in other packages.
"""

from . import events, facets, identifiers, kinds, stance, validators

__all__ = ["events", "facets", "identifiers", "kinds", "stance", "validators"]
