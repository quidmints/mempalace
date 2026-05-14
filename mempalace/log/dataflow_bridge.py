"""
Python ↔ Rust dataflow bridge.

Sub-slice F shipped a `PyDataflowHandle` PyO3 class exposing the
14 DD-backed master views. Sub-slice H is the wire that lets the
Python side use it.

# What it does

When the `mempalace_core` extension is importable AND has
`PyDataflowHandle`:

  - Maintains a process-wide `PyDataflowHandle` instance with all
    14 views registered.
  - On startup, calls `PyFrontierRegistry::attach_dataflow(handle)`
    so the sub-slice G mode (frontier reads from DD) activates
    transparently for Python callers of `FrontierBridge`.
  - Exposes `feed(entry)`, `advance_to(offset)`, `query(view_name,
    key_bytes)`, and `frontier_of(view_name)` to Python consumers.
  - The behavioral tests in `tests/test_dataflow_subslice_*.py`
    use `get_dataflow_bridge()` to acquire the handle.

When the extension is NOT importable, or doesn't have
`PyDataflowHandle` (pre-F builds):

  - All bridge methods are no-ops or return None.
  - Behavioral tests skip via `is_dataflow_live()`.
  - Other code paths are unaffected.

# Relationship to FrontierBridge

`FrontierBridge` (Phase-5/G surface) and `DataflowBridge` are
sibling adapters over different parts of the same Rust extension.
They share extension-availability state but track different
classes:

  - FrontierBridge → PyFrontierRegistry (batch coordination,
    committed/applied offsets, meet)
  - DataflowBridge → PyDataflowHandle (view queries, feed/advance,
    DD frontier reads)

When both are live, sub-slice G's `attach_dataflow` is called by
`DataflowBridge.__init__`, so the frontier registry transparently
starts reading from the DD frontier — no Python caller of
`FrontierBridge.committed_offset()` needs to know.

# What I can't validate from this environment

Same caveats as `rust_bridge.py`: no Rust toolchain, so PyO3
boundary shapes are inferred from the Rust source. Marked
`TODO(rust-build)`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from mempalace.log.rust_bridge import (
    get_frontier_bridge,
    is_rust_available,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Extension probe — separate from FrontierBridge's because the class
# we look for is different (PyDataflowHandle, not PyFrontierRegistry).
# =============================================================================


_DATAFLOW_PROBED: bool = False
_DATAFLOW_AVAILABLE: bool = False
_DATAFLOW_HANDLE_CLS: Any = None
_PROBE_LOCK = threading.Lock()


def _probe_dataflow_handle() -> None:
    """Try to find `PyDataflowHandle` on `mempalace_core`. Idempotent."""
    global _DATAFLOW_PROBED, _DATAFLOW_AVAILABLE, _DATAFLOW_HANDLE_CLS
    with _PROBE_LOCK:
        if _DATAFLOW_PROBED:
            return
        _DATAFLOW_PROBED = True
        if not is_rust_available():
            # Extension not importable at all
            return
        try:
            import mempalace_core  # type: ignore[import-not-found]
        except ImportError:
            return
        cls = getattr(mempalace_core, "PyDataflowHandle", None)
        if cls is None:
            logger.debug(
                "mempalace_core imported but PyDataflowHandle not "
                "found on the module. The dataflow bridge cannot be "
                "wired. (Build with sub-slice F or later.)"
            )
            return
        _DATAFLOW_HANDLE_CLS = cls
        _DATAFLOW_AVAILABLE = True
        logger.info("Rust dataflow bridge live (PyDataflowHandle found)")


def is_dataflow_live() -> bool:
    """True if `mempalace_core.PyDataflowHandle` is importable.

    Used by behavioral tests to decide whether to skip.
    """
    _probe_dataflow_handle()
    return _DATAFLOW_AVAILABLE


def reset_dataflow_probe_for_testing() -> None:
    """Force a re-probe (test hook)."""
    global _DATAFLOW_PROBED, _DATAFLOW_AVAILABLE, _DATAFLOW_HANDLE_CLS
    with _PROBE_LOCK:
        _DATAFLOW_PROBED = False
        _DATAFLOW_AVAILABLE = False
        _DATAFLOW_HANDLE_CLS = None


# =============================================================================
# Names of the 14 master views (matches `dataflow/views/mod.rs` and
# the match arms in `bindings.rs::PyDataflowHandle::start`).
# =============================================================================


STANDARD_VIEW_NAMES: list[str] = [
    "current_nodes",
    "current_edges",
    "current_interpretations",
    "current_schemas",
    "heat_field",
    "velocity_field",
    "recurrence_clusters",
    "active_periods",
    "active_iams",
    "open_contradictions",
    "canon_set",
    "pending_review",
    "match_cache",
    "matched_against",
]


# =============================================================================
# DataflowBridge
# =============================================================================


class DataflowBridge:
    """Adapter over `mempalace_core.PyDataflowHandle`.

    Holds at most one `PyDataflowHandle` per process. On creation,
    if the handle is live, also attaches it to the
    `FrontierBridge`'s underlying `PyFrontierRegistry` so the
    sub-slice G frontier-read path activates.

    Methods that need the handle return None when it isn't live;
    methods that don't (like `is_live`) return their fallback value.
    """

    def __init__(self, view_names: list[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._handle: Any = None
        self._view_names = view_names if view_names is not None else list(STANDARD_VIEW_NAMES)
        self._init_if_available()

    def _init_if_available(self) -> None:
        if not is_dataflow_live():
            return
        try:
            # TODO(rust-build): the Rust constructor is
            # `PyDataflowHandle::start(view_names: Vec<String>)`. PyO3
            # surfaces this as a static method on the class; the call
            # is `PyDataflowHandle.start([...])`. Empty list = all 14.
            assert _DATAFLOW_HANDLE_CLS is not None
            self._handle = _DATAFLOW_HANDLE_CLS.start(self._view_names)
        except Exception as e:
            logger.warning(
                "Failed to start PyDataflowHandle: %s. "
                "Dataflow bridge will run in fallback mode.",
                e,
            )
            self._handle = None
            return
        # Wire frontier registry to the dataflow if both are live.
        # Sub-slice G's `PyFrontierRegistry::attach_dataflow` is the
        # method that switches frontier reads from the parking_lot
        # shadow tracker to the DD frontier.
        try:
            fb = get_frontier_bridge()
            if fb.is_live and self._handle is not None:
                rust_registry = fb._state.rust_registry  # noqa: SLF001
                # TODO(rust-build): confirm the PyO3 method name is
                # `attach_dataflow(handle: PyDataflowHandle)`. The Rust
                # side defines it on `PyFrontierRegistry` taking an
                # `Arc<DataflowHandle>`; PyO3 should accept a
                # `PyDataflowHandle` and peel the inner handle.
                if hasattr(rust_registry, "attach_dataflow"):
                    rust_registry.attach_dataflow(self._handle)
                    logger.info(
                        "FrontierBridge attached to DataflowHandle; "
                        "frontier reads now driven by DD."
                    )
                else:
                    logger.debug(
                        "PyFrontierRegistry has no attach_dataflow "
                        "method; sub-slice G wiring not active. "
                        "(Likely a pre-G extension build.)"
                    )
        except Exception as e:
            logger.warning(
                "Failed to attach DataflowHandle to FrontierBridge: %s. "
                "FrontierBridge keeps using legacy parking_lot tracker.",
                e,
            )

    @property
    def is_live(self) -> bool:
        """True if the underlying `PyDataflowHandle` is instantiated."""
        return self._handle is not None

    @property
    def handle(self) -> Any:
        """Direct access to the underlying `PyDataflowHandle`. None
        when the bridge isn't live. Used by tests that want raw access."""
        return self._handle

    # ---- view operations -------------------------------------------------------

    def feed(self, offset: int, kind: str, payload: bytes) -> None:
        """Feed a single event tuple into the dataflow.

        No-op when the bridge isn't live. The `payload` should be raw
        JSON bytes — the per-view Rust parser handles deserialization.
        """
        if not self.is_live:
            return
        try:
            # TODO(rust-build): confirm the signature
            # `feed(offset: int, kind: str, payload: bytes) -> None`.
            self._handle.feed(offset, kind, payload)
        except Exception as e:
            logger.warning(
                "Rust handle.feed(offset=%d, kind=%s) failed: %s",
                offset, kind, e,
            )

    def advance_to(self, offset: int) -> None:
        """Block until every view's frontier is past `offset`.

        No-op when the bridge isn't live. Used by callers that need
        a happens-before barrier between feed and query.
        """
        if not self.is_live:
            return
        try:
            self._handle.advance_to(offset)
        except Exception as e:
            logger.warning("Rust handle.advance_to(%d) failed: %s", offset, e)

    def query(self, view_name: str, key_bytes: bytes) -> bytes | None:
        """Look up a view by name + key. Returns the value bytes, or
        None (key not found, or bridge not live)."""
        if not self.is_live:
            return None
        try:
            # TODO(rust-build): confirm the signature
            # `query(view_name: str, key_bytes: bytes) -> Optional[bytes]`.
            return self._handle.query(view_name, key_bytes)
        except Exception as e:
            logger.warning(
                "Rust handle.query(%s, %s) failed: %s",
                view_name, key_bytes, e,
            )
            return None

    def frontier_of(self, view_name: str) -> int | None:
        """Return the live DD frontier for `view_name`, or None if
        the bridge isn't live."""
        if not self.is_live:
            return None
        try:
            return self._handle.frontier_of(view_name)
        except Exception as e:
            logger.warning(
                "Rust handle.frontier_of(%s) failed: %s", view_name, e,
            )
            return None

    def known_views(self) -> set[str]:
        """The view names registered with this handle."""
        if not self.is_live:
            return set()
        try:
            return set(self._handle.known_views())
        except Exception as e:
            logger.warning("Rust handle.known_views() failed: %s", e)
            return set()

    def shutdown(self) -> None:
        """Tear down the worker thread. Idempotent."""
        if not self.is_live:
            return
        try:
            self._handle.shutdown()
        except Exception as e:
            logger.warning("Rust handle.shutdown() failed: %s", e)
        self._handle = None


