"""Tests for DD sub-slice D — aggregating views.

Sub-slice D converts the six views that aggregate state across many
events:

  - canon_set
  - active_periods
  - active_iams
  - recurrence_clusters
  - heat_field
  - velocity_field

Same operator chain as sub-slices B/C, but two of these (heat_field,
velocity_field) involve an architectural shift: they now subscribe
to a new event kind `node_accessed` instead of being mutated by
external method calls. This shift is the architecturally honest
answer per the spec (Part 1.3 — view state lives in the event log,
not in side-channel mutations).

# Tests

Per view: structural (always run, reads source) + a couple of behavioral
tests that skip until the Rust extension is built.
"""

from __future__ import annotations

import importlib
import os
import re
import unittest


VIEW_DIR = "/home/claude/work/mempalace_core/src/dataflow/views"
VIEWS_MOD_PATH = f"{VIEW_DIR}/mod.rs"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# =============================================================================
# Common assertions used across views
# =============================================================================


def assert_dd_view_shape(test: unittest.TestCase, src: str, *, view_name: str,
                          subscribed_kinds: list[str], state_struct: str) -> None:
    """Every DD-backed view in sub-slice D follows the same shape:
    flat_map → reduce → arrange_by_key, with a snapshot mirror via
    inspect_batch, and a State struct that's DD-compatible."""
    # Operator chain
    test.assertIn("input.flat_map", src)
    test.assertIn(".reduce(", src)
    test.assertIn("arrange_by_key", src)
    test.assertIn("inspect_batch", src)

    # ViewSpec impl
    test.assertIn(f"impl ViewSpec for ", src)
    test.assertRegex(
        src,
        r'fn name\(&self\)\s*->\s*&\'static str\s*\{\s*"' + re.escape(view_name) + r'"',
    )

    # subscribed_kinds includes all expected
    m = re.search(r'fn subscribed_kinds.*?\}', src, re.DOTALL)
    test.assertIsNotNone(m, "subscribed_kinds method not found")
    body = m.group(0)
    for k in subscribed_kinds:
        test.assertIn(f'"{k}"', body, f"missing subscription to {k}")

    # State struct is DD-compatible
    m = re.search(
        r"#\[derive\(([^)]+)\)\]\s*pub struct " + re.escape(state_struct),
        src,
    )
    test.assertIsNotNone(m, f"{state_struct} struct not found")
    derives = m.group(1)
    for required in ["Clone", "Eq", "Hash", "Ord"]:
        test.assertIn(required, derives, f"{state_struct} missing derive: {required}")


# =============================================================================
# canon_set
# =============================================================================


class TestCanonSetView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/canon_set.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="canon_set",
            subscribed_kinds=["node_created", "node_property_set"],
            state_struct="CanonState",
        )

    def test_inline_tests(self):
        for t in [
            "fn fold_created_canonical_emits_state",
            "fn fold_created_non_canonical_then_flipped_on",
            "fn fold_canonical_then_off_yields_none",
            "fn canon_path_update_applied",
            "fn structural_leverage_update_applied",
            "fn parse_event_picks_node_id_from_canonical_set",
        ]:
            self.assertIn(t, self.src)

    def test_canonical_off_retracts_from_set(self):
        """Setting canonical=false must yield a None state (which DD
        retracts). This is the spec's bidirectional-flux behavior:
        canonical state is symmetric, not a one-way ratchet."""
        # The fold_events function must explicitly check for is_canonical
        # before returning the state.
        self.assertIn("if is_canonical", self.src)


# =============================================================================
# active_periods
# =============================================================================


class TestActivePeriodsView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/active_periods.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="active_periods",
            subscribed_kinds=["node_created", "node_property_set"],
            state_struct="ActivePeriodState",
        )

    def test_filters_by_period_kind(self):
        """Only node_created with kind=period should produce state."""
        self.assertIn('p.node_kind != "period"', self.src)

    def test_inline_tests(self):
        for t in [
            "fn parse_skips_non_period_node_created",
            "fn parse_period_created_extracts_props",
            "fn fold_state_change",
            "fn fold_orphan_state_change_noop",
            "fn fold_precedence_update",
        ]:
            self.assertIn(t, self.src)

    def test_legacy_compat_fields_present(self):
        for f in ['"period_id"', '"theme_id"', '"name"', '"state"',
                  '"started_at_ms"', '"ended_at_ms"', '"precedence"']:
            self.assertIn(f, self.src)


# =============================================================================
# active_iams
# =============================================================================


