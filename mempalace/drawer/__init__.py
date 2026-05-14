"""
mempalace.drawer — drawer capture, facet extraction, amendment, collision.

Drawers are the substrate. Each drawer is a 5-facet bundle (verbatim text,
acoustic, semantic embedding, structural, social). Capture is the boundary
between the outside world and the palace; nothing else writes substrate.
"""

from . import amend, capture, collision, facets

__all__ = ["amend", "capture", "collision", "facets"]
