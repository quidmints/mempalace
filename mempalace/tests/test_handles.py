"""Tests for the handle protocol (Part 5)."""

from __future__ import annotations

import unittest

from mempalace.tests.conftest import reset_module_state


class TestHandleLifecycle(unittest.TestCase):
    """Test handle protocol via the MCP tools layer (the user-facing path)."""

    def setUp(self) -> None:
        reset_module_state()
        from mempalace.mcp import MCPServer, register_default_tools
        # Reset in-memory handle table
        from mempalace.mcp.tools import handle as handle_mod
        handle_mod._HANDLES.clear()
        self.server = MCPServer()
        register_default_tools(self.server)

    def test_allocate_returns_handle_id(self) -> None:
        res = self.server.call_sync(
            "mempalace_handle_allocate",
            {"owner": "tester", "initial_context": "test"},
        )
        self.assertTrue(res["ok"])
        self.assertIn("handle_id", res["result"])
        self.assertTrue(res["result"]["handle_id"].startswith("hndl_"))

    def test_resolve_unknown_handle(self) -> None:
        res = self.server.call_sync(
            "mempalace_handle_resolve", {"handle_id": "hndl_nope"},
        )
        self.assertTrue(res["ok"])
        self.assertFalse(res["result"]["resolved"])

    def test_full_lifecycle(self) -> None:
        # allocate
        res = self.server.call_sync(
            "mempalace_handle_allocate", {"owner": "x"},
        )
        hid = res["result"]["handle_id"]
        # resolve
        res = self.server.call_sync(
            "mempalace_handle_resolve", {"handle_id": hid},
        )
        self.assertTrue(res["result"]["resolved"])
        # refine
        res = self.server.call_sync(
            "mempalace_handle_refine",
            {"handle_id": hid, "refinement": "first"},
        )
        self.assertTrue(res["result"]["refined"])
        self.assertEqual(res["result"]["refinement_count"], 1)
        # close
        res = self.server.call_sync(
            "mempalace_handle_close", {"handle_id": hid},
        )
        self.assertTrue(res["result"]["closed"])
        # refine after close → fails
        res = self.server.call_sync(
            "mempalace_handle_refine",
            {"handle_id": hid, "refinement": "after_close"},
        )
        self.assertFalse(res["result"]["refined"])


if __name__ == "__main__":
    unittest.main()
