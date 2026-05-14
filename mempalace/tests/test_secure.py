"""Tests for `mempalace.secure` — Track 5A + 5B.

Covers:
  - PhoneSecureElement contract (encrypt → decrypt roundtrip,
    handle binding, attestation sig, revocation).
  - CloudBoxKeyManager contract (bundle load, decrypt, idle-zero,
    expiry, state machine).
  - Phone ↔ CloudBox interop (encrypt on phone, decrypt on cloud
    box using the same handle + attestation_sig).
  - Cross-palace handle rejection.
"""

from __future__ import annotations

import time
import unittest

from mempalace.secure import (
    AttestationError,
    BundleVerificationError,
    EncryptResult,
    KeyHandleError,
    KeyManagerState,
    KeysNotLoaded,
    PhoneSecureElement,
    RevokedError,
    SessionKeyBundle,
    SoftwareCloudBoxKM,
    SoftwarePhoneSE,
)


# =============================================================================
# Phone SE — basic encrypt/decrypt
# =============================================================================


class TestPhoneSEBasic(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        se = SoftwarePhoneSE()
        self.assertIsInstance(se, PhoneSecureElement)

    def test_palace_id_stable(self) -> None:
        se1 = SoftwarePhoneSE(master_key=b"A" * 32)
        se2 = SoftwarePhoneSE(master_key=b"A" * 32)
        self.assertEqual(se1.palace_id(), se2.palace_id())

    def test_palace_id_differs_with_different_master_key(self) -> None:
        se1 = SoftwarePhoneSE(master_key=b"A" * 32)
        se2 = SoftwarePhoneSE(master_key=b"B" * 32)
        self.assertNotEqual(se1.palace_id(), se2.palace_id())

    def test_drawer_encrypt_decrypt_roundtrip(self) -> None:
        se = SoftwarePhoneSE()
        plaintext = b"hello drawer contents"
        result = se.encrypt_drawer(plaintext, drawer_id="drw_test_1")
        recovered = se.decrypt(
            result.ciphertext,
            dek_handle=result.dek_handle,
            attestation_sig=result.attestation_sig,
        )
        self.assertEqual(recovered, plaintext)

    def test_property_encrypt_decrypt_roundtrip(self) -> None:
        se = SoftwarePhoneSE()
        plaintext = b'{"some": "value"}'
        result = se.encrypt_property(
            plaintext,
            node_id="nde_x",
            field_name="bio",
        )
        recovered = se.decrypt(
            result.ciphertext,
            dek_handle=result.dek_handle,
            attestation_sig=result.attestation_sig,
        )
        self.assertEqual(recovered, plaintext)

    def test_ciphertext_differs_each_call(self) -> None:
        se = SoftwarePhoneSE()
        plaintext = b"same content"
        r1 = se.encrypt_drawer(plaintext, drawer_id="drw_a")
        r2 = se.encrypt_drawer(plaintext, drawer_id="drw_a")
        # Different nonces → different ciphertexts even for same plaintext+context
        self.assertNotEqual(r1.ciphertext, r2.ciphertext)

    def test_handle_bound_to_palace(self) -> None:
        """A handle from one SE can't be used by another SE."""
        se_a = SoftwarePhoneSE()
        se_b = SoftwarePhoneSE()
        result_a = se_a.encrypt_drawer(b"x", drawer_id="drw_x")
        with self.assertRaises(KeyHandleError):
            se_b.decrypt(
                result_a.ciphertext,
                dek_handle=result_a.dek_handle,
                attestation_sig=result_a.attestation_sig,
            )

    def test_attestation_sig_required(self) -> None:
        se = SoftwarePhoneSE()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_y")
        with self.assertRaises(AttestationError):
            se.decrypt(
                result.ciphertext,
                dek_handle=result.dek_handle,
                attestation_sig=b"\x00" * 32,
            )

    def test_ciphertext_tamper_detected(self) -> None:
        se = SoftwarePhoneSE()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_z")
        # Flip one byte in the ciphertext
        tampered = bytearray(result.ciphertext)
        tampered[20] ^= 0xFF
        with self.assertRaises(AttestationError):
            se.decrypt(
                bytes(tampered),
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )

    def test_handle_with_wrong_drawer_context_fails(self) -> None:
        """Encrypting with one drawer_id and decrypting with a handle
        for a different drawer_id should fail. We construct a forged
        handle to test this — in normal usage it can't happen because
        the SE produces handles internally."""
        se = SoftwarePhoneSE()
        r = se.encrypt_drawer(b"x", drawer_id="drw_real")
        # Forge a handle pointing at a different drawer_id
        forged = (
            f"seh1:{se.palace_id()}:drawer.verbatim:"
            f"{b'drw_other'.hex()}:drawer/drw_other"
        )
        with self.assertRaises(AttestationError):
            se.decrypt(
                r.ciphertext,
                dek_handle=forged,
                attestation_sig=r.attestation_sig,
            )


class TestPhoneSERevocation(unittest.TestCase):
    def test_revoke_makes_decrypt_raise(self) -> None:
        se = SoftwarePhoneSE()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_x")
        se.revoke_palace()
        with self.assertRaises(RevokedError):
            se.decrypt(
                result.ciphertext,
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )

    def test_revoke_makes_encrypt_raise(self) -> None:
        se = SoftwarePhoneSE()
        se.revoke_palace()
        with self.assertRaises(RevokedError):
            se.encrypt_drawer(b"x", drawer_id="drw_x")

    def test_revoke_idempotent(self) -> None:
        se = SoftwarePhoneSE()
        se.revoke_palace()
        se.revoke_palace()  # should not raise
        self.assertTrue(se.is_revoked())

    def test_revoke_blocks_bundle_release(self) -> None:
        se = SoftwarePhoneSE()
        se.revoke_palace()
        with self.assertRaises(RevokedError):
            se.release_session_bundle(daemon_attestation=b"daemon-hash")


# =============================================================================
# CloudBoxKeyManager — basic
# =============================================================================


DAEMON_ATTESTATION = b"daemon-binary-hash-abc123"


def _fresh_se_and_manager(
    *, ttl_seconds: int = 24 * 3600
) -> tuple[SoftwarePhoneSE, SoftwareCloudBoxKM]:
    se = SoftwarePhoneSE()
    manager = SoftwareCloudBoxKM()
    bundle = se.release_session_bundle(
        daemon_attestation=DAEMON_ATTESTATION,
        ttl_seconds=ttl_seconds,
    )
    manager.load_bundle(bundle, daemon_binary_attestation=DAEMON_ATTESTATION)
    return se, manager


class TestCloudBoxKMBasic(unittest.TestCase):
    def test_initial_state_locked(self) -> None:
        manager = SoftwareCloudBoxKM()
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_INITIAL)
        self.assertFalse(manager.is_loaded())

    def test_load_transitions_to_active(self) -> None:
        _, manager = _fresh_se_and_manager()
        self.assertEqual(manager.current_state(), KeyManagerState.LOADED_ACTIVE)
        self.assertTrue(manager.is_loaded())
        self.assertEqual(manager.bundle_generation(), 1)

    def test_decrypt_fails_when_locked(self) -> None:
        manager = SoftwareCloudBoxKM()
        with self.assertRaises(KeysNotLoaded):
            manager.decrypt(
                b"x" * 32,
                dek_handle="seh1:p:drawer.verbatim:00:l",
                attestation_sig=b"\x00" * 32,
            )


