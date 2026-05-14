"""Tests for Track 6A-C — topology + phone-decrypt + invalidate.

Covers:
  - TopologyBrowser pagination (nodes, edges, drawers, segments).
  - TopologyBrowser filtering (by node_kind, edge_kind, source/target).
  - TopologyBrowser stats.
  - TopologyBrowser respects invalidation flags.
  - PhoneDecryptEndpoint returns ciphertext envelope without decrypting.
  - PhoneDecryptEndpoint raises DrawerNotFoundError, DrawerNotEncryptedError,
    DrawerInvalidatedError appropriately.
  - End-to-end: encrypt drawer → fetch envelope → phone decrypts.
  - invalidate_drawer + revalidate_drawer + view consistency.
  - invalidate_node, invalidate_edge.
  - Cross-flow: invalidate then check phone-decrypt rejects.
"""

from __future__ import annotations

import unittest

from mempalace.drawer.capture import capture_drawer
from mempalace.embed.client import EmbeddingStore, InMemoryBackend
from mempalace.embed.model import EmbeddingService
from mempalace.log.client import LogClient
from mempalace.schema.events import EdgeCreated, NodeCreated
from mempalace.schema.identifiers import (
    make_drawer_id,
    make_edge_id,
    make_entity_id,
    make_event_id_log,
    make_id,
    make_theme_id,
)
from mempalace.schema.kinds import EdgeKind, InteractionalKind, NodeKind
from mempalace.secure import SoftwareCloudBoxKM, SoftwarePhoneSE
from mempalace.tests.conftest import fresh_palace, reset_module_state
from mempalace.views import current as current_views
from mempalace.views.invalidate import (
    invalidate_drawer,
    invalidate_edge,
    invalidate_node,
    revalidate_drawer,
    revalidate_edge,
    revalidate_node,
)
from mempalace.views.phone_decrypt import (
    DrawerCiphertextEnvelope,
    DrawerInvalidatedError,
    DrawerNotEncryptedError,
    DrawerNotFoundError,
    PhoneDecryptEndpoint,
)
from mempalace.views.topology import (
    DEFAULT_PAGE_SIZE,
    TopologyBrowser,
)


DAEMON_ATTESTATION = b"daemon-binary-hash-test"


def _fresh_pair():
    se = SoftwarePhoneSE()
    manager = SoftwareCloudBoxKM()
    bundle = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
    manager.load_bundle(bundle, daemon_binary_attestation=DAEMON_ATTESTATION)
    return se, manager


def _capture_one(log: LogClient, transcript: str = "hello", *, se=None) -> str:
    """Capture a drawer and return its drawer_id."""
    result = capture_drawer(
        transcript=transcript,
        actor="test",
        duration_ms=1000,
        log_client=log,
        embedding_service=EmbeddingService(),
        embedding_store=EmbeddingStore(backend=InMemoryBackend()),
        interactional=InteractionalKind.MEMO_TO_SELF,
        secure_element=se,
    )
    return result.drawer_id


def _seed_node(log: LogClient, *, kind: NodeKind = NodeKind.ENTITY) -> str:
    """Append a NodeCreated event; return node_id."""
    if kind == NodeKind.ENTITY:
        nid = make_entity_id()
    elif kind == NodeKind.THEME:
        nid = make_theme_id()
    else:
        nid = make_id("ent")
    evt = NodeCreated(
        event_id=make_event_id_log(),
        recorded_at=1000,
        actor="test",
        node_id=nid,
        node_kind=kind.value,
        properties={},
    )
    result = log.append(evt)
    assert result.accepted, f"validation failed: {result.validation}"
    return nid


def _seed_edge(
    log: LogClient,
    *,
    source: str,
    target: str,
    kind: EdgeKind = EdgeKind.ASSERTED_SUBJECT,
) -> str:
    eid = make_edge_id()
    evt = EdgeCreated(
        event_id=make_event_id_log(),
        recorded_at=1000,
        actor="test",
        edge_id=eid,
        edge_kind=kind.value,
        source_node_id=source,
        target_node_id=target,
    )
    result = log.append(evt)
    assert result.accepted, f"validation failed: {result.validation}"
    return eid