# =============================================================================
# Process-wide singleton — mirrors FrontierBridge pattern
# =============================================================================


_DATAFLOW_BRIDGE: DataflowBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def get_dataflow_bridge() -> DataflowBridge:
    """Return the process-wide `DataflowBridge`, initializing once.

    The first caller initializes the underlying `PyDataflowHandle`
    with all 14 views (the standard set). To customize, call
    `set_dataflow_bridge(DataflowBridge(view_names=[...]))` first.
    """
    global _DATAFLOW_BRIDGE
    with _BRIDGE_LOCK:
        if _DATAFLOW_BRIDGE is None:
            _DATAFLOW_BRIDGE = DataflowBridge()
        return _DATAFLOW_BRIDGE


def set_dataflow_bridge(bridge: DataflowBridge | None) -> None:
    """Replace the process-wide bridge (test hook).

    Calling with None forces re-initialization on next
    `get_dataflow_bridge()` call.
    """
    global _DATAFLOW_BRIDGE
    with _BRIDGE_LOCK:
        if _DATAFLOW_BRIDGE is not None and _DATAFLOW_BRIDGE is not bridge:
            _DATAFLOW_BRIDGE.shutdown()
        _DATAFLOW_BRIDGE = bridge


__all__ = [
    "DataflowBridge",
    "STANDARD_VIEW_NAMES",
    "get_dataflow_bridge",
    "is_dataflow_live",
    "reset_dataflow_probe_for_testing",
    "set_dataflow_bridge",
]