class TestPhoneCloudBoxInterop(unittest.TestCase):
    """The critical contract: a ciphertext encrypted by the phone must
    be decryptable by the cloud-box manager loaded with that phone's
    bundle."""

    def test_drawer_ciphertext_decrypts_on_cloud_box(self) -> None:
        se, manager = _fresh_se_and_manager()
        plaintext = b"drawer contents to encrypt"
        encrypt_result = se.encrypt_drawer(plaintext, drawer_id="drw_real")
        recovered = manager.decrypt(
            encrypt_result.ciphertext,
            dek_handle=encrypt_result.dek_handle,
            attestation_sig=encrypt_result.attestation_sig,
        )
        self.assertEqual(recovered, plaintext)

    def test_property_ciphertext_decrypts_on_cloud_box(self) -> None:
        se, manager = _fresh_se_and_manager()
        plaintext = b"some property value"
        encrypt_result = se.encrypt_property(
            plaintext,
            node_id="nde_y",
            field_name="bio",
        )
        recovered = manager.decrypt(
            encrypt_result.ciphertext,
            dek_handle=encrypt_result.dek_handle,
            attestation_sig=encrypt_result.attestation_sig,
        )
        self.assertEqual(recovered, plaintext)

    def test_cross_palace_handle_rejected_on_cloud_box(self) -> None:
        # Manager loaded for palace A
        _, manager = _fresh_se_and_manager()
        # Different palace's SE produces a handle
        se_other = SoftwarePhoneSE()
        result = se_other.encrypt_drawer(b"x", drawer_id="drw_x")
        with self.assertRaises(KeyHandleError):
            manager.decrypt(
                result.ciphertext,
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )

    def test_tampered_ciphertext_rejected_on_cloud_box(self) -> None:
        se, manager = _fresh_se_and_manager()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_x")
        tampered = bytearray(result.ciphertext)
        tampered[10] ^= 0xFF
        with self.assertRaises(AttestationError):
            manager.decrypt(
                bytes(tampered),
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )


