"""
Secure-element + cloud-box key management.

Implements Track 5A (PhoneSecureElement) and Track 5B
(CloudBoxKeyManager) per ENCRYPTION_AT_EDGE_DESIGN.md (v2).

# Two distinct primitives

  - `PhoneSecureElement` runs on the user's phone. Hardware-isolated
    in production (iOS Secure Enclave, Android StrongBox); raw key
    bytes never leave it. The `SoftwarePhoneSE` impl is for tests
    and dev only and prints a stark warning when used.

  - `CloudBoxKeyManager` runs on the user's cloud box. Software-only
    by design (R3 §10 rejected SGX/TEE). Receives TTL'd session-key
    bundles from the phone via challenge-response; idle-zeros after
    inactivity; no hardware isolation.

# Why both, not one

The v1 ENCRYPTION_AT_EDGE_DESIGN.md treated these as one
"SecureElement" Protocol. v2 split them because they have different
security primitives and conflating them led to overpromising about
what the cloud box can guarantee. See ENCRYPTION_AT_EDGE_DESIGN.md
v2 §"The threat model — actual" for the full story.

# Importing

    from mempalace.secure import (
        # phone-side
        PhoneSecureElement, SoftwarePhoneSE,
        # cloud-box-side
        CloudBoxKeyManager, SoftwareCloudBoxKM, KeyManagerState,
        # shared types
        EncryptResult, SessionKeyBundle,
        # errors
        AttestationError, KeyHandleError, RevokedError,
        BundleVerificationError, KeysNotLoaded,
    )
"""

from mempalace.secure.element import (
    AttestationError,
    EncryptResult,
    KeyHandleError,
    PhoneSecureElement,
    RevokedError,
    SessionKeyBundle,
    SoftwarePhoneSE,
)
from mempalace.secure.key_manager import (
    BundleVerificationError,
    CloudBoxKeyManager,
    KeyManagerState,
    KeysNotLoaded,
    SoftwareCloudBoxKM,
)

__all__ = [
    "AttestationError",
    "BundleVerificationError",
    "CloudBoxKeyManager",
    "EncryptResult",
    "KeyHandleError",
    "KeyManagerState",
    "KeysNotLoaded",
    "PhoneSecureElement",
    "RevokedError",
    "SessionKeyBundle",
    "SoftwareCloudBoxKM",
    "SoftwarePhoneSE",
]