# =============================================================================
# Track 6A — Topology browser
# =============================================================================


class TestTopologyBrowserStats(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_empty_palace_stats(self) -> None:
        log = fresh_palace()["log"]
        browser = TopologyBrowser(log_client=log)
        stats = browser.stats()
        self.assertEqual(stats["active_nodes"], 0)
        self.assertEqual(stats["active_edges"], 0)
        self.assertEqual(stats["all_drawers"], 0)
        self.assertEqual(stats["invalidated_drawers"], 0)

    def test_populated_palace_stats(self) -> None:
        log = fresh_palace()["log"]
        n1 = _seed_node(log, kind=NodeKind.ENTITY)
        n2 = _seed_node(log, kind=NodeKind.THEME)
        _seed_edge(log, source=n1, target=n2)
        _capture_one(log, "first")
        _capture_one(log, "second")

        browser = TopologyBrowser(log_client=log)
        stats = browser.stats()
        self.assertEqual(stats["active_nodes"], 2)
        self.assertEqual(stats["active_edges"], 1)
        self.assertEqual(stats["all_drawers"], 2)


class TestTopologyBrowserNodes(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_list_nodes_empty(self) -> None:
        log = fresh_palace()["log"]
        browser = TopologyBrowser(log_client=log)
        page = browser.list_nodes()
        self.assertEqual(page.nodes, [])
        self.assertIsNone(page.next_cursor)
        self.assertEqual(page.total_count, 0)

    def test_list_nodes_with_data(self) -> None:
        log = fresh_palace()["log"]
        node_ids = [_seed_node(log) for _ in range(3)]
        browser = TopologyBrowser(log_client=log)
        page = browser.list_nodes()
        self.assertEqual(page.total_count, 3)
        self.assertEqual(len(page.nodes), 3)
        # Each node dict has the structural fields
        for nd in page.nodes:
            self.assertIn("node_id", nd)
            self.assertIn("node_kind", nd)
            self.assertIn("is_active", nd)
            self.assertTrue(nd["is_active"])
        returned_ids = {nd["node_id"] for nd in page.nodes}
        self.assertEqual(returned_ids, set(node_ids))

    def test_filter_by_node_kind(self) -> None:
        log = fresh_palace()["log"]
        e1 = _seed_node(log, kind=NodeKind.ENTITY)
        e2 = _seed_node(log, kind=NodeKind.ENTITY)
        t1 = _seed_node(log, kind=NodeKind.THEME)
        browser = TopologyBrowser(log_client=log)

        entities = browser.list_nodes(node_kind=NodeKind.ENTITY)
        self.assertEqual(entities.total_count, 2)
        self.assertEqual(
            {n["node_id"] for n in entities.nodes}, {e1, e2}
        )

        themes = browser.list_nodes(node_kind=NodeKind.THEME)
        self.assertEqual(themes.total_count, 1)
        self.assertEqual(themes.nodes[0]["node_id"], t1)

    def test_pagination(self) -> None:
        log = fresh_palace()["log"]
        for _ in range(7):
            _seed_node(log)
        browser = TopologyBrowser(log_client=log)

        # Page 1: 3 nodes
        p1 = browser.list_nodes(page_size=3)
        self.assertEqual(len(p1.nodes), 3)
        self.assertIsNotNone(p1.next_cursor)

        # Page 2: 3 more
        p2 = browser.list_nodes(page_size=3, cursor=p1.next_cursor)
        self.assertEqual(len(p2.nodes), 3)
        self.assertIsNotNone(p2.next_cursor)

        # Page 3: last 1
        p3 = browser.list_nodes(page_size=3, cursor=p2.next_cursor)
        self.assertEqual(len(p3.nodes), 1)
        self.assertIsNone(p3.next_cursor)

        # All distinct
        all_ids = (
            {n["node_id"] for n in p1.nodes}
            | {n["node_id"] for n in p2.nodes}
            | {n["node_id"] for n in p3.nodes}
        )
        self.assertEqual(len(all_ids), 7)

    def test_invalidated_node_excluded_by_default(self) -> None:
        log = fresh_palace()["log"]
        n1 = _seed_node(log)
        n2 = _seed_node(log)
        invalidate_node(n1, log_client=log)
        browser = TopologyBrowser(log_client=log)

        page = browser.list_nodes()
        self.assertEqual(page.total_count, 1)
        self.assertEqual(page.nodes[0]["node_id"], n2)

        # With include_invalidated, n1 reappears
        page_all = browser.list_nodes(include_invalidated=True)
        self.assertEqual(page_all.total_count, 2)


class TestTopologyBrowserEdges(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_list_edges_with_filters(self) -> None:
        log = fresh_palace()["log"]
        a = _seed_node(log)
        b = _seed_node(log)
        c = _seed_node(log)
        e1 = _seed_edge(log, source=a, target=b, kind=EdgeKind.ASSERTED_SUBJECT)
        e2 = _seed_edge(log, source=a, target=c, kind=EdgeKind.ASSERTED_OBJECT)
        e3 = _seed_edge(log, source=b, target=c, kind=EdgeKind.ASSERTED_SUBJECT)

        browser = TopologyBrowser(log_client=log)

        # Filter by source
        from_a = browser.list_edges(source_node_id=a)
        self.assertEqual(from_a.total_count, 2)

        # Filter by target
        to_c = browser.list_edges(target_node_id=c)
        self.assertEqual(to_c.total_count, 2)

        # Filter by kind
        subj_edges = browser.list_edges(edge_kind=EdgeKind.ASSERTED_SUBJECT)
        self.assertEqual(subj_edges.total_count, 2)

    def test_invalidated_edge_excluded_by_default(self) -> None:
        log = fresh_palace()["log"]
        a = _seed_node(log)
        b = _seed_node(log)
        e1 = _seed_edge(log, source=a, target=b)
        e2 = _seed_edge(log, source=a, target=b)
        invalidate_edge(e1, log_client=log)
        browser = TopologyBrowser(log_client=log)

        active = browser.list_edges()
        self.assertEqual(active.total_count, 1)
        self.assertEqual(active.edges[0]["edge_id"], e2)


class TestTopologyBrowserDrawers(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_list_drawers(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "first")
        d2 = _capture_one(log, "second")
        d3 = _capture_one(log, "third")

        browser = TopologyBrowser(log_client=log)
        page = browser.list_drawers()
        self.assertEqual(page.total_count, 3)
        self.assertEqual(
            {d["drawer_id"] for d in page.drawers}, {d1, d2, d3}
        )

    def test_get_drawer(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "a", se=None)

        browser = TopologyBrowser(log_client=log)
        drawer = browser.get_drawer(d1)
        self.assertIsNotNone(drawer)
        self.assertEqual(drawer["drawer_id"], d1)
        self.assertEqual(drawer["interactional"], "memo_to_self")
        # Legacy capture is not encrypted
        self.assertFalse(drawer["is_encrypted"])
        self.assertEqual(drawer["encryption_schema_version"], "v0")

    def test_get_drawer_encrypted_marks_correctly(self) -> None:
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "secret content", se=se)

        browser = TopologyBrowser(log_client=log)
        drawer = browser.get_drawer(d1)
        self.assertTrue(drawer["is_encrypted"])
        self.assertEqual(drawer["encryption_schema_version"], "v2")

    def test_get_drawer_not_found(self) -> None:
        log = fresh_palace()["log"]
        browser = TopologyBrowser(log_client=log)
        self.assertIsNone(browser.get_drawer("drw_not_a_real_id"))

    def test_invalidated_drawer_excluded_by_default(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "a")
        d2 = _capture_one(log, "b")
        invalidate_drawer(d1, log_client=log)

        browser = TopologyBrowser(log_client=log)
        page = browser.list_drawers()
        self.assertEqual(page.total_count, 1)
        self.assertEqual(page.drawers[0]["drawer_id"], d2)

        # include_invalidated brings d1 back
        page_all = browser.list_drawers(include_invalidated=True)
        self.assertEqual(page_all.total_count, 2)


# =============================================================================
# Track 6B — Phone-only-decrypt endpoint
# =============================================================================


class TestPhoneDecryptEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_returns_envelope_for_encrypted_drawer(self) -> None:
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "encrypted content", se=se)

        ep = PhoneDecryptEndpoint(log_client=log)
        envelope = ep.fetch_drawer_ciphertext(d1)
        self.assertIsInstance(envelope, DrawerCiphertextEnvelope)
        self.assertEqual(envelope.drawer_id, d1)
        self.assertNotEqual(envelope.ciphertext, b"")
        self.assertNotEqual(envelope.dek_handle, "")
        self.assertNotEqual(envelope.attestation_sig, b"")
        self.assertNotEqual(envelope.content_hash, "")

    def test_envelope_does_not_contain_plaintext(self) -> None:
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        secret = "this is the secret payload that must not leak"
        d1 = _capture_one(log, secret, se=se)

        ep = PhoneDecryptEndpoint(log_client=log)
        envelope = ep.fetch_drawer_ciphertext(d1)
        self.assertNotIn(secret.encode("utf-8"), envelope.ciphertext)

    def test_phone_can_decrypt_envelope(self) -> None:
        """The full round-trip: cloud-box ships envelope, phone decrypts."""
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        secret = "phone-readable content"
        d1 = _capture_one(log, secret, se=se)

        ep = PhoneDecryptEndpoint(log_client=log)
        envelope = ep.fetch_drawer_ciphertext(d1)

        # Phone-side decryption — using the same SE that did the encrypt
        plaintext = se.decrypt(
            envelope.ciphertext,
            dek_handle=envelope.dek_handle,
            attestation_sig=envelope.attestation_sig,
        )
        self.assertEqual(plaintext, secret.encode("utf-8"))

    def test_drawer_not_found(self) -> None:
        log = fresh_palace()["log"]
        ep = PhoneDecryptEndpoint(log_client=log)
        with self.assertRaises(DrawerNotFoundError):
            ep.fetch_drawer_ciphertext("drw_not_a_real_id")

    def test_legacy_drawer_raises_not_encrypted(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "legacy plaintext")  # no SE
        ep = PhoneDecryptEndpoint(log_client=log)
        with self.assertRaises(DrawerNotEncryptedError):
            ep.fetch_drawer_ciphertext(d1)

    def test_invalidated_drawer_blocked_by_default(self) -> None:
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "x", se=se)
        invalidate_drawer(d1, log_client=log)

        ep = PhoneDecryptEndpoint(log_client=log)
        with self.assertRaises(DrawerInvalidatedError):
            ep.fetch_drawer_ciphertext(d1)

        # Explicitly allowed
        envelope = ep.fetch_drawer_ciphertext(d1, allow_invalidated=True)
        self.assertEqual(envelope.drawer_id, d1)


# =============================================================================
# Track 6C — Invalidate flow
# =============================================================================


class TestInvalidateDrawer(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_invalidate_then_check(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "x")
        self.assertFalse(current_views.is_drawer_invalidated(d1))

        invalidate_drawer(d1, log_client=log)
        current_views.tick_views()
        self.assertTrue(current_views.is_drawer_invalidated(d1))

        when = current_views.drawer_invalidated_at(d1)
        self.assertIsNotNone(when)
        self.assertGreater(when, 0)

    def test_revalidate_clears_flag(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "x")
        invalidate_drawer(d1, log_client=log)
        current_views.tick_views()
        self.assertTrue(current_views.is_drawer_invalidated(d1))

        revalidate_drawer(d1, log_client=log)
        current_views.tick_views()
        self.assertFalse(current_views.is_drawer_invalidated(d1))

    def test_invalidate_idempotent_at_view_level(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "x")
        invalidate_drawer(d1, log_client=log)
        invalidate_drawer(d1, log_client=log)
        invalidate_drawer(d1, log_client=log)
        current_views.tick_views()
        # Still just one entry in the invalidated_drawers map
        self.assertTrue(current_views.is_drawer_invalidated(d1))
        self.assertEqual(
            current_views.invalidated_drawer_ids(),
            [d1],
        )

    def test_invalidated_drawer_ids_lists_all(self) -> None:
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "a")
        d2 = _capture_one(log, "b")
        d3 = _capture_one(log, "c")
        invalidate_drawer(d1, log_client=log)
        invalidate_drawer(d3, log_client=log)
        current_views.tick_views()
        invalidated = set(current_views.invalidated_drawer_ids())
        self.assertEqual(invalidated, {d1, d3})


class TestInvalidateNode(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_invalidate_node_marks_state(self) -> None:
        log = fresh_palace()["log"]
        n1 = _seed_node(log)
        current_views.tick_views()
        node = current_views.current_node(n1)
        self.assertIsNotNone(node)
        self.assertTrue(node.is_active())

        invalidate_node(n1, log_client=log)
        current_views.tick_views()
        node = current_views.current_node(n1)
        self.assertFalse(node.is_active())
        self.assertIsNotNone(node.invalidated_at)

    def test_revalidate_node_clears(self) -> None:
        log = fresh_palace()["log"]
        n1 = _seed_node(log)
        invalidate_node(n1, log_client=log)
        current_views.tick_views()
        revalidate_node(n1, log_client=log)
        current_views.tick_views()
        node = current_views.current_node(n1)
        self.assertTrue(node.is_active())


class TestInvalidateEdge(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_invalidate_then_revalidate(self) -> None:
        log = fresh_palace()["log"]
        a = _seed_node(log)
        b = _seed_node(log)
        e1 = _seed_edge(log, source=a, target=b)
        current_views.tick_views()
        edge = current_views.current_edge(e1)
        self.assertTrue(edge.is_active())

        invalidate_edge(e1, log_client=log)
        current_views.tick_views()
        edge = current_views.current_edge(e1)
        self.assertFalse(edge.is_active())

        revalidate_edge(e1, log_client=log)
        current_views.tick_views()
        edge = current_views.current_edge(e1)
        self.assertTrue(edge.is_active())

    def test_invalidated_edge_excluded_from_traversal(self) -> None:
        """Outgoing/incoming edge views filter out invalidated edges."""
        log = fresh_palace()["log"]
        a = _seed_node(log)
        b = _seed_node(log)
        e1 = _seed_edge(log, source=a, target=b)
        current_views.tick_views()

        out_before = current_views.outgoing_edges(a)
        self.assertEqual(len(out_before), 1)

        invalidate_edge(e1, log_client=log)
        current_views.tick_views()

        out_after = current_views.outgoing_edges(a)
        self.assertEqual(len(out_after), 0)


# =============================================================================
# Cross-flow: Topology + Invalidate + Phone-decrypt
# =============================================================================


class TestEndToEndUserFlows(unittest.TestCase):
    """The full user-facing flow: capture → browse topology →
    fetch ciphertext → invalidate → topology hides it →
    phone-decrypt rejects → revalidate → all back."""

    def setUp(self) -> None:
        reset_module_state()

    def test_full_user_journey(self) -> None:
        se, _ = _fresh_pair()
        log = fresh_palace()["log"]
        d1 = _capture_one(log, "private content", se=se)
        browser = TopologyBrowser(log_client=log)
        ep = PhoneDecryptEndpoint(log_client=log)

        # 1. Topology view sees the drawer
        page = browser.list_drawers()
        self.assertEqual(page.total_count, 1)
        self.assertEqual(page.drawers[0]["drawer_id"], d1)

        # 2. Phone fetches ciphertext
        envelope = ep.fetch_drawer_ciphertext(d1)
        plaintext = se.decrypt(
            envelope.ciphertext,
            dek_handle=envelope.dek_handle,
            attestation_sig=envelope.attestation_sig,
        )
        self.assertEqual(plaintext, b"private content")

        # 3. User decides to hide it
        invalidate_drawer(d1, log_client=log)

        # 4. Topology view excludes it by default
        current_views.tick_views()
        page = browser.list_drawers()
        self.assertEqual(page.total_count, 0)

        # 5. Phone-decrypt refuses by default
        with self.assertRaises(DrawerInvalidatedError):
            ep.fetch_drawer_ciphertext(d1)

        # 6. User changes their mind; revalidate
        revalidate_drawer(d1, log_client=log)

        # 7. Everything's back
        current_views.tick_views()
        page = browser.list_drawers()
        self.assertEqual(page.total_count, 1)
        envelope = ep.fetch_drawer_ciphertext(d1)
        self.assertEqual(envelope.drawer_id, d1)


if __name__ == "__main__":
    unittest.main()
