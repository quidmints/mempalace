"""
MCP server entry — tool dispatch.

Per Part 11.1: the MCP server is a thin dispatch layer. Each tool is
registered as a (name → handler) entry; `MCPServer.handle_request()`
routes incoming requests to the appropriate handler. Tools live in
`mempalace.mcp.tools.*`.

This module owns:

  - ToolHandler protocol      (sync or async function over a request dict)
  - ToolSpec                  (name, description, JSON Schema, handler)
  - MCPServer                 (registry + dispatch)
  - request/response shapes   (matches MCP JSON-RPC 2.0)

Production wiring uses the official `mcp` Python package; this module
keeps the dispatch logic separate so tests can exercise it without the
transport layer.

Spec ref: Part 11.1.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Tool spec + protocol
# =============================================================================


# A tool handler is a callable that takes the parsed `params` dict and
# returns a result dict. May be sync or async.
ToolHandler = Callable[[dict[str, Any]], Any]


@dataclass
class ToolSpec:
    """Description of a tool registered with the MCP server."""

    name: str
    description: str
    handler: ToolHandler
    input_schema: dict[str, Any] = field(default_factory=dict)
    deprecated: bool = False
    deprecation_message: str = ""


# =============================================================================
# Server
# =============================================================================


@dataclass
class MCPServer:
    """In-process MCP server with a tool registry.

    The transport layer (stdio, websocket, etc.) is the caller's
    concern; this class wraps the dispatch table.
    """

    name: str = "mempalace-mcp"
    version: str = "0.1.0"
    _tools: dict[str, ToolSpec] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- registration -----------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        with self._lock:
            self._tools[spec.name] = spec

    def register_many(self, specs: list[ToolSpec]) -> int:
        for s in specs:
            self.register(s)
        return len(specs)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._tools.pop(name, None) is not None

    def list_tools(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": s.input_schema,
                    "deprecated": s.deprecated,
                    "deprecation_message": s.deprecation_message,
                }
                for s in self._tools.values()
            ]

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def size(self) -> int:
        with self._lock:
            return len(self._tools)

    # ---- dispatch ---------------------------------------------------------

    async def call(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Async dispatch: returns {"ok": bool, "result": ..., "error": ...}.

        Uses the handler verbatim. If the handler is async, awaits it.
        """
        params = params or {}
        spec = self.get(name)
        if spec is None:
            return {"ok": False, "error": f"unknown tool: {name}"}

        try:
            outcome = spec.handler(params)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, Awaitable):
                outcome = await outcome  # type: ignore[assignment]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        meta: dict[str, Any] = {}
        if spec.deprecated:
            meta["deprecated"] = True
            if spec.deprecation_message:
                meta["deprecation_message"] = spec.deprecation_message

        return {"ok": True, "result": outcome, "meta": meta}

    def call_sync(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sync wrapper for tests / non-async callers."""
        return asyncio.run(self.call(name, params))

    # ---- JSON-RPC 2.0 wrapper --------------------------------------------

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle a JSON-RPC 2.0 request envelope.

        Supported methods:
          - "tools/list"  → returns the registered tools
          - "tools/call"  → dispatches; params = {"name": "...", "arguments": {...}}
        """
        rpc_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {}) or {}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"tools": self.list_tools()},
            }

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            outcome = await self.call(tool_name, arguments)
            if outcome.get("ok"):
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": outcome.get("result"),
                }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32_000,
                    "message": outcome.get("error", "tool error"),
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32_601, "message": f"method not found: {method}"},
        }


# =============================================================================
# Module-level singleton
# =============================================================================


_SERVER: MCPServer | None = None
_SERVER_LOCK = threading.Lock()


def get_server() -> MCPServer:
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            _SERVER = MCPServer()
        return _SERVER


def set_server(server: MCPServer) -> None:
    global _SERVER
    with _SERVER_LOCK:
        _SERVER = server


def register_default_tools(server: MCPServer | None = None) -> int:
    """Register every tool in `mempalace.mcp.tools.*`.

    Each submodule exposes a `register(server)` function that adds its
    tools to the given server.
    """
    srv = server or get_server()
    from .tools import (
        assert_ as assert_tools,
        canon as canon_tools,
        event as event_tools,
        handle as handle_tools,
        iams as iams_tools,
        legacy as legacy_tools,
        match as match_tools,
        period as period_tools,
        review as review_tools,
        signature as signature_tools,
        velocity as velocity_tools,
    )
    before = srv.size()
    handle_tools.register(srv)
    assert_tools.register(srv)
    canon_tools.register(srv)
    iams_tools.register(srv)
    period_tools.register(srv)
    event_tools.register(srv)
    velocity_tools.register(srv)
    signature_tools.register(srv)
    match_tools.register(srv)
    review_tools.register(srv)
    legacy_tools.register(srv)
    return srv.size() - before


__all__ = [
    "MCPServer",
    "ToolHandler",
    "ToolSpec",
    "get_server",
    "register_default_tools",
    "set_server",
]
