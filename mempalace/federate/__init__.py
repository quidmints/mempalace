"""
mempalace.federate — federation layer.

Per R3 §7 / Part 9: cross-palace matching with three concentric privacy
boundaries (structural → derivation → substrate), session-keyed sandboxes,
hardware-attested model loading, signed findings, and rate-limited
peer-to-peer transport.

Submodules:

  attest          — cross-platform hardware attestation chain verification
  session_keys    — per-sandbox ephemeral keys with zeroize-on-destroy
  sandbox         — sandbox lifecycle (provision → load → run → teardown)
  transport       — libp2p protocol IDs + transport interface
  enclave_run     — sandbox-bound step execution
  rate_limit      — per-key token-bucket rate limiter
  layers/         — Layer 1/2/3 matching steps
  manifest        — public manifest publishing + foreign-manifest cache
  slice           — encrypted slice extraction per match-request layer
  findings        — structured findings emission to switchboard/oracle
  kg_sketch       — MinHash sketch over typed walks (Layer 1 input)
  manifold_index  — velocity-field summary (Layer 1 input)
  watermark       — embedding watermarks for substrate-leak detection
  foyer           — first-encounter rendered surface for peers

Spec ref: R3 §7, Part 9.
"""

from .attest import (
    AndroidVerifier,
    AttestationChain,
    ChainVerificationResult,
    DevVerifier,
    IOSVerifier,
    LinuxTPMVerifier,
    Platform,
    PlatformVerifier,
    verify_attestation_chain,
)
from .enclave_run import EnclaveRunResult, run_step_in_sandbox
from .findings import (
    Finding,
    FindingTopology,
    build_finding,
    classify_topology,
    emit_finding,
)
from .foyer import (
    FoyerNowItem,
    FoyerRecentPromotion,
    FoyerSurface,
    FoyerThemeCard,
    render_foyer,
)
from .kg_sketch import (
    DEFAULT_EDGE_WEIGHT,
    EDGE_KIND_WEIGHTS,
    KGSketch,
    build_kg_sketch,
    schema_fingerprint,
    sketch_jaccard,
)
from .layers import (
    AssertionOverlap,
    CCGraphSketch,
    DerivationGraphSimilarity,
    DerivationLayer,
    ParalinguisticSimilarity,
    RPathOverlap,
    SemanticSimilarity,
    StructuralLayerOutputs,
    StructuralMatchingLayer,
    SubstrateLayer,
    VerbatimSimilarity,
)
from .manifest import (
    DEFAULT_MANIFEST_TTL_MS,
    ManifestStore,
    PublicManifest,
    ThemeEntry,
    build_manifest,
    get_manifest_store,
    set_manifest_store,
)
from .manifold_index import (
    DEFAULT_HALF_LIFE_DAYS,
    PathWeightEMA,
    TUNNEL_MIN_GAIN,
    Tunnel,
    VelocityFieldSummary,
    build_velocity_field_summary,
    discover_tunnels,
    region_uncertainty,
)
from .rate_limit import RateLimiter, TokenBucket, get_rate_limiter
from .sandbox import (
    SandboxManager,
    SandboxState,
    SandboxStatus,
    get_sandbox_manager,
    set_sandbox_manager,
)
from .session_keys import SessionKey, SessionKeyManager, get_session_key_manager
from .slice import (
    DerivationSlicePayload,
    EncryptedSlice,
    SliceLayer,
    SliceScope,
    StructuralSlicePayload,
    SubstrateDrawerEntry,
    SubstrateSlicePayload,
    build_encrypted_slice,
    extract_substrate_slice,
)
from .transport import (
    Libp2pTransport,
    NoopTransport,
    PeerInfo,
    ProtocolHandler,
    ProtocolId,
    Transport,
    get_transport,
    set_transport,
)
from .watermark import (
    DetectionResult,
    WatermarkRegistry,
    WatermarkSeed,
    get_watermark_registry,
    set_watermark_registry,
)

__all__ = [
    # attest
    "AndroidVerifier",
    "AttestationChain",
    "ChainVerificationResult",
    "DevVerifier",
    "IOSVerifier",
    "LinuxTPMVerifier",
    "Platform",
    "PlatformVerifier",
    "verify_attestation_chain",
    # enclave_run
    "EnclaveRunResult",
    "run_step_in_sandbox",
    # findings
    "Finding",
    "FindingTopology",
    "build_finding",
    "classify_topology",
    "emit_finding",
    # foyer
    "FoyerNowItem",
    "FoyerRecentPromotion",
    "FoyerSurface",
    "FoyerThemeCard",
    "render_foyer",
    # kg_sketch
    "DEFAULT_EDGE_WEIGHT",
    "EDGE_KIND_WEIGHTS",
    "KGSketch",
    "build_kg_sketch",
    "schema_fingerprint",
    "sketch_jaccard",
    # layers
    "AssertionOverlap",
    "CCGraphSketch",
    "DerivationGraphSimilarity",
    "DerivationLayer",
    "ParalinguisticSimilarity",
    "RPathOverlap",
    "SemanticSimilarity",
    "StructuralLayerOutputs",
    "StructuralMatchingLayer",
    "SubstrateLayer",
    "VerbatimSimilarity",
    # manifest
    "DEFAULT_MANIFEST_TTL_MS",
    "ManifestStore",
    "PublicManifest",
    "ThemeEntry",
    "build_manifest",
    "get_manifest_store",
    "set_manifest_store",
    # manifold_index
    "DEFAULT_HALF_LIFE_DAYS",
    "PathWeightEMA",
    "TUNNEL_MIN_GAIN",
    "Tunnel",
    "VelocityFieldSummary",
    "build_velocity_field_summary",
    "discover_tunnels",
    "region_uncertainty",
    # rate_limit
    "RateLimiter",
    "TokenBucket",
    "get_rate_limiter",
    # sandbox
    "SandboxManager",
    "SandboxState",
    "SandboxStatus",
    "get_sandbox_manager",
    "set_sandbox_manager",
    # session_keys
    "SessionKey",
    "SessionKeyManager",
    "get_session_key_manager",
    # slice
    "DerivationSlicePayload",
    "EncryptedSlice",
    "SliceLayer",
    "SliceScope",
    "StructuralSlicePayload",
    "SubstrateDrawerEntry",
    "SubstrateSlicePayload",
    "build_encrypted_slice",
    "extract_substrate_slice",
    # transport
    "Libp2pTransport",
    "NoopTransport",
    "PeerInfo",
    "ProtocolHandler",
    "ProtocolId",
    "Transport",
    "get_transport",
    "set_transport",
    # watermark
    "DetectionResult",
    "WatermarkRegistry",
    "WatermarkSeed",
    "get_watermark_registry",
    "set_watermark_registry",
]
