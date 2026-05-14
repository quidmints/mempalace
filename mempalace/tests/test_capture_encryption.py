"""Tests for Track 5C-E: encrypted capture + read path.

Covers:
  - Capture without secure_element produces v0 events (legacy path).
  - Capture with secure_element produces v2 events with ciphertext.
  - Captured ciphertext decrypts cleanly through the cloud-box manager.
  - Read helpers handle legacy + encrypted events correctly.
  - Tampered events raise AttestationError on read.
"""

from __future__ import annotations

import unittest

from mempalace.drawer.capture import capture_drawer
from mempalace.drawer.secure_read import (
    decrypt_verbatim,
    decrypt_verbatim_str,
    is_encrypted,
)
from mempalace.embed.client import EmbeddingStore, InMemoryBackend
from mempalace.embed.model import EmbeddingService
from mempalace.log.client import LogClient
from mempalace.schema.events import DrawerCaptured
from mempalace.schema.kinds import InteractionalKind
from mempalace.secure import (
    AttestationError,
    KeysNotLoaded,
    SoftwareCloudBoxKM,
    SoftwarePhoneSE,
)
from mempalace.tests.conftest import reset_module_state


DAEMON_ATTESTATION = b"daemon-binary-hash-test"


def _fresh_pair():
    se = SoftwarePhoneSE()
    manager = SoftwareCloudBoxKM()
    bundle = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
    manager.load_bundle(bundle, daemon_binary_attestation=DAEMON_ATTESTATION)
    return se, manager


def _capture_kwargs(transcript="hello world"):
    """Build a minimal capture call."""
    return dict(
        transcript=transcript,
        actor="test",
        duration_ms=1000,
        log_client=LogClient(),
        embedding_service=EmbeddingService(),
        embedding_store=EmbeddingStore(backend=InMemoryBackend()),
        interactional=InteractionalKind.MEMO_TO_SELF,
    )


def _drawer_captured_events(log: LogClient) -> list[DrawerCaptured]:
    """Pull DrawerCaptured events back from the log, reconstructed
    as dataclasses."""
    end = log.current_offset() + 1
    raw = log.read_range(0, end)
    events: list[DrawerCaptured] = []
    for _offset, kind, payload in raw:
        if kind != DrawerCaptured.EVENT_KIND:
            continue
        # Filter payload to fields the dataclass knows about
        # so we don't choke on log-internal extras.
        import dataclasses as _dc
        valid_fields = {f.name for f in _dc.fields(DrawerCaptured)}
        cleaned = {k: v for k, v in payload.items() if k in valid_fields}
        # Bytes fields can come back as base64-encoded strings or bytes
        # depending on serialization path. The MockBackend stores dicts
        # as-is, so bytes stay bytes.
        events.append(DrawerCaptured(**cleaned))
    return events


# =============================================================================
# Legacy path — no encryption
# =============================================================================


class TestCaptureLegacyPath(unittest.TestCase):
    """capture_drawer without secure_element preserves the existing
    behavior (no ciphertext fields populated)."""

    def setUp(self) -> None:
        reset_module_state()

    def test_event_has_v0_schema(self) -> None:
        log = LogClient()
        result = capture_drawer(**{**_capture_kwargs(), "log_client": log})
        # Find the captured event in the log
        events = _drawer_captured_events(log)
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.encryption_schema_version, "v0")
        self.assertEqual(evt.verbatim_ciphertext, b"")
        self.assertEqual(evt.verbatim_dek_handle, "")
        self.assertEqual(evt.verbatim_attestation_sig, b"")
        self.assertEqual(evt.session_bundle_generation, 0)

    def test_is_encrypted_false_for_legacy(self) -> None:
        log = LogClient()
        capture_drawer(**{**_capture_kwargs(), "log_client": log})
        evt = _drawer_captured_events(log)[0]
        self.assertFalse(is_encrypted(evt))

    def test_decrypt_verbatim_returns_none_for_legacy(self) -> None:
        """Legacy events return None — caller has plaintext through
        another channel."""
        log = LogClient()
        capture_drawer(**{**_capture_kwargs(), "log_client": log})
        evt = _drawer_captured_events(log)[0]
        _, manager = _fresh_pair()
        self.assertIsNone(decrypt_verbatim(evt, manager))
        self.assertIsNone(decrypt_verbatim_str(evt, manager))


# =============================================================================
# Encrypted path — with secure_element
# =============================================================================


