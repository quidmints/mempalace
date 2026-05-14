"""
FOYER renderer cache.

Per Part 8.2: FOYER files are the rendered, human-readable view of the
canon set. They're consumed by Claude in the FOYER consumer kind (low-
latency, high-canonicality, no-exploration retrieval). FOYER files
update when:

  - the canon_set changes (a new theme/period becomes canonical, or one
    is decanonicalized)
  - canonical assertions get new substrate (drawers anchoring them
    change)
  - canonical schemas get refined

Re-rendering is expensive (template rendering, Markdown formatting,
optional embedding refresh) so we cache the rendered files keyed by
canon_node_id.

This module caches the rendered Markdown blobs in memory and writes
them to disk under a configurable path. Real disk writes are gated
behind `enable_disk_persistence`; in dev/test we keep them in memory.

Spec ref: Part 8.2.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..views.current import _get_store
from .base import DerivedRepresentation


@dataclass
class FoyerEntry:
    """A rendered FOYER file."""

    canon_node_id: str
    rendered_markdown: str
    rendered_at_ms: int
    inputs_hash: str
    on_disk_path: str | None = None


# =============================================================================
# Cache
# =============================================================================


class FoyerCache(DerivedRepresentation):
    """Re-renders FOYER files when canon set or canonical content changes."""

    name = "derived.foyer_cache"
    subscribed_kinds = (
        "node_property_set",      # canon flag flipped
        "edge_created",
        "edge_invalidated",
        "interpretation_assigned",
        "schema_induced",
    )

    def __init__(
        self,
        *,
        output_dir: str | None = None,
        enable_disk_persistence: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._entries: dict[str, FoyerEntry] = {}
        self._cache_lock = threading.Lock()
        self._output_dir = Path(output_dir) if output_dir else None
        self._disk_enabled = enable_disk_persistence

    def reset_state(self) -> None:
        with self._cache_lock:
            self._entries.clear()

    # ---- subscriber: invalidate on canon-touching events --------------------

    def apply(self, offset: int, kind: str, payload: dict) -> None:
        affected_id = payload.get("node_id") or payload.get("source_node_id")
        if not affected_id:
            return

        store = _get_store()
        # Only invalidate if the affected node is a canonical theme/period
        node = store.nodes.get(affected_id)
        if node is None:
            return
        if not node.canonical and affected_id not in store.canonicals:
            return

        with self._cache_lock:
            self._entries.pop(affected_id, None)

    # ---- render ------------------------------------------------------------

    def render(self, canon_node_id: str, *, force: bool = False) -> FoyerEntry | None:
        """Render or return cached FOYER file for a canonical node."""
        with self._cache_lock:
            existing = self._entries.get(canon_node_id)
        if existing and not force:
            return existing

        store = _get_store()
        node = store.nodes.get(canon_node_id)
        if node is None or not node.canonical:
            return None

        # Build the rendered Markdown. Format:
        #   # <name>
        #   _canonical | importance: <imp>_
        #   <description>
        #
        #   ## Contained
        #   - <child name> (<child kind>)
        #   ...
        name = node.properties.get("name", canon_node_id)
        imp = node.importance
        desc = node.properties.get("description", "")
        children: list[str] = []
        for eid in store.outgoing.get(canon_node_id, []):
            edge = store.edges.get(eid)
            if edge is None or edge.edge_kind != "contains" or not edge.is_active():
                continue
            child = store.nodes.get(edge.target_node_id)
            if child is None:
                continue
            child_name = child.properties.get("name", edge.target_node_id)
            children.append(f"- {child_name} ({child.node_kind})")

        rendered = (
            f"# {name}\n"
            f"_canonical | importance: {imp:.2f}_\n\n"
            f"{desc}\n\n"
        )
        if children:
            rendered += "## Contained\n" + "\n".join(children) + "\n"

        # Inputs hash
        h = hashlib.sha256()
        h.update(name.encode("utf-8"))
        h.update(str(imp).encode("utf-8"))
        h.update(desc.encode("utf-8"))
        h.update("\n".join(children).encode("utf-8"))
        inputs_hash = h.hexdigest()[:16]

        on_disk: str | None = None
        if self._disk_enabled and self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            file_path = self._output_dir / f"{canon_node_id}.md"
            file_path.write_text(rendered, encoding="utf-8")
            on_disk = str(file_path)

        entry = FoyerEntry(
            canon_node_id=canon_node_id,
            rendered_markdown=rendered,
            rendered_at_ms=int(time.time() * 1000),
            inputs_hash=inputs_hash,
            on_disk_path=on_disk,
        )
        with self._cache_lock:
            self._entries[canon_node_id] = entry
        return entry

    def render_all_canon(self) -> list[FoyerEntry]:
        """Render every canonical node currently in the view."""
        store = _get_store()
        canonical_ids = list(store.canonicals)
        out: list[FoyerEntry] = []
        for cid in canonical_ids:
            entry = self.render(cid)
            if entry is not None:
                out.append(entry)
        return out

    def get(self, canon_node_id: str) -> FoyerEntry | None:
        with self._cache_lock:
            return self._entries.get(canon_node_id)

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._entries)


__all__ = ["FoyerCache", "FoyerEntry"]
