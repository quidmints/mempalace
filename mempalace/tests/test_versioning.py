"""Tests for Phase 2 — content-version stamps on artifacts.

Covers:
  - hash_content / hash_dependency_snapshot determinism
  - VersionStamp content equality / dependency equality
  - SubstrateVersionTracker
  - SignatureSnapshot is stamped on build
  - ProposalRecord is stamped on add
  - Staleness detection: an artifact stamped against substrate v17 is
    detectably-stale once substrate moves to v23
"""

from __future__ import annotations

import unittest

from mempalace.schema.versioning import (
    EMPTY_CONTENT_HASH,
    EMPTY_DEPENDENCY_HASH,
    SubstrateVersionTracker,
    VersionStamp,
    hash_content,
    hash_dependency_snapshot,
    make_stamp,
)
from mempalace.tests.conftest import reset_module_state


class TestHashFunctions(unittest.TestCase):
    def test_hash_content_deterministic(self) -> None:
        a = hash_content(b"hello world")
        b = hash_content(b"hello world")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_hash_content_distinguishes_payloads(self) -> None:
        a = hash_content(b"x")
        b = hash_content(b"y")
        self.assertNotEqual(a, b)

    def test_hash_dependency_snapshot_order_independent(self) -> None:
        a = hash_dependency_snapshot([("k1", 1), ("k2", 2)])
        b = hash_dependency_snapshot([("k2", 2), ("k1", 1)])
        self.assertEqual(a, b)

    def test_hash_dependency_snapshot_distinguishes_versions(self) -> None:
        a = hash_dependency_snapshot([("k", 1)])
        b = hash_dependency_snapshot([("k", 2)])
        self.assertNotEqual(a, b)

    def test_hash_dependency_snapshot_no_collision_via_concatenation(self) -> None:
        # If we naively concatenated keys, "ab" + "c" might collide
        # with "a" + "bc". The length-prefixed encoding prevents this.
        a = hash_dependency_snapshot([("ab", 1), ("c", 1)])
        b = hash_dependency_snapshot([("a", 1), ("bc", 1)])
        self.assertNotEqual(a, b)

    def test_empty_dependency_snapshot_returns_known_value(self) -> None:
        self.assertEqual(hash_dependency_snapshot([]), EMPTY_DEPENDENCY_HASH)


class TestVersionStamp(unittest.TestCase):
    def test_default_stamp_is_unstamped(self) -> None:
        s = VersionStamp()
        self.assertFalse(s.is_stamped)
        self.assertEqual(s.content_hash, EMPTY_CONTENT_HASH)
        self.assertEqual(s.dependency_version_snapshot_hash, EMPTY_DEPENDENCY_HASH)

    def test_make_stamp_produces_stamped(self) -> None:
        s = make_stamp(content=b"payload", log_offset=42, dependencies=[("k", 1)])
        self.assertTrue(s.is_stamped)
        self.assertEqual(s.computed_at_log_offset, 42)
        self.assertEqual(len(s.dependency_version_snapshot), 1)

    def test_matches_content_for_same_payload(self) -> None:
        s1 = make_stamp(content=b"x", log_offset=1, dependencies=[("k", 1)])
        s2 = make_stamp(content=b"x", log_offset=999, dependencies=[("k", 99)])
        self.assertTrue(s1.matches_content(s2))
        self.assertFalse(s1.matches_dependencies(s2))

    def test_is_stale_against_when_dependency_advanced(self) -> None:
        s = make_stamp(content=b"x", log_offset=10, dependencies=[("dep1", 5)])
        # Same version → not stale
        self.assertFalse(s.is_stale_against({"dep1": 5}))
        # Dep advanced → stale
        self.assertTrue(s.is_stale_against({"dep1": 6}))

    def test_is_stale_against_when_dependency_disappeared(self) -> None:
        s = make_stamp(content=b"x", log_offset=10, dependencies=[("dep1", 5)])
        # No record of the dep — conservatively stale
        self.assertTrue(s.is_stale_against({}))

    def test_empty_snapshot_conservatively_stale(self) -> None:
        # A stamp with no recorded dependencies can't be proven fresh
        s = make_stamp(content=b"x", log_offset=10, dependencies=[])
        self.assertTrue(s.is_stale_against({"dep": 1}))


class TestSubstrateVersionTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.t = SubstrateVersionTracker()

    def test_unrecorded_field_returns_zero(self) -> None:
        self.assertEqual(self.t.version_of("never_seen"), 0)

    def test_record_change_advances_version(self) -> None:
        self.t.record_change("k", 5)
        self.assertEqual(self.t.version_of("k"), 5)

    def test_record_change_only_advances(self) -> None:
        self.t.record_change("k", 10)
        self.t.record_change("k", 5)  # earlier offset
        self.assertEqual(self.t.version_of("k"), 10)

    def test_snapshot_returns_sorted(self) -> None:
        self.t.record_change("z", 3)
        self.t.record_change("a", 1)
        self.t.record_change("m", 2)
        snap = self.t.snapshot(["a", "z", "m"])
        # Sorted by key
        self.assertEqual(snap, [("a", 1), ("m", 2), ("z", 3)])


class TestSignatureSnapshotStamped(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_build_signature_snapshot_stamps_with_content(self) -> None:
        from mempalace.signatures.store import build_signature_snapshot

        snap = build_signature_snapshot(
            period_id="p1",
            window_start_ms=1000,
            window_end_ms=2000,
            mean_position_by_theme={"t1": [0.1, 0.2]},
            velocity_by_theme={"t1": 0.5},
            schema_fingerprints=["sf_a", "sf_b"],
            log_offset=100,
            dependencies=[("substrate:t1:embedding", 50)],
        )
        self.assertTrue(snap.version_stamp.is_stamped)
        self.assertEqual(snap.version_stamp.computed_at_log_offset, 100)
        self.assertEqual(len(snap.version_stamp.dependency_version_snapshot), 1)

    def test_two_snapshots_with_same_content_have_same_content_hash(self) -> None:
        from mempalace.signatures.store import build_signature_snapshot

        s1 = build_signature_snapshot(
            period_id="p1", window_start_ms=1000, window_end_ms=2000,
            mean_position_by_theme={"t1": [0.5]},
            now_ms=1_000_000,
        )
        s2 = build_signature_snapshot(
            period_id="p1", window_start_ms=1000, window_end_ms=2000,
            mean_position_by_theme={"t1": [0.5]},
            now_ms=2_000_000,  # different timestamp
        )
        # snapshot_id differs (uuid in it), captured_at_ms differs,
        # but the *content hash* should be identical because we
        # exclude those fields from the canonical-bytes
        self.assertEqual(
            s1.version_stamp.content_hash,
            s2.version_stamp.content_hash,
        )

    def test_changing_velocity_changes_content_hash(self) -> None:
        from mempalace.signatures.store import build_signature_snapshot

        s1 = build_signature_snapshot(
            period_id="p", window_start_ms=0, window_end_ms=10,
            velocity_by_theme={"t": 0.5},
        )
        s2 = build_signature_snapshot(
            period_id="p", window_start_ms=0, window_end_ms=10,
            velocity_by_theme={"t": 0.6},
        )
        self.assertNotEqual(
            s1.version_stamp.content_hash,
            s2.version_stamp.content_hash,
        )


class TestProposalStamping(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def _stub_proposal(self, pid: str, value: dict | None = None):
        from mempalace.miner.base import ProposalLifecycle, ProposalRecord
        return ProposalRecord(
            proposal_id=pid, proposal_kind="memory_type",
            proposed_value=value or {"x": pid}, confidence=0.8,
            miner_class=1, lifecycle=ProposalLifecycle.PROVISIONAL,
        )

    def test_proposal_stamped_on_add_if_unstamped(self) -> None:
        from mempalace.miner.proposals import ProposalStore

        store = ProposalStore()
        record = self._stub_proposal("p1")
        self.assertFalse(record.version_stamp.is_stamped)
        result = store.add(record, log_offset=100)
        self.assertTrue(result.entry.record.version_stamp.is_stamped)
        self.assertEqual(
            result.entry.record.version_stamp.computed_at_log_offset, 100,
        )

    def test_pre_stamped_proposal_passes_through(self) -> None:
        from mempalace.miner.proposals import ProposalStore

        store = ProposalStore()
        record = self._stub_proposal("p1")
        record.version_stamp = make_stamp(
            content=b"miner-supplied",
            log_offset=42,
            dependencies=[("d", 1)],
        )
        original_hash = record.version_stamp.content_hash
        result = store.add(record, log_offset=999)  # log_offset ignored
        self.assertEqual(
            result.entry.record.version_stamp.content_hash,
            original_hash,
        )
        self.assertEqual(
            result.entry.record.version_stamp.computed_at_log_offset, 42,
        )

    def test_proposals_with_same_content_share_content_hash(self) -> None:
        from mempalace.miner.proposals import ProposalStore

        store = ProposalStore()
        # Two different proposal_ids but same content
        r1 = self._stub_proposal("pa", value={"k": "v"})
        r2 = self._stub_proposal("pb", value={"k": "v"})
        store.add(r1, log_offset=10)
        store.add(r2, log_offset=20)
        self.assertEqual(
            r1.version_stamp.content_hash,
            r2.version_stamp.content_hash,
        )


class TestStalenessDetection(unittest.TestCase):
    """The keystone Phase 2 property: an artifact stamped against
    substrate frontier T can be checked against current frontier T+k
    and reported as stale-because-X-changed."""

    def setUp(self) -> None:
        reset_module_state()
        self.tracker = SubstrateVersionTracker()
        # Initial substrate state
        self.tracker.record_change("substrate:n1:weight", 10)
        self.tracker.record_change("substrate:n1:summary", 11)
        self.tracker.record_change("substrate:n2:label", 12)

    def test_artifact_stamped_then_dep_advances_is_stale(self) -> None:
        # Artifact reads n1:weight at v=10, n2:label at v=12
        deps = self.tracker.snapshot(["substrate:n1:weight", "substrate:n2:label"])
        artifact_stamp = make_stamp(
            content=b"derived value", log_offset=12, dependencies=deps,
        )

        # Substrate now: n1:weight changed
        self.tracker.record_change("substrate:n1:weight", 20)

        # Stale check
        self.assertTrue(artifact_stamp.is_stale_against(self.tracker.all_versions()))

    def test_artifact_stale_only_when_dependency_advances(self) -> None:
        # Artifact reads n1:weight only
        deps = self.tracker.snapshot(["substrate:n1:weight"])
        artifact_stamp = make_stamp(
            content=b"x", log_offset=10, dependencies=deps,
        )

        # Substrate: n1:summary advances (NOT a dependency of this artifact)
        self.tracker.record_change("substrate:n1:summary", 100)

        # Artifact should NOT be stale — its declared deps haven't changed
        self.assertFalse(artifact_stamp.is_stale_against(self.tracker.all_versions()))

    def test_two_artifacts_with_matching_dep_hashes_share_frontier(self) -> None:
        deps = self.tracker.snapshot(["substrate:n1:weight"])
        s1 = make_stamp(content=b"out_a", log_offset=10, dependencies=deps)
        s2 = make_stamp(content=b"out_b", log_offset=10, dependencies=deps)
        # Different content, same deps
        self.assertFalse(s1.matches_content(s2))
        self.assertTrue(s1.matches_dependencies(s2))

    def test_dependency_hash_changes_when_a_dep_advances(self) -> None:
        deps_v1 = self.tracker.snapshot(["substrate:n1:weight"])
        s1 = make_stamp(content=b"x", log_offset=10, dependencies=deps_v1)

        # Advance a dep
        self.tracker.record_change("substrate:n1:weight", 20)
        deps_v2 = self.tracker.snapshot(["substrate:n1:weight"])
        s2 = make_stamp(content=b"x", log_offset=20, dependencies=deps_v2)

        # Same content payload, but the *dependency hashes* differ —
        # this lets a downstream cache key on (content_hash,
        # dep_hash) and recompute when deps change even if content
        # hasn't been recomputed yet.
        self.assertTrue(s1.matches_content(s2))
        self.assertFalse(s1.matches_dependencies(s2))


if __name__ == "__main__":
    unittest.main()
