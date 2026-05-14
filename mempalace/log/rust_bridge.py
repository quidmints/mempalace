"""
Python ↔ Rust frontier bridge.

Phase 5 shipped a Rust-side `FrontierRegistry` in
`mempalace_core::views::frontier`, exposed via PyO3 as
`mempalace_core.PyFrontierRegistry`. Until this module landed, the
Rust registry existed but nothing imported or fed it. This module is
the wire.

# What it does

When the `mempalace_core` extension is importable:

  - Maintains a process-wide `PyFrontierRegistry` instance.
  - Pushes batch lifecycle events to Rust as they happen (open / close
    / abort) via `LogClient.open_batch` / `close_batch` /
    `BatchHandle.__exit__`.
  - Pushes per-event applied offsets via `LogClient.append`.
  - Exposes `committed_offset(consumer_id)` and `meet(consumer_ids)`
    as O(1) reads against Rust state.
  - The Python `FrontierRegistry`'s `_refresh_locked` consults this
    bridge first and falls back to the scan-based path only if the
    bridge says the consumer is unknown to Rust.

When the extension is NOT importable (no `cargo build` has been run,
no rustc, dev environments, etc.):

  - All bridge methods are no-ops.
  - The Python `FrontierRegistry` runs entirely on the scan-based
    path, behaving exactly as it did before this wire existed.

The two implementations are required to agree on `committed_offset`
and `meet` semantics; the existing
`test_phase5_frontier_alignment` suite asserts this. With the bridge
live, a future test will assert the same outputs come from both
paths.

# What I can't validate from this environment

There's no Rust toolchain available where this file was written, so:

  - The exact signatures of the PyO3-generated Python class are
    inferred from the Rust source in
    `mempalace_core/src/pyo3/bindings.rs`. Method names match; the
    argument shapes are what PyO3 produces from the Rust signatures
    (String → str, u64 → int, Vec<String> → list[str]). If PyO3
    surprises us with a different boundary shape, that's a TODO.

  - I can't actually run `import mempalace_core` and confirm the
    class shows up. The structural test in `tests/test_rust_bridge.py`
    runs whether or not the extension is built; the integration
    tests skip when it's not built.

Every guess is marked `TODO(rust-build)` so it can be confirmed
or fixed once the extension actually compiles.

# Why a bridge instead of full delegation

The Python `FrontierRegistry` carries the public API; readers depend
on it. Routing every call through Rust would be more invasive without
buying anything — the Python implementation is already correct. The
bridge replaces only the *expensive* operation: the per-call log scan
in `_refresh_locked` becomes an O(1) Rust lookup when Rust is live.

The lazy cache in `FrontierRegistry._fresh` continues to apply; the
bridge is consulted on cache misses only.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Extension-availability probe
# =============================================================================

# TODO(rust-build): once `cargo build` runs and produces the extension,
# `import mempalace_core` should yield a module exposing
# `PyFrontierRegistry`. Confirm the class is at the top level of the
# module (i.e., `mempalace_core.PyFrontierRegistry`, not nested under
# a sub-module). The Rust `pyo3/bindings.rs::register` calls
# `m.add_class::<PyFrontierRegistry>()` on the top-level module, so
# top-level access is what we expect.

_RUST_AVAILABLE: bool = False
_RUST_REGISTRY_CLS: Any = None
_PROBE_LOCK = threading.Lock()
_PROBED: bool = False


def _probe_rust_extension() -> None:
    """Try to import mempalace_core. Idempotent; only probes once."""
    global _RUST_AVAILABLE, _RUST_REGISTRY_CLS, _PROBED
    with _PROBE_LOCK:
        if _PROBED:
            return
        _PROBED = True
        try:
            import mempalace_core  # type: ignore[import-not-found]
        except ImportError:
            logger.debug(
                "mempalace_core extension not built; "
                "frontier bridge will run in fallback mode "
                "(scan-based, no Rust acceleration). "
                "Build with `cargo build --release -p mempalace_core` "
                "to enable the Rust path."
            )
            return
        # TODO(rust-build): confirm the class name is exactly
        # PyFrontierRegistry. The Rust file declares
        # `#[pyclass] pub struct PyFrontierRegistry` and registers it
        # via `m.add_class::<PyFrontierRegistry>()`.
        cls = getattr(mempalace_core, "PyFrontierRegistry", None)
        if cls is None:
            # Distinguish two cases:
            #   1. The compiled extension is loaded but doesn't expose
            #      PyFrontierRegistry (real bug; warn loudly).
            #   2. Python found a namespace package (the Rust source
            #      dir on the path, no __init__.py) — the import
            #      "succeeded" but the module is empty. Common during
            #      development; warn quietly at debug level.
            module_file = getattr(mempalace_core, "__file__", None)
            module_path = list(getattr(mempalace_core, "__path__", []))
            is_namespace_package = module_file is None and bool(module_path)
            if is_namespace_package:
                logger.debug(
                    "mempalace_core matched as a namespace package "
                    "at %s (no compiled extension found). Falling "
                    "back to scan-based path. This is normal in "
                    "development; build with `cargo build` to wire "
                    "the Rust bridge.",
                    module_path,
                )
            else:
                logger.warning(
                    "mempalace_core imported (from %s) but "
                    "PyFrontierRegistry not found on the module. "
                    "The Rust bridge cannot be wired. Falling back "
                    "to scan-based path.",
                    module_file,
                )
            return
        _RUST_REGISTRY_CLS = cls
        _RUST_AVAILABLE = True
        logger.info("Rust frontier bridge live (PyFrontierRegistry found)")


def is_rust_available() -> bool:
    """True if `mempalace_core.PyFrontierRegistry` is importable.

    Safe to call repeatedly. The probe runs once on first call.
    """
    _probe_rust_extension()
    return _RUST_AVAILABLE


def reset_probe_for_testing() -> None:
    """Force a re-probe on next `is_rust_available()` call.

    Test hook only — exposed so unit tests can simulate the
    extension being absent or present.
    """
    global _PROBED, _RUST_AVAILABLE, _RUST_REGISTRY_CLS
    with _PROBE_LOCK:
        _PROBED = False
        _RUST_AVAILABLE = False
        _RUST_REGISTRY_CLS = None


# =============================================================================
# Bridge — process-wide adapter
# =============================================================================


@dataclass
class _BridgeState:
    """Captures whether the bridge is live and (if so) holds the
    Rust registry instance."""

    rust_registry: Any = None
    """An instance of `mempalace_core.PyFrontierRegistry`, or None."""

    consumer_offsets: dict[str, int] = None
    """Tracks the highest log offset seen per consumer locally.
    Used to feed `record_applied` to Rust without a separate scan."""

    open_batch_starts: dict[tuple[str, str], int] = None
    """(consumer_id, batch_id) → start_offset. We track these locally
    so we can pass `start_offset` to Rust on `record_batch_started`."""

    def __post_init__(self) -> None:
        if self.consumer_offsets is None:
            self.consumer_offsets = {}
        if self.open_batch_starts is None:
            self.open_batch_starts = {}


class FrontierBridge:
    """Thin adapter over `mempalace_core.PyFrontierRegistry`.

    All methods are no-ops when the extension isn't available. When
    it is, methods forward to Rust, with batch-aware committed_offset
    semantics.

    Thread-safe: holds an internal lock for state mutation.

    The bridge is consulted by `LogClient` (which forwards batch and
    append events) and by `FrontierRegistry` (which reads
    committed_offset / meet on cache misses).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _BridgeState()
        self._init_rust_if_available()

    def _init_rust_if_available(self) -> None:
        """Lazy-init the Rust registry when the extension is present."""
        if not is_rust_available():
            return
        try:
            # TODO(rust-build): the Rust constructor is `PyFrontierRegistry::new()`
            # taking no arguments. Confirm PyO3 surfaces it as
            # `PyFrontierRegistry()` with no positional arguments.
            self._state.rust_registry = _RUST_REGISTRY_CLS()
        except Exception as e:
            logger.warning(
                "Failed to instantiate Rust PyFrontierRegistry: %s. "
                "Falling back to scan-based path.", e,
            )
            self._state.rust_registry = None

    # ---- queries -----------------------------------------------------------

    @property
    def is_live(self) -> bool:
        """True if the Rust registry is instantiated and ready."""
        return self._state.rust_registry is not None

    def committed_offset(self, consumer_id: str) -> int | None:
        """Return committed_offset from Rust, or None if Rust isn't live.

        None means "no Rust answer; caller should fall back to its
        own path." It does NOT mean "offset 0" — that ambiguity is
        why this returns Optional.
        """
        if not self.is_live:
            return None
        try:
            # TODO(rust-build): confirm the method signature is
            # `committed_offset(view_name: str) -> int`. The Rust
            # signature is `pub fn committed_offset(&self, view_name: String) -> u64`.
            return self._state.rust_registry.committed_offset(consumer_id)
        except Exception as e:
            logger.warning("Rust committed_offset call failed: %s", e)
            return None

    def meet(self, consumer_ids: Iterable[str]) -> int | None:
        """Return meet from Rust, or None if Rust isn't live."""
        if not self.is_live:
            return None
        try:
            ids_list = list(consumer_ids)
            # TODO(rust-build): confirm `meet(view_names: list[str]) -> int`.
            return self._state.rust_registry.meet(ids_list)
        except Exception as e:
            logger.warning("Rust meet call failed: %s", e)
            return None

    def known_views(self) -> set[str]:
        """Return the set of consumer_ids Rust knows about (empty if
        not live)."""
        if not self.is_live:
            return set()
        try:
            # TODO(rust-build): confirm `known_views() -> list[str]`.
            return set(self._state.rust_registry.known_views())
        except Exception as e:
            logger.warning("Rust known_views call failed: %s", e)
            return set()

    def open_batch_count(self) -> int:
        if not self.is_live:
            return 0
        try:
            return self._state.rust_registry.open_batch_count()
        except Exception as e:
            logger.warning("Rust open_batch_count call failed: %s", e)
            return 0

    # ---- writes (called by LogClient) --------------------------------------

    def notify_applied(self, consumer_id: str, offset: int) -> None:
        """A new event was appended at `offset` on behalf of `consumer_id`.

        Called by `LogClient.append` after every successful append
        when the event's consumer_id can be determined.
        """
        if not self.is_live:
            return
        with self._lock:
            prev = self._state.consumer_offsets.get(consumer_id, 0)
            if offset > prev:
                self._state.consumer_offsets[consumer_id] = offset
        try:
            # TODO(rust-build): confirm the method signature is
            # `record_applied(view_name: str, offset: int) -> None`.
            # Note: the Rust side auto-registers the view on first
            # `record_applied` call (see the Rust `FrontierRegistry::register`
            # being called from inside `record_applied`). So we don't
            # need to pre-register.
            self._state.rust_registry.record_applied(consumer_id, offset)
        except Exception as e:
            logger.warning(
                "Rust record_applied(%s, %d) failed: %s",
                consumer_id, offset, e,
            )

    def notify_batch_opened(
        self,
        consumer_id: str,
        batch_id: str,
        start_offset: int,
    ) -> None:
        """A batch started: caps every view's committed_offset on the
        Rust side.

        `start_offset` is the log offset of the BatchStarted event
        itself.
        """
        with self._lock:
            self._state.open_batch_starts[(consumer_id, batch_id)] = start_offset
        if not self.is_live:
            return
        try:
            # TODO(rust-build): confirm signature
            # `record_batch_started(consumer_id: str, batch_id: str, start_offset: int)`.
            self._state.rust_registry.record_batch_started(
                consumer_id, batch_id, start_offset,
            )
        except Exception as e:
            logger.warning(
                "Rust record_batch_started(%s, %s, %d) failed: %s",
                consumer_id, batch_id, start_offset, e,
            )

    def notify_batch_closed(self, consumer_id: str, batch_id: str) -> None:
        """A batch closed (committed or aborted): lifts the cap on
        the Rust side."""
        with self._lock:
            self._state.open_batch_starts.pop((consumer_id, batch_id), None)
        if not self.is_live:
            return
        try:
            # TODO(rust-build): confirm signature
            # `record_batch_closed(consumer_id: str, batch_id: str)`.
            self._state.rust_registry.record_batch_closed(
                consumer_id, batch_id,
            )
        except Exception as e:
            logger.warning(
                "Rust record_batch_closed(%s, %s) failed: %s",
                consumer_id, batch_id, e,
            )


# =============================================================================
# Process-wide singleton
# =============================================================================


_BRIDGE: FrontierBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def get_frontier_bridge() -> FrontierBridge:
    """Return the process-wide `FrontierBridge`, initializing once."""
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = FrontierBridge()
        return _BRIDGE


def set_frontier_bridge(bridge: FrontierBridge | None) -> None:
    """Replace the process-wide bridge (test hook)."""
    global _BRIDGE
    with _BRIDGE_LOCK:
        _BRIDGE = bridge


__all__ = [
    "FrontierBridge",
    "get_frontier_bridge",
    "is_rust_available",
    "reset_probe_for_testing",
    "set_frontier_bridge",
]