class TestCaptureEncryptedPath(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_event_has_v2_schema(self) -> None:
        se, _ = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs("secret transcript"), "log_client": log},
            secure_element=se,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertEqual(evt.encryption_schema_version, "v2")
        self.assertNotEqual(evt.verbatim_ciphertext, b"")
        self.assertNotEqual(evt.verbatim_dek_handle, "")
        self.assertNotEqual(evt.verbatim_attestation_sig, b"")

    def test_is_encrypted_true(self) -> None:
        se, _ = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs(), "log_client": log},
            secure_element=se,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertTrue(is_encrypted(evt))

    def test_ciphertext_does_not_contain_plaintext(self) -> None:
        se, _ = _fresh_pair()
        log = LogClient()
        secret = "this is the secret payload that should not leak"
        capture_drawer(
            **{**_capture_kwargs(secret), "log_client": log},
            secure_element=se,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertNotIn(secret.encode("utf-8"), evt.verbatim_ciphertext)

    def test_session_bundle_generation_recorded(self) -> None:
        """When a manager is provided, its generation is on the event."""
        se, manager = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs(), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertEqual(evt.session_bundle_generation, 1)

    def test_session_bundle_generation_zero_when_no_manager(self) -> None:
        """Encryption without a manager: generation defaults to 0."""
        se, _ = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs(), "log_client": log},
            secure_element=se,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertEqual(evt.session_bundle_generation, 0)


# =============================================================================
# Read path — decrypt round-trips
# =============================================================================


class TestReadPathRoundtrip(unittest.TestCase):
    def setUp(self) -> None:
        reset_module_state()

    def test_decrypt_verbatim_recovers_plaintext(self) -> None:
        se, manager = _fresh_pair()
        log = LogClient()
        secret = "this is the verbatim transcript"
        capture_drawer(
            **{**_capture_kwargs(secret), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]
        recovered = decrypt_verbatim(evt, manager)
        self.assertEqual(recovered, secret.encode("utf-8"))

    def test_decrypt_verbatim_str_returns_decoded(self) -> None:
        se, manager = _fresh_pair()
        log = LogClient()
        secret = "verbatim with special chars: ñ é 漢字 🦊"
        capture_drawer(
            **{**_capture_kwargs(secret), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]
        self.assertEqual(decrypt_verbatim_str(evt, manager), secret)

    def test_decrypt_fails_when_manager_locked(self) -> None:
        se, manager = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs(), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]
        manager.idle_zero()
        with self.assertRaises(KeysNotLoaded):
            decrypt_verbatim(evt, manager)

    def test_tampered_event_rejected_at_read(self) -> None:
        se, manager = _fresh_pair()
        log = LogClient()
        capture_drawer(
            **{**_capture_kwargs(), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]
        # Tamper with the on-disk ciphertext (simulate operator
        # disk modification)
        tampered = bytearray(evt.verbatim_ciphertext)
        tampered[15] ^= 0xFF
        evt.verbatim_ciphertext = bytes(tampered)
        with self.assertRaises(AttestationError):
            decrypt_verbatim(evt, manager)

    def test_phone_only_decrypt_path_also_works(self) -> None:
        """The phone (SE) can decrypt directly without the cloud box —
        used for in-app drawer view per
        USER_VIEW_AND_DELETE_DESIGN.md §"Drawer view (on-demand,
        phone-side decryption)"."""
        se, _ = _fresh_pair()
        log = LogClient()
        secret = "viewable on phone only"
        capture_drawer(
            **{**_capture_kwargs(secret), "log_client": log},
            secure_element=se,
        )
        evt = _drawer_captured_events(log)[0]
        # Phone decrypts directly using its SE — no manager involved
        recovered = se.decrypt(
            evt.verbatim_ciphertext,
            dek_handle=evt.verbatim_dek_handle,
            attestation_sig=evt.verbatim_attestation_sig,
        )
        self.assertEqual(recovered, secret.encode("utf-8"))


class TestRoundtripWithBundleLifecycle(unittest.TestCase):
    """Encrypt → idle-zero → reload → decrypt should still work."""

    def setUp(self) -> None:
        reset_module_state()

    def test_decrypt_after_bundle_reload(self) -> None:
        se, manager = _fresh_pair()
        log = LogClient()
        secret = "this should survive bundle reload"
        capture_drawer(
            **{**_capture_kwargs(secret), "log_client": log},
            secure_element=se,
            cloud_box_key_manager=manager,
        )
        evt = _drawer_captured_events(log)[0]

        # Idle-zero
        manager.idle_zero()

        # Phone issues a fresh bundle, daemon reloads
        bundle2 = se.release_session_bundle(daemon_attestation=DAEMON_ATTESTATION)
        manager.load_bundle(bundle2, daemon_binary_attestation=DAEMON_ATTESTATION)

        # Decrypt under new bundle (same master key, different generation)
        recovered = decrypt_verbatim_str(evt, manager)
        self.assertEqual(recovered, secret)


if __name__ == "__main__":
    unittest.main()
