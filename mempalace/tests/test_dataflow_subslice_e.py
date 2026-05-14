"""Tests for DD sub-slice E — joining views.

Sub-slice E converts the five views that aggregate state from
multiple correlated event kinds:

  - current_schemas         (single event kind, simple reduce)
  - open_contradictions     (asserted/resolved fold per edge_id)
  - pending_review          (multi-source aggregator with category-keyed adds/removes)
  - match_cache             (request + finding joined per match_id)
  - matched_against         (request + finding ledger per match_id)

Per-view structural tests + behavioral tests that skip until the
Rust extension is built.
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
# Common assertions
# =============================================================================

def assert_dd_view_shape(test: unittest.TestCase, src: str, *,
                          view_name: str, subscribed_kinds: list[str]) -> None:
    """Common DD view shape assertions."""
    # Operator chain
    test.assertIn("input.flat_map", src)
    test.assertIn(".reduce(", src)
    test.assertIn("arrange_by_key", src)
    test.assertIn("inspect_batch", src)

    # ViewSpec
    test.assertIn("impl ViewSpec for ", src)
    test.assertRegex(
        src,
        rf'fn name\(&self\)\s*->\s*&\'static str\s*\{{\s*"{re.escape(view_name)}"',
    )

    # subscribed_kinds — must list every expected kind
    m = re.search(r'fn subscribed_kinds.*?\}', src, re.DOTALL)
    assert m is not None
    body = m.group(0)
    for kind in subscribed_kinds:
        test.assertIn(f'"{kind}"', body, f"missing subscription: {kind}")


# =============================================================================
# current_schemas
# =============================================================================

class TestCurrentSchemasView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = f"{VIEW_DIR}/current_schemas.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_view_shape(self) -> None:
        assert_dd_view_shape(
            self, self.src,
            view_name="current_schemas",
            subscribed_kinds=["schema_induced"],
        )

    def test_state_is_dd_compat(self) -> None:
        m = re.search(r"#\[derive\(([^)]+)\)\]\s*pub struct SchemaState", self.src)
        self.assertIsNotNone(m)
        derives = m.group(1)
        for d in ["Clone", "Eq", "Hash", "Ord"]:
            self.assertIn(d, derives)

    def test_floats_stored_as_bits(self) -> None:
        self.assertIn("stability_score_bits: u64", self.src)
        self.assertIn("coverage_score_bits: u64", self.src)

    def test_pick_latest_used(self) -> None:
        self.assertIn("fn pick_latest", self.src)
        self.assertIn("max_by_key", self.src)

    def test_inline_tests(self) -> None:
        for t in [
            "fn parse_picks_schema_node_id_as_key",
            "fn parse_skips_unrelated_kinds",
            "fn pick_latest_chooses_highest_offset",
            "fn snapshot_query_legacy_shape",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# open_contradictions
# =============================================================================

class TestOpenContradictionsView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = f"{VIEW_DIR}/open_contradictions.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_view_shape(self) -> None:
        assert_dd_view_shape(
            self, self.src,
            view_name="open_contradictions",
            subscribed_kinds=["contradiction_asserted", "contradiction_resolved"],
        )

    def test_resolved_emits_none(self) -> None:
        """Resolution must produce a None from fold_events so DD
        retracts the entry from the trace. The fold_events function
        must explicitly handle the Resolved variant by setting
        current = None."""
        self.assertIn("ParsedContradictionEvent::Resolved", self.src)
        self.assertIn("current = None", self.src)

    def test_inline_tests(self) -> None:
        for t in [
            "fn parse_asserted_keys_by_edge_id",
            "fn parse_resolved_keys_by_edge_id",
            "fn fold_asserted_only_keeps_open",
            "fn fold_resolved_returns_none",
            "fn fold_re_asserted_after_resolution_reopens",
            "fn snapshot_query_legacy_shape",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# pending_review
# =============================================================================

class TestPendingReviewView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = f"{VIEW_DIR}/pending_review.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_view_shape(self) -> None:
        assert_dd_view_shape(
            self, self.src,
            view_name="pending_review",
            subscribed_kinds=[
                "schema_induced",
                "contradiction_asserted",
                "contradiction_resolved",
                "drawer_hash_collision",
                "canonical_promoted",
                "canonical_rejected",
            ],
        )

    def test_item_id_is_deterministic_pri_format(self) -> None:
        """Item ids follow `pri_{category}_{ref}` so resolution
        events key to the same id as their corresponding assertion
        events. This is the mechanism by which contradiction_resolved
        retracts the prior contradiction_open item."""
        # Direct check on the format string used to build item_ids
        self.assertIn('"pri_{}_{}"', self.src)

    def test_extract_reference_handles_known_categories(self) -> None:
        self.assertIn("fn extract_reference_from_item_id", self.src)
        for cat in [
            "schema_proposal",
            "contradiction_open",
            "drawer_collision",
            "canonical_promotion",
            "canonical_rejection",
        ]:
            self.assertIn(f'"{cat}"', self.src)

    def test_inline_tests(self) -> None:
        for t in [
            "fn parse_schema_induced_to_proposal_category",
            "fn parse_contradiction_resolved_emits_remove_with_same_key_as_assert",
            "fn parse_unrelated_kind_returns_none",
            "fn parse_returns_none_when_no_reference_in_payload",
            "fn fold_add_only_keeps_open",
            "fn fold_add_then_remove_returns_none",
            "fn extract_reference_strips_known_category_prefix",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# match_cache
# =============================================================================

class TestMatchCacheView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = f"{VIEW_DIR}/match_cache.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_view_shape(self) -> None:
        assert_dd_view_shape(
            self, self.src,
            view_name="match_cache",
            subscribed_kinds=["match_request_received", "finding_emitted"],
        )

    def test_join_via_reduce_keyed_by_match_id(self) -> None:
        """The join semantics are implemented by reducing on match_id
        (both events share that key). fold_events must require BOTH
        request and finding to be present."""
        self.assertIn("fn fold_events", self.src)
        self.assertIn("ParsedMatchEvent::Request", self.src)
        self.assertIn("ParsedMatchEvent::Finding", self.src)
        # Both must be present (unwrap of Option)
        self.assertIn("let req = request?;", self.src)
        self.assertIn("let fin = finding?;", self.src)

    def test_format_week_key_uses_iso_week(self) -> None:
        """Cache key window comes from chrono iso_week — same as legacy."""
        self.assertIn("fn format_week_key", self.src)
        self.assertIn("iso_week", self.src)
        self.assertIn('"{}-W{:02}"', self.src)

    def test_strength_stored_as_json_string(self) -> None:
        """serde_json::Value isn't Eq/Hash/Ord; stored as JSON string."""
        self.assertIn("strength_per_dimension_json: String", self.src)

    def test_lookup_supports_both_match_id_and_cache_key(self) -> None:
        """The legacy view supports lookup-by-cache-key
        (`requester|target|window`) via `lookup()`. The DD version's
        query_bytes must support both."""
        self.assertIn("cache_key()", self.src)
        # The query_bytes should contain a fallback to cache_key
        m = re.search(r"fn query_bytes.*?\n    \}", self.src, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn("cache_key", m.group(0))

    def test_inline_tests(self) -> None:
        for t in [
            "fn fold_returns_none_when_only_request",
            "fn fold_returns_none_when_only_finding",
            "fn fold_request_and_finding_produces_cache_entry",
            "fn format_week_key_shape",
            "fn parse_request_keys_by_match_id",
            "fn query_supports_cache_key_lookup",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# matched_against
# =============================================================================

class TestMatchedAgainstView(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = f"{VIEW_DIR}/matched_against.rs"
        if not os.path.exists(path):
            raise unittest.SkipTest(f"missing {path}")
        cls.src = _read(path)

    def test_view_shape(self) -> None:
        assert_dd_view_shape(
            self, self.src,
            view_name="matched_against",
            subscribed_kinds=["match_request_received", "finding_emitted"],
        )

    def test_request_alone_produces_open_entry(self) -> None:
        """Unlike match_cache, matched_against emits the entry as soon
        as the request arrives (with completed_at_ms = None). The
        finding just stamps the completion timestamp."""
        # Request is sufficient — no `finding?` unwrap on a finding var
        self.assertIn("let (offset, p, ts) = request?;", self.src)
        # Completion is optional
        self.assertIn("completed_at_ms: completion_ts", self.src)

    def test_completed_at_ms_optional(self) -> None:
        self.assertIn("pub completed_at_ms: Option<u64>", self.src)

    def test_inline_tests(self) -> None:
        for t in [
            "fn fold_request_only_produces_open_entry",
            "fn fold_request_then_finding_stamps_completion",
            "fn fold_finding_only_returns_none",
            "fn parse_request_keys_by_match_id",
            "fn snapshot_query_legacy_shape",
            "fn snapshot_query_completed_null_when_open",
        ]:
            self.assertIn(t, self.src)


# =============================================================================
# Module registration
# =============================================================================

class TestSubsliceERegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(VIEWS_MOD_PATH):
            raise unittest.SkipTest("views/mod.rs missing")
        cls.src = _read(VIEWS_MOD_PATH)

    def test_all_five_registered(self) -> None:
        for name in [
            "current_schemas",
            "open_contradictions",
            "pending_review",
            "match_cache",
            "matched_against",
        ]:
            self.assertIn(f"pub mod {name}", self.src)


# =============================================================================
# Behavioral tests — skipped until extension built
# =============================================================================

def _has_rust_dataflow() -> bool:
    try:
        mp = importlib.import_module("mempalace_core")
    except ImportError:
        return False
    return hasattr(mp, "PyDataflowHandle")


@unittest.skipUnless(
    _has_rust_dataflow(),
    "Rust extension with DD handle not built (sub-slice H wires this up)",
)
class TestSubsliceEBehavioral(unittest.TestCase):
    def test_open_contradictions_resolution_retracts(self) -> None:
        import mempalace_core as mp  # type: ignore
        import json
        handle = mp.PyDataflowHandle.start(["open_contradictions"])
        try:
            handle.feed(
                offset=1,
                kind="contradiction_asserted",
                payload=json.dumps({
                    "edge_id": "edg_c1",
                    "contradicting_assertion_id": "asn_a",
                    "contradicted_assertion_id": "asn_b",
                }).encode(),
            )
            handle.advance_to(1)
            v = json.loads(handle.query("open_contradictions", b"edg_c1"))
            self.assertEqual(v["edge_id"], "edg_c1")

            handle.feed(
                offset=2,
                kind="contradiction_resolved",
                payload=json.dumps({"edge_id": "edg_c1"}).encode(),
            )
            handle.advance_to(2)
            self.assertIsNone(handle.query("open_contradictions", b"edg_c1"))
        finally:
            handle.shutdown()

    def test_match_cache_requires_both_events(self) -> None:
        import mempalace_core as mp  # type: ignore
        import json
        handle = mp.PyDataflowHandle.start(["match_cache"])
        try:
            handle.feed(
                offset=1,
                kind="match_request_received",
                payload=json.dumps({
                    "match_id": "m1",
                    "requester_pubkey": "pk_a",
                    "target_palace_id": "pk_b",
                    "_timestamp_ms": 1000,
                }).encode(),
            )
            handle.advance_to(1)
            # Request alone shouldn't produce a cache entry
            self.assertIsNone(handle.query("match_cache", b"m1"))

            handle.feed(
                offset=2,
                kind="finding_emitted",
                payload=json.dumps({
                    "match_id": "m1",
                    "topology": "peer",
                    "_timestamp_ms": 2000,
                }).encode(),
            )
            handle.advance_to(2)
            v = json.loads(handle.query("match_cache", b"m1"))
            self.assertEqual(v["finding_topology"], "peer")
        finally:
            handle.shutdown()


if __name__ == "__main__":
    unittest.main()
