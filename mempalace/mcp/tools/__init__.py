"""
mempalace.mcp.tools — tool implementations.

Each submodule exposes a `register(server)` function that adds its
tools to the given MCPServer. The server.py:register_default_tools
calls all of them.

Submodules:

  handle      — handle protocol (allocate / resolve / refine / close)
  assert_     — assertion writes (8-part frame)
  canon       — promote-to-canon, canon-amend
  iams        — current self-entity I-am role-set
  period      — period open / close / seal
  event       — agent-asserted events
  velocity    — velocity field readout
  signature   — signature export at requested level
  match       — federation match request + findings
  review      — pending-review surface
  legacy      — backward-compat shim for old tool names

Spec ref: Part 11.1.
"""
