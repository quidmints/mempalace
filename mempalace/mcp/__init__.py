"""
mempalace.mcp — MCP server + tool surface.

Per Part 11.1: a thin MCP layer over the new event-log substrate.
The server is a tool registry with async dispatch; tools are grouped
into submodules (handle / assert / canon / iams / period / event /
velocity / signature / match / review / legacy).

Spec ref: Part 11.
"""

from .server import (
    MCPServer,
    ToolHandler,
    ToolSpec,
    get_server,
    register_default_tools,
    set_server,
)

__all__ = [
    "MCPServer",
    "ToolHandler",
    "ToolSpec",
    "get_server",
    "register_default_tools",
    "set_server",
]
