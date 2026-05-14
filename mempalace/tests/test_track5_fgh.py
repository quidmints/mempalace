"""Tests for Track 5F (federation egress), 5G (disk-at-rest), 5H
(burn-recovery quarantine).

Covers:
  Track 5F:
    - encrypt_with_fek / decrypt_with_fek round-trip across two
      independent cloud-box key managers.
    - Wrong sandbox_id, wrong FEK, tampered ciphertext all fail.
    - End-to-end: build + sign Finding → wrap → unwrap → recovers
      identical Finding with signature preserved.
    - derive_fek_from_shared_secret: same inputs → same FEK across
      two callers; different sandbox_id → different FEK.

  Track 5G:
    - derive_dark deterministic from master_key.
    - EncryptedLogBackend round-trips: append + read_range both work
      transparently when DARK is set.
    - Cold-start without DARK: a fresh client over the same backend
      sees encrypted blobs (can't read structural data).
    - require_encryption=True + no DARK refuses appends.
    - Tombstoning passthrough: rewrite_payload re-encrypts.

  Track 5H:
    - scan_for_unreadable_events distinguishes decrypted_ok / erased
      / unreadable.
    - After "burn" (DARK removed), scan finds unreadable events.
    - has_burn_fallout signal works.
"""

from __future__ import annotations

import secrets
import unittest

from mempalace.federate.egress import (
    EncryptedFindingEnvelope,
    derive_fek_from_shared_secret,
    unwrap_finding,
    wrap_finding_for_egress,
)
from mempalace.federate.findings import Finding, FindingTopology
from mempalace.federate.session_keys import SessionKeyManager
from mempalace.log.at_rest import (
    EncryptedLogBackend,
    derive_dark,
    wrap_log_with_dark,
)
from mempalace.log.client import LogClient, MockBackend
from mempalace.schema.events import NodeCreated
from mempalace.schema.identifiers import make_entity_id, make_event_id_log
from mempalace.secure import AttestationError, SoftwareCloudBoxKM
from mempalace.secure.burn_recovery import (
    has_burn_fallout,
    scan_for_unreadable_events,
)
from mempalace.tests.conftest import reset_module_state


# =============================================================================
# Track 5F — FEK encryption round-trip
# =============================================================================


class TestFEKRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.fek = secrets.token_bytes(32)
        self.km_alice = SoftwareCloudBoxKM()
        self.km_bob = SoftwareCloudBoxKM()

    def test_roundtrip(self) -> None:
        plaintext = b"sensitive finding payload"
        ct = self.km_alice.encrypt_with_fek(
            plaintext, fek=self.fek, sandbox_id="sbx_match",
        )
        recovered = self.km_bob.decrypt_with_fek(
            ct, fek=self.fek, sandbox_id="sbx_match",
        )
        self.assertEqual(recovered, plaintext)

    def test_wrong_sandbox_id_fails(self) -> None:
        ct = self.km_alice.encrypt_with_fek(
            b"x", fek=self.fek, sandbox_id="sbx_one",
        )
        with self.assertRaises(AttestationError):
            self.km_bob.decrypt_with_fek(
                ct, fek=self.fek, sandbox_id="sbx_other",
            )

    def test_wrong_fek_fails(self) -> None:
        ct = self.km_alice.encrypt_with_fek(
            b"x", fek=self.fek, sandbox_id="sbx",
        )
        wrong_fek = secrets.token_bytes(32)
        with self.assertRaises(AttestationError):
            self.km_bob.decrypt_with_fek(
                ct, fek=wrong_fek, sandbox_id="sbx",
            )

    def test_tampered_ciphertext_fails(self) -> None:
        ct = bytearray(self.km_alice.encrypt_with_fek(
            b"x", fek=self.fek, sandbox_id="sbx",
        ))
        ct[15] ^= 0xFF  # flip a bit
        with self.assertRaises(AttestationError):
            self.km_bob.decrypt_with_fek(
                bytes(ct), fek=self.fek, sandbox_id="sbx",
            )

    def test_short_fek_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.km_alice.encrypt_with_fek(
                b"x", fek=b"too-short", sandbox_id="sbx",
            )


# =============================================================================
# Track 5F — derive_fek_from_shared_secret
# =============================================================================


