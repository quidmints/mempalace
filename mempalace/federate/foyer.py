"""
FOYER — first-encounter rendered surface.

Per R3 §6.6 / Part 8.4: when a peer starts a session, they don't see
the entire palace; they see the FOYER, a rendered surface that:

  - Lists the palace's themes (with one-line summaries from canon)
  - Surfaces "today" — what the local palace is currently working on
  - Shows recent canonical promotions (what changed since last seen)
  - Provides handles to drill in (each item is a handle the peer can
    refine and resolve via the standard handle lifecycle)

The FOYER is a derived representation: it reads canon_set + the foyer
cache + recent promotion events, and projects to a peer-facing summary.

This module owns the projection. The actual cache is in
mempalace.derived.foyer_cache (Batch 7).

Spec ref: R3 §6.6, Part 8.4.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..derived.foyer_cache import FoyerCache, FoyerEntry


# =============================================================================
# FOYER surface
# =============================================================================


@dataclass
class FoyerThemeCard:
    """One theme as it appears on the FOYER."""

    theme_id: str
    name: str
    one_line_summary: str = ""
    drawer_count: int = 0
    last_active_ms: int = 0
    canonicality: float = 0.0


@dataclass
class FoyerNowItem:
    """A 'today' item — currently bursting / active in the palace."""

    handle_id: str
    surface: str                      # one-liner, no substrate text
    activity_weight: float = 0.0
    region: str = ""                  # canonicalized theme/period/event id


@dataclass
class FoyerRecentPromotion:
    """Recently-canonicalized item that may have changed since peer's
    last visit."""

    canonical_id: str
    surface: str
    promoted_at_ms: int = 0
    canonical_kind: str = ""          # "predicate" | "schema" | "theme" | ...


@dataclass
class FoyerSurface:
    """The full FOYER object served to a peer."""

    schema_version: str = "foyer.v1"
    palace_id: str = ""
    rendered_at_ms: int = 0
    themes: list[FoyerThemeCard] = field(default_factory=list)
    now_items: list[FoyerNowItem] = field(default_factory=list)
    recent_promotions: list[FoyerRecentPromotion] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)


# =============================================================================
# Renderer
# =============================================================================


def render_foyer(
    *,
    palace_id: str,
    foyer_cache: FoyerCache,
    canon_themes: Iterable[dict[str, Any]],
    now_handles: Iterable[dict[str, Any]],
    recent_promotions: Iterable[dict[str, Any]],
    max_themes: int = 12,
    max_now: int = 6,
    max_promotions: int = 8,
    now_ms: int | None = None,
) -> FoyerSurface:
    """Render a FOYER from the inputs.

    `canon_themes` is the list of canonical theme records from
    views.current_themes (with name + count + canonicality).

    `now_handles` is the list of handles currently considered active,
    surfaced from rank.dispatch outputs over the last hour.

    `recent_promotions` is the list of CanonicalPromoted records from
    views.canon_set since the peer's last_seen offset.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # ---- theme cards -------------------------------------------------------
    theme_cards: list[FoyerThemeCard] = []
    for t in canon_themes:
        tid = t.get("theme_id") or t.get("id")
        name = t.get("name", "")
        if not tid or not name:
            continue
        # Pull a cached one-liner if available; otherwise leave blank.
        # FoyerEntry.rendered_markdown holds the rendered surface; we
        # take the first line as the one-liner.
        cache_entry: FoyerEntry | None = foyer_cache.get(tid)
        summary = ""
        if cache_entry is not None and cache_entry.rendered_markdown:
            first_line = cache_entry.rendered_markdown.splitlines()[0].strip()
            # strip leading markdown header chars
            summary = first_line.lstrip("#").strip()
        theme_cards.append(
            FoyerThemeCard(
                theme_id=tid,
                name=name,
                one_line_summary=summary,
                drawer_count=int(t.get("drawer_count", 0) or 0),
                last_active_ms=int(t.get("last_active_ms", 0) or 0),
                canonicality=float(t.get("canonicality", 0.0) or 0.0),
            )
        )
    # Sort: highest canonicality first, then most recent
    theme_cards.sort(key=lambda c: (-c.canonicality, -c.last_active_ms))
    theme_cards = theme_cards[:max_themes]

    # ---- now items ---------------------------------------------------------
    now_items: list[FoyerNowItem] = []
    for h in now_handles:
        hid = h.get("handle_id", "")
        surface = h.get("surface", "")
        if not hid or not surface:
            continue
        now_items.append(
            FoyerNowItem(
                handle_id=hid,
                surface=surface,
                activity_weight=float(h.get("activity_weight", 0.0) or 0.0),
                region=str(h.get("region", "") or ""),
            )
        )
    now_items.sort(key=lambda i: -i.activity_weight)
    now_items = now_items[:max_now]

    # ---- recent promotions -------------------------------------------------
    recent: list[FoyerRecentPromotion] = []
    for p in recent_promotions:
        cid = p.get("canonical_id", "")
        surface = p.get("surface", "")
        if not cid:
            continue
        recent.append(
            FoyerRecentPromotion(
                canonical_id=cid,
                surface=surface,
                promoted_at_ms=int(p.get("promoted_at_ms", 0) or 0),
                canonical_kind=str(p.get("canonical_kind", "") or ""),
            )
        )
    recent.sort(key=lambda r: -r.promoted_at_ms)
    recent = recent[:max_promotions]

    # ---- next actions (suggested handles for the peer to refine) ----------
    next_actions: list[str] = []
    if now_items:
        next_actions.append("refine: " + now_items[0].handle_id)
    if recent:
        next_actions.append("explore: " + recent[0].canonical_id)
    if theme_cards:
        next_actions.append("browse_theme: " + theme_cards[0].theme_id)

    return FoyerSurface(
        palace_id=palace_id,
        rendered_at_ms=now_ms,
        themes=theme_cards,
        now_items=now_items,
        recent_promotions=recent,
        next_actions=next_actions,
    )


__all__ = [
    "FoyerNowItem",
    "FoyerRecentPromotion",
    "FoyerSurface",
    "FoyerThemeCard",
    "render_foyer",
]
