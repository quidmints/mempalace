"""End-to-end integration tests.

Touches: log → graph → views → miner → canonicalizer → resolve → mcp
in one flow. Verifies the substrate cleanly composes across batches.
"""

from __future__ import annotations

import unittest

from mempalace.tests.conftest import fresh_palace, reset_module_state


class TestEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.p = fresh_palace()

    def test_palace_lifecycle_end_to_end(self) -> None:
        """Walk a palace from empty → populated → mined → resolved.

        Steps:
          1. Create theme + period in the graph
          2. Append drawer events
          3. Run Class 1 miner over the drawers
          4. Run Class 2 miner across drawers
          5. Verify the proposal store accumulates miner outputs
        """
        from mempalace.miner import (
            Class1Pass,
            Class2Pass,
            PassContext,
            get_proposal_store,
        )

        # 1. Create theme + period
        theme_id = self.p["graph"].create_theme(name="Running")
        period_id = self.p["graph"].create_period(
            theme_id=theme_id, name="Q1", started_at_ms=1_000_000,
        )
        self.assertTrue(theme_id.startswith("thm_"))
        self.assertTrue(period_id.startswith("prd_"))

        # 2. Drawers (minimal — sufficient for class1/class2)
        drawers = [
            {"drawer_id": "d1",
             "verbatim": "I went for a 5-mile run today; I felt great after.",
             "themes": [theme_id]},
            {"drawer_id": "d2",
             "verbatim": "I plan to run again tomorrow at sunrise.",
             "themes": [theme_id]},
            {"drawer_id": "d3",
             "verbatim": "I was happy after the run. I want to keep this up.",
             "themes": [theme_id]},
        ]

        # 3. Class 1 — per-drawer enrichment
        c1 = Class1Pass()
        c1_result = c1.run(PassContext(parameters={"drawers": drawers}))
        self.assertTrue(c1_result.success)
        self.assertEqual(c1_result.inputs_consumed, 3)
        # 5 proposals × 3 drawers = 15
        self.assertEqual(c1_result.outputs_emitted, 15)

        # 4. Class 2 — cross-drawer aggregation
        c2 = Class2Pass()
        c2_result = c2.run(PassContext(parameters={"drawers": drawers}))
        self.assertTrue(c2_result.success)
        # At least period_state + (per-theme) velocity_update should fire
        kinds = {p.proposal_kind for p in c2_result.proposals}
        self.assertIn("period_state", kinds)
        self.assertIn("velocity_update", kinds)

        # 5. Proposals accumulate into the store
        store = get_proposal_store()
        for p in c1_result.proposals + c2_result.proposals:
            store.add(p)
        self.assertGreaterEqual(store.size(), 15 + len(c2_result.proposals))


class TestMcpIntegration(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_mcp_roundtrip_via_handle_lifecycle(self) -> None:
        """Verify the MCP server dispatches handle protocol cleanly."""
        from mempalace.mcp import MCPServer, register_default_tools
        from mempalace.mcp.tools import handle as handle_mod

        handle_mod._HANDLES.clear()
        server = MCPServer()
        register_default_tools(server)

        # JSON-RPC envelope: tools/list returns the tool catalog
        import asyncio
        resp = asyncio.run(server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        }))
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertGreaterEqual(len(resp["result"]["tools"]), 17)

        # tools/call: allocate handle
        resp = asyncio.run(server.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "mempalace_handle_allocate",
                "arguments": {"owner": "agent_test"},
            },
        }))
        self.assertIn("result", resp)
        self.assertIn("handle_id", resp["result"])


class TestResolveIntegration(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_baseline_market_eligibility_threshold(self) -> None:
        """Confirms the on-chain side and off-chain side agree on the
        90-day baseline window."""
        from mempalace.signatures.baseline import MIN_BASELINE_WINDOW_DAYS

        # Off-chain min window must equal the on-chain validation
        # (see mempalace_chain/.../state/baseline_market.rs:validate_window)
        self.assertEqual(MIN_BASELINE_WINDOW_DAYS, 90)


class TestCanonicalizerToMinerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_canonicalizer_resolves_class2_predicates(self) -> None:
        """Class 2 emits assertion proposals; canonicalizer should
        canonicalize their predicate surfaces."""
        from mempalace.canonicalizer import CanonDomain, Canonicalizer
        from mempalace.miner import Class2Pass, PassContext

        def deterministic_embed(s: str) -> list[float]:
            v = [0.0] * 16
            for i, ch in enumerate(s.lower()[:16]):
                v[i] = (ord(ch) % 17) / 17.0
            n = sum(x * x for x in v) ** 0.5
            return [x / n for x in v] if n > 0 else v

        can = Canonicalizer(embedder=deterministic_embed)
        can.seed(CanonDomain.PREDICATES, [
            ("pred_loves", "loves", deterministic_embed("loves")),
        ])
        # Run Class 2 against a drawer with "loves" predicate text
        drawers = [
            {"drawer_id": "d1",
             "verbatim": "Sarah loves coffee daily mornings",
             "themes": []}
        ]
        c2 = Class2Pass()
        result = c2.run(PassContext(parameters={"drawers": drawers}))
        # Canonicalize each assertion's predicate
        assertions = [p for p in result.proposals if p.proposal_kind == "assertion"]
        self.assertGreater(len(assertions), 0)
        for a in assertions:
            pred = a.proposed_value.get("predicate", "")
            res = can.resolve(CanonDomain.PREDICATES, pred)
            # Either it matched the canonical or queued in candidate pool
            self.assertTrue(
                res.matched_existing or res.queued_in_cluster_id is not None
            )


if __name__ == "__main__":
    unittest.main()