class TestActiveIamsView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/active_iams.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="active_iams",
            subscribed_kinds=["edge_created", "edge_invalidated"],
            state_struct="IamBinding",
        )

    def test_self_entity_filter(self):
        """Only role_in_period edges sourced at the self-entity
        produce state."""
        self.assertIn('p.edge_kind != "role_in_period"', self.src)
        self.assertIn("SELF_ENTITY_ID", self.src)

    def test_self_entity_id_constant(self):
        # Must match the legacy view + Python conventions.
        self.assertIn('"ent_self_self0000"', self.src)

    def test_invalidate_retracts_from_set(self):
        """Invalidated bindings must yield None to DD retracts them."""
        self.assertIn("if invalidated", self.src)
        # Specifically: the fold returns None when invalidated
        self.assertIn("None\n    } else {", self.src)

    def test_inline_tests(self):
        for t in [
            "fn parse_skips_non_self_source",
            "fn parse_skips_non_role_edge_kind",
            "fn parse_extracts_role_in_period",
            "fn fold_invalidate_yields_none",
            "fn fold_create_only_yields_binding",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# recurrence_clusters
# =============================================================================


class TestRecurrenceClustersView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/recurrence_clusters.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="recurrence_clusters",
            subscribed_kinds=["recurrence_cluster_member"],
            state_struct="ClusterState",
        )

    def test_dedupe_in_fold(self):
        """fold_memberships must dedupe drawer_ids."""
        self.assertIn("if !members.contains", self.src)

    def test_first_drawer_is_representative(self):
        # Representative is the first member by offset
        self.assertIn("representative = events.first()", self.src)

    def test_inline_tests(self):
        for t in [
            "fn parse_event_extracts_cluster_and_drawer",
            "fn fold_first_drawer_is_representative",
            "fn fold_dedupe_preserves_order",
            "fn fold_empty_returns_none",
            "fn parse_skips_unrelated_kinds",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# heat_field — architectural shift
# =============================================================================


class TestHeatFieldView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/heat_field.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="heat_field",
            subscribed_kinds=["node_created", "node_accessed", "node_property_set"],
            state_struct="HeatState",
        )

    def test_subscribes_to_node_accessed(self):
        """Sub-slice D introduces `node_accessed` as the event that
        replaces the legacy view's external `bump()` method. This
        is the architectural shift heat needs."""
        self.assertIn('"node_accessed"', self.src)
        self.assertIn("ParsedHeatEvent::Accessed", self.src)

    def test_no_external_bump_method(self):
        """The DD view must NOT expose a `pub fn bump()` — that's
        the side-channel mutation that DD doesn't tolerate."""
        # Allow `bump_amount` as a config field/local var, but no
        # `pub fn bump(`
        self.assertNotRegex(
            self.src,
            r"pub fn bump\s*\(",
            msg="heat_field DD view must not expose external bump() method",
        )

    def test_decay_is_lazy_at_query_time(self):
        """Heat is stored as `(heat_at_anchor, anchor_ms)`; decay
        is computed at query time via `heat_at(now_ms, ...)`."""
        self.assertIn("pub fn heat_at(", self.src)
        self.assertIn("heat_at_anchor", self.src)
        self.assertIn("anchor_ms", self.src)

    def test_canon_floor_present(self):
        self.assertIn("canon_floor", self.src)
        self.assertIn("DEFAULT_CANON_FLOOR", self.src)

    def test_documents_architectural_shift(self):
        """The module-level docstring must explain why the
        architectural shift was made."""
        self.assertIn("Architectural shift", self.src)
        self.assertIn("node_accessed", self.src)

    def test_inline_tests(self):
        for t in [
            "fn fold_two_accesses_increases_heat",
            "fn fold_caps_heat_at_one",
            "fn heat_at_decays_over_time",
            "fn canon_floor_bounds_decay",
            "fn parse_node_accessed_uses_event_field",
            "fn parse_node_accessed_falls_back_to_offset",
            "fn parse_property_set_only_canonical_field",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# velocity_field — same architectural shift
# =============================================================================


class TestVelocityFieldView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = f"{VIEW_DIR}/velocity_field.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_shape(self):
        assert_dd_view_shape(
            self, self.src,
            view_name="velocity_field",
            subscribed_kinds=["node_created", "node_accessed"],
            state_struct="VelocityState",
        )

    def test_subscribes_to_node_accessed(self):
        self.assertIn('"node_accessed"', self.src)

    def test_no_external_record_access_method(self):
        """The DD view must NOT expose `pub fn record_access()` —
        access is event-driven."""
        self.assertNotRegex(
            self.src,
            r"pub fn record_access\s*\(",
            msg="velocity_field DD view must not expose external record_access()",
        )

    def test_window_math_uses_seven_fourteen_thirty_sixty_ninety(self):
        # The legacy semantics use 7/14/30/60/90 day windows.
        for d in ["7 * MS_PER_DAY", "14 * MS_PER_DAY", "30 * MS_PER_DAY",
                  "60 * MS_PER_DAY", "90 * MS_PER_DAY"]:
            self.assertIn(d, self.src)

    def test_velocity_at_query_helper(self):
        """The summary helper computes velocity at a given `now_ms`."""
        self.assertIn("pub fn velocity_at(&self, now_ms: u64)", self.src)
        self.assertIn("VelocitySummary", self.src)

    def test_trims_to_ninety_day_window(self):
        """The fold trims the access list to the 90-day window
        relative to the latest access."""
        self.assertIn("90 * MS_PER_DAY", self.src)

    def test_inline_tests(self):
        for t in [
            "fn fold_only_create_returns_empty_state",
            "fn fold_only_accesses_returns_state",
            "fn fold_trims_to_90_day_window",
            "fn velocity_at_zero_prior_returns_count_7d",
            "fn velocity_at_with_prior_uses_ratio",
            "fn velocity_at_skips_future_timestamps",
            "fn parse_node_accessed_falls_back_to_offset",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# Module registration
# =============================================================================


class TestSubsliceDRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(VIEWS_MOD_PATH):
            raise unittest.SkipTest("views/mod.rs missing")
        cls.src = _read(VIEWS_MOD_PATH)

    def test_all_six_registered(self):
        for v in [
            "canon_set",
            "active_periods",
            "active_iams",
            "recurrence_clusters",
            "heat_field",
            "velocity_field",
        ]:
            self.assertIn(f"pub mod {v}", self.src)


# =============================================================================
# Behavioral tests — skipped until extension is built
# =============================================================================


def _has_rust_dataflow():
    try:
        mp = importlib.import_module("mempalace_core")
    except ImportError:
        return False
    return hasattr(mp, "PyDataflowHandle")


@unittest.skipUnless(
    _has_rust_dataflow(),
    "Rust extension with DD handle not built (sub-slice H wires this up)",
)
class TestSubsliceDBehavioral(unittest.TestCase):
    """End-to-end DD tests for the six views. Each runs a small
    scenario and confirms the materialized state matches the
    expected legacy semantics."""

    def test_canon_set_roundtrip(self):
        import json
        import mempalace_core as mp  # type: ignore
        h = mp.PyDataflowHandle.start(["canon_set"])
        try:
            h.feed(offset=1, kind="node_created", payload=json.dumps({
                "node_id": "n1", "node_kind": "schema",
                "canonical": True, "canon_path": "soul/loyalty.md",
            }).encode())
            h.advance_to(1)
            v = json.loads(h.query("canon_set", b"n1"))
            self.assertEqual(v["canon_path"], "soul/loyalty.md")
            # Flip canonical off → retract
            h.feed(offset=2, kind="node_property_set", payload=json.dumps({
                "node_id": "n1", "field_name": "canonical", "new_value": False,
            }).encode())
            h.advance_to(2)
            self.assertIsNone(h.query("canon_set", b"n1"))
        finally:
            h.shutdown()

    def test_active_iams_roundtrip(self):
        import json
        import mempalace_core as mp  # type: ignore
        h = mp.PyDataflowHandle.start(["active_iams"])
        try:
            h.feed(offset=1, kind="edge_created", payload=json.dumps({
                "edge_id": "e1",
                "edge_kind": "role_in_period",
                "source_node_id": "ent_self_self0000",
                "target_node_id": "schema_engineer",
                "properties": {"role": "engineer"},
            }).encode())
            h.advance_to(1)
            v = json.loads(h.query("active_iams", b"e1"))
            self.assertEqual(v["role"], "engineer")
            h.feed(offset=2, kind="edge_invalidated",
                    payload=json.dumps({"edge_id": "e1"}).encode())
            h.advance_to(2)
            self.assertIsNone(h.query("active_iams", b"e1"))
        finally:
            h.shutdown()

    def test_heat_field_accumulates(self):
        import json
        import mempalace_core as mp  # type: ignore
        h = mp.PyDataflowHandle.start(["heat_field"])
        try:
            h.feed(offset=1, kind="node_created", payload=json.dumps({
                "node_id": "n1",
            }).encode())
            for i, ts in enumerate([1000, 2000, 3000], start=2):
                h.feed(offset=i, kind="node_accessed", payload=json.dumps({
                    "node_id": "n1", "accessed_at_ms": ts,
                }).encode())
            h.advance_to(4)
            v = json.loads(h.query("heat_field", b"n1"))
            self.assertEqual(v["access_count"], 3)
            # heat = 0.5 + 3 * 0.05 = 0.65
            self.assertAlmostEqual(v["heat_at_anchor"], 0.65, places=5)
        finally:
            h.shutdown()

    def test_velocity_field_window_counts(self):
        import json
        import mempalace_core as mp  # type: ignore
        h = mp.PyDataflowHandle.start(["velocity_field"])
        try:
            now = 100 * 86_400_000  # day 100
            for i, days_ago in enumerate([1, 2, 5, 10, 11], start=1):
                ts = now - days_ago * 86_400_000
                h.feed(offset=i, kind="node_accessed", payload=json.dumps({
                    "node_id": "n1", "accessed_at_ms": ts,
                }).encode())
            h.advance_to(5)
            v = json.loads(h.query("velocity_field", b"n1"))
            # 3 accesses within 7d (1, 2, 5); 2 in prior 7d (10, 11)
            # velocity = (3 - 2) / 2 = 0.5
            self.assertEqual(v["access_count_7d"], 3)
            self.assertAlmostEqual(v["velocity_7d"], 0.5, places=5)
        finally:
            h.shutdown()


if __name__ == "__main__":
    unittest.main()