class TestFEKDerivation(unittest.TestCase):
    def test_same_inputs_same_fek(self) -> None:
        secret = secrets.token_bytes(32)
        a = derive_fek_from_shared_secret(secret, sandbox_id="sbx_match")
        b = derive_fek_from_shared_secret(secret, sandbox_id="sbx_match")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_different_sandbox_different_fek(self) -> None:
        secret = secrets.token_bytes(32)
        a = derive_fek_from_shared_secret(secret, sandbox_id="sbx_a")
        b = derive_fek_from_shared_secret(secret, sandbox_id="sbx_b")
        self.assertNotEqual(a, b)

    def test_short_secret_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_fek_from_shared_secret(b"too-short", sandbox_id="sbx")


# =============================================================================
# Track 5F — Finding wrap/unwrap
# =============================================================================


class TestFindingEgress(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.fek = secrets.token_bytes(32)
        self.sandbox_id = "sbx_alice_bob"

        # Sender
        self.km_alice = SoftwareCloudBoxKM()
        self.skm = SessionKeyManager()
        self.key_id = self.skm.generate()
        self.pubkey_hex = self.skm.get_pubkey(self.key_id)

        # Recipient
        self.km_bob = SoftwareCloudBoxKM()

    def _build_signed_finding(self, *, match_id: str = "match_001") -> Finding:
        f = Finding(
            match_id=match_id,
            topology=FindingTopology.PEER,
            strength_per_dimension={"mood": 0.7, "theme": 0.3},
            emitter_palace_id="palace_alice",
            emitted_at_ms=1_000_000,
            session_pubkey_hex=self.pubkey_hex,
            provenance_hash_hex="deadbeef" * 8,
        )
        f.signature_hex = self.skm.sign(self.key_id, f.content_bytes())
        return f

    def test_wrap_unwrap_roundtrip(self) -> None:
        f = self._build_signed_finding()

        envelope = wrap_finding_for_egress(
            f, fek=self.fek, sandbox_id=self.sandbox_id,
            key_manager=self.km_alice,
        )
        self.assertNotEqual(envelope.ciphertext, b"")
        self.assertEqual(envelope.match_id, f.match_id)
        self.assertEqual(envelope.signature_hex, f.signature_hex)

        recovered = unwrap_finding(
            envelope, fek=self.fek, sandbox_id=self.sandbox_id,
            key_manager=self.km_bob,
        )
        self.assertEqual(recovered.match_id, f.match_id)
        self.assertEqual(recovered.topology, f.topology)
        self.assertEqual(
            recovered.strength_per_dimension, f.strength_per_dimension,
        )
        self.assertEqual(recovered.signature_hex, f.signature_hex)
        self.assertEqual(recovered.emitter_palace_id, f.emitter_palace_id)

    def test_unsigned_finding_rejected(self) -> None:
        f = self._build_signed_finding()
        f.signature_hex = ""  # un-sign

        with self.assertRaises(ValueError):
            wrap_finding_for_egress(
                f, fek=self.fek, sandbox_id=self.sandbox_id,
                key_manager=self.km_alice,
            )

    def test_envelope_sandbox_mismatch_rejected(self) -> None:
        """Envelope says one sandbox, caller asks to unwrap as another."""
        f = self._build_signed_finding()
        envelope = wrap_finding_for_egress(
            f, fek=self.fek, sandbox_id="sbx_actual",
            key_manager=self.km_alice,
        )
        with self.assertRaises(ValueError):
            unwrap_finding(
                envelope, fek=self.fek, sandbox_id="sbx_other",
                key_manager=self.km_bob,
            )

    def test_signature_preserved_through_envelope(self) -> None:
        """The signature is exposed on the envelope so recipients can
        verify before paying decryption cost. Round-trip should
        preserve it byte-exact."""
        f = self._build_signed_finding()
        envelope = wrap_finding_for_egress(
            f, fek=self.fek, sandbox_id=self.sandbox_id,
            key_manager=self.km_alice,
        )
        # Envelope exposes the signature without needing decryption
        self.assertEqual(envelope.signature_hex, f.signature_hex)
        self.assertEqual(envelope.session_pubkey_hex, f.session_pubkey_hex)
        # And the unwrapped Finding has the same signature
        recovered = unwrap_finding(
            envelope, fek=self.fek, sandbox_id=self.sandbox_id,
            key_manager=self.km_bob,
        )
        self.assertEqual(recovered.signature_hex, f.signature_hex)


# =============================================================================
# Track 5G — disk-at-rest encryption
# =============================================================================


class TestDarkDerivation(unittest.TestCase):
    def test_deterministic(self) -> None:
        master = secrets.token_bytes(32)
        a = derive_dark(master)
        b = derive_dark(master)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_different_master_different_dark(self) -> None:
        a = derive_dark(secrets.token_bytes(32))
        b = derive_dark(secrets.token_bytes(32))
        self.assertNotEqual(a, b)

    def test_short_master_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_dark(b"too-short")


class TestEncryptedLogBackend(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.master = secrets.token_bytes(32)
        self.dark = derive_dark(self.master)

    def _make_encrypted_client(self) -> tuple[LogClient, EncryptedLogBackend]:
        client = LogClient(backend=MockBackend())
        enc = wrap_log_with_dark(client, self.dark)
        return client, enc

    def test_roundtrip_through_wrapper(self) -> None:
        client, _ = self._make_encrypted_client()
        node_id = make_entity_id()

        evt = NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={"name": "sensitive_value"},
        )
        result = client.append(evt)
        self.assertTrue(result.accepted)

        # Read back through the same client — sees plaintext
        rows = list(client.read_range(0, client.current_offset() + 1))
        self.assertEqual(len(rows), 1)
        _o, kind, payload = rows[0]
        self.assertEqual(kind, "node_created")
        self.assertEqual(payload["node_id"], node_id)
        self.assertEqual(payload["properties"], {"name": "sensitive_value"})

    def test_cold_start_without_dark_sees_ciphertext(self) -> None:
        """Build a new client over the same backend but without DARK.
        Should see encrypted blobs, not plaintext."""
        client, enc = self._make_encrypted_client()
        node_id = make_entity_id()
        client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={"name": "secret"},
        ))

        # Cold-start: build a fresh client over the SAME inner
        # MockBackend, but no wrapper.
        cold_client = LogClient(backend=enc.inner)
        rows = list(cold_client.read_range(0, cold_client.current_offset() + 1))
        self.assertEqual(len(rows), 1)
        _o, _kind, payload = rows[0]

        # Payload should be the encrypted form
        self.assertTrue(payload.get("_at_rest_encrypted"))
        self.assertNotIn("node_id", payload)
        self.assertNotIn("properties", payload)

    def test_set_dark_none_falls_back_to_passthrough(self) -> None:
        client, enc = self._make_encrypted_client()
        # Drop the DARK
        enc.set_dark(None)
        self.assertFalse(enc.is_active())

        # Append now goes through unencrypted (and increments
        # passthrough counter)
        client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id="nde_x",
            node_kind="entity",
            properties={},
        ))
        self.assertGreater(enc.passthrough_count(), 0)

    def test_require_encryption_blocks_append_without_dark(self) -> None:
        client = LogClient(backend=MockBackend())
        enc = wrap_log_with_dark(client, dark=None, require_encryption=True)

        with self.assertRaises(RuntimeError):
            client.append(NodeCreated(
                event_id=make_event_id_log(),
                recorded_at=1000,
                actor="test",
                node_id="nde_x",
                node_kind="entity",
                properties={},
            ))

    def test_tombstoning_passthrough_re_encrypts(self) -> None:
        """Track 6D tombstoning routes through Track 5G; tombstoned
        payload should still be encrypted on disk."""
        client, enc = self._make_encrypted_client()
        node_id = make_entity_id()
        result = client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id=node_id,
            node_kind="entity",
            properties={"name": "before_erase"},
        ))
        offset = result.offset

        # Tombstone via the wrapper
        new_payload = {
            "node_id": node_id,
            "node_kind": "entity",
            "properties": {},
            "_erased": True,
            "_erased_for": node_id,
        }
        ok = client.rewrite_payload(offset, new_payload)
        self.assertTrue(ok)

        # Read back through the wrapper — should decrypt to the
        # tombstoned form
        rows = list(client.read_range(0, client.current_offset() + 1))
        _o, _kind, payload = rows[0]
        self.assertTrue(payload.get("_erased"))
        self.assertEqual(payload.get("properties"), {})

        # Cold-start: tombstoned entry is still encrypted at rest
        cold_client = LogClient(backend=enc.inner)
        cold_rows = list(cold_client.read_range(0, cold_client.current_offset() + 1))
        _o, _kind, cold_payload = cold_rows[0]
        self.assertTrue(cold_payload.get("_at_rest_encrypted"))
        self.assertNotIn("_erased", cold_payload)