# =============================================================================
# Bundle verification
# =============================================================================


class TestBundleVerification(unittest.TestCase):
    def test_bundle_with_wrong_daemon_attestation_rejected(self) -> None:
        se = SoftwarePhoneSE()
        bundle = se.release_session_bundle(
            daemon_attestation=b"daemon-A",
            ttl_seconds=3600,
        )
        manager = SoftwareCloudBoxKM()
        with self.assertRaises(BundleVerificationError):
            manager.load_bundle(
                bundle,
                daemon_binary_attestation=b"daemon-B",  # wrong
            )
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_FAILED)

    def test_expired_bundle_rejected(self) -> None:
        se = SoftwarePhoneSE()
        # ttl_seconds = -1 → already expired
        bundle = se.release_session_bundle(
            daemon_attestation=DAEMON_ATTESTATION,
            ttl_seconds=-1,
        )
        manager = SoftwareCloudBoxKM()
        with self.assertRaises(BundleVerificationError):
            manager.load_bundle(
                bundle,
                daemon_binary_attestation=DAEMON_ATTESTATION,
            )

    def test_tampered_bundle_signature_rejected(self) -> None:
        se = SoftwarePhoneSE()
        bundle = se.release_session_bundle(
            daemon_attestation=DAEMON_ATTESTATION,
            ttl_seconds=3600,
        )
        # Tamper with the signature
        bad = SessionKeyBundle(
            bundle_id=bundle.bundle_id,
            generation=bundle.generation,
            issued_at_ms=bundle.issued_at_ms,
            expires_at_ms=bundle.expires_at_ms,
            bundle_blob=bundle.bundle_blob,
            daemon_attestation=bundle.daemon_attestation,
            bundle_signature=b"\x00" * 32,  # forged
        )
        manager = SoftwareCloudBoxKM()
        with self.assertRaises(BundleVerificationError):
            manager.load_bundle(
                bad,
                daemon_binary_attestation=DAEMON_ATTESTATION,
            )

    def test_bundle_generation_increments(self) -> None:
        se = SoftwarePhoneSE()
        b1 = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
        b2 = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
        self.assertEqual(b1.generation, 1)
        self.assertEqual(b2.generation, 2)


# =============================================================================
# Idle-zero + state machine
# =============================================================================