# =============================================================================
# Track 5H — burn-recovery quarantine
# =============================================================================


class TestBurnRecoveryQuarantine(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()
        self.master = secrets.token_bytes(32)
        self.dark = derive_dark(self.master)

    def test_clean_log_no_unreadable(self) -> None:
        client = LogClient(backend=MockBackend())
        wrap_log_with_dark(client, self.dark)

        for i in range(3):
            client.append(NodeCreated(
                event_id=make_event_id_log(),
                recorded_at=1000 + i,
                actor="test",
                node_id=f"nde_{i}",
                node_kind="entity",
                properties={"i": i},
            ))

        report = scan_for_unreadable_events(client)
        self.assertEqual(report.total_scanned, 3)
        self.assertEqual(report.decrypted_ok, 3)
        self.assertEqual(report.unreadable_count, 0)
        self.assertFalse(has_burn_fallout(report))

    def test_post_burn_log_has_unreadable(self) -> None:
        """Write some encrypted entries, then simulate burn (DARK
        gone). Recovery scan should flag everything as unreadable."""
        client = LogClient(backend=MockBackend())
        enc = wrap_log_with_dark(client, self.dark)

        for i in range(3):
            client.append(NodeCreated(
                event_id=make_event_id_log(),
                recorded_at=1000 + i,
                actor="test",
                node_id=f"nde_{i}",
                node_kind="entity",
                properties={"i": i},
            ))

        # BURN: DARK destroyed
        enc.set_dark(None)

        report = scan_for_unreadable_events(client)
        self.assertEqual(report.total_scanned, 3)
        # Without DARK, the wrapper passes through encrypted blobs;
        # they show up as unreadable in the scan
        self.assertEqual(report.unreadable_count, 3)
        self.assertTrue(has_burn_fallout(report))

    def test_distinguishes_erased_from_unreadable(self) -> None:
        """Track 6D tombstones are deliberate; not burn fallout."""
        client = LogClient(backend=MockBackend())
        enc = wrap_log_with_dark(client, self.dark)

        # Append an event, then tombstone it (Track 6D shape)
        client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id="nde_to_erase",
            node_kind="entity",
            properties={},
        ))
        client.rewrite_payload(1, {
            "node_id": "nde_to_erase",
            "_erased": True,
            "_erased_for": "nde_to_erase",
        })

        # Append another that's NOT tombstoned
        client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1001,
            actor="test",
            node_id="nde_kept",
            node_kind="entity",
            properties={},
        ))

        report = scan_for_unreadable_events(client)
        # 2 events total: 1 erased tombstone, 1 readable
        self.assertEqual(report.total_scanned, 2)
        self.assertEqual(report.erased_tombstones, 1)
        self.assertEqual(report.decrypted_ok, 1)
        self.assertEqual(report.unreadable_count, 0)

    def test_legacy_pre_5g_events_count_as_decrypted_ok(self) -> None:
        """Plaintext events from before 5G shouldn't be flagged as
        unreadable."""
        client = LogClient(backend=MockBackend())
        # NO wrap — these go in plaintext
        client.append(NodeCreated(
            event_id=make_event_id_log(),
            recorded_at=1000,
            actor="test",
            node_id="nde_legacy",
            node_kind="entity",
            properties={"old": True},
        ))

        # Now wrap; but the existing entry stays plaintext
        wrap_log_with_dark(client, self.dark)

        report = scan_for_unreadable_events(client)
        self.assertEqual(report.total_scanned, 1)
        self.assertEqual(report.decrypted_ok, 1)
        self.assertEqual(report.unreadable_count, 0)


if __name__ == "__main__":
    unittest.main()