class TestIdleZero(unittest.TestCase):
    def test_idle_zero_transitions_state(self) -> None:
        _, manager = _fresh_se_and_manager()
        self.assertEqual(manager.current_state(), KeyManagerState.LOADED_ACTIVE)
        manager.idle_zero()
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_ZEROED)
        self.assertFalse(manager.is_loaded())

    def test_decrypt_after_idle_zero_raises(self) -> None:
        se, manager = _fresh_se_and_manager()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_x")
        manager.idle_zero()
        with self.assertRaises(KeysNotLoaded):
            manager.decrypt(
                result.ciphertext,
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )

    def test_idle_zero_idempotent(self) -> None:
        _, manager = _fresh_se_and_manager()
        manager.idle_zero()
        manager.idle_zero()
        manager.idle_zero()
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_ZEROED)

    def test_reload_after_idle_zero_resumes(self) -> None:
        se, manager = _fresh_se_and_manager()
        result = se.encrypt_drawer(b"contents", drawer_id="drw_x")
        manager.idle_zero()
        # Phone issues a fresh bundle
        bundle = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
        manager.load_bundle(
            bundle, daemon_binary_attestation=DAEMON_ATTESTATION
        )
        recovered = manager.decrypt(
            result.ciphertext,
            dek_handle=result.dek_handle,
            attestation_sig=result.attestation_sig,
        )
        self.assertEqual(recovered, b"contents")
        # Generation increments — same SE, second bundle
        self.assertEqual(manager.bundle_generation(), 2)

    def test_record_activity_does_not_advance_when_locked(self) -> None:
        manager = SoftwareCloudBoxKM()
        manager.record_activity()  # should not raise
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_INITIAL)


class TestExpiry(unittest.TestCase):
    def test_decrypt_after_expiry_raises_and_zeros(self) -> None:
        # ttl=1 second, sleep past it
        se, manager = _fresh_se_and_manager(ttl_seconds=1)
        result = se.encrypt_drawer(b"contents", drawer_id="drw_x")
        # Pre-expiry, decrypt works
        recovered = manager.decrypt(
            result.ciphertext,
            dek_handle=result.dek_handle,
            attestation_sig=result.attestation_sig,
        )
        self.assertEqual(recovered, b"contents")

        time.sleep(1.1)
        with self.assertRaises(KeysNotLoaded):
            manager.decrypt(
                result.ciphertext,
                dek_handle=result.dek_handle,
                attestation_sig=result.attestation_sig,
            )
        self.assertEqual(manager.current_state(), KeyManagerState.LOCKED_ZEROED)


class TestEgressEncryption(unittest.TestCase):
    """Skeleton for Track 5F. Just verifies the surface exists and
    produces non-empty output."""

    def test_encrypt_for_egress_returns_bytes(self) -> None:
        _, manager = _fresh_se_and_manager()
        result = manager.encrypt_for_egress(
            b"finding payload",
            sandbox_id="sandbox_xyz",
            peer_pubkey=b"\x01" * 32,
        )
        self.assertIsInstance(result, bytes)
        self.assertNotEqual(result, b"finding payload")

    def test_encrypt_for_egress_fails_when_locked(self) -> None:
        manager = SoftwareCloudBoxKM()
        with self.assertRaises(KeysNotLoaded):
            manager.encrypt_for_egress(
                b"x",
                sandbox_id="s",
                peer_pubkey=b"k" * 32,
            )

    def test_egress_ciphertexts_differ_per_sandbox(self) -> None:
        _, manager = _fresh_se_and_manager()
        c1 = manager.encrypt_for_egress(
            b"same plaintext",
            sandbox_id="sandbox_A",
            peer_pubkey=b"\x01" * 32,
        )
        c2 = manager.encrypt_for_egress(
            b"same plaintext",
            sandbox_id="sandbox_B",
            peer_pubkey=b"\x01" * 32,
        )
        self.assertNotEqual(c1, c2)


if __name__ == "__main__":
    unittest.main()
