"""
libp2p transport for federation.

Per R3 §7.5: federation is a pull-based, peer-to-peer protocol over libp2p
with versioned protocol IDs. This module ships the protocol-ID constants
and a transport interface that production wires to a real libp2p node.

Protocol IDs (versioned for forward-compatibility):

  /mempalace/discovery/1.0.0      — peer discovery + capability advertisement
  /mempalace/manifest/1.0.0       — pull a peer's public manifest
  /mempalace/match-request/1.0.0  — request a match (sender → receiver)
  /mempalace/slice/1.0.0          — exchange encrypted slices for a match
  /mempalace/findings/1.0.0       — return structured findings
  /mempalace/heartbeat/1.0.0      — liveness + integrity-lockout
  /mempalace/evidence/1.0.0       — exchange substrate-verification evidence

Production deployment requires `py-libp2p` or equivalent. This file
provides the interface and a no-op transport for testing.

Spec ref: R3 §7.5.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol


# =============================================================================
# Protocol IDs
# =============================================================================


class ProtocolId(str, Enum):
    DISCOVERY = "/mempalace/discovery/1.0.0"
    MANIFEST = "/mempalace/manifest/1.0.0"
    MATCH_REQUEST = "/mempalace/match-request/1.0.0"
    SLICE = "/mempalace/slice/1.0.0"
    FINDINGS = "/mempalace/findings/1.0.0"
    HEARTBEAT = "/mempalace/heartbeat/1.0.0"
    EVIDENCE = "/mempalace/evidence/1.0.0"


# =============================================================================
# Transport interface
# =============================================================================


@dataclass
class PeerInfo:
    """Minimal peer descriptor."""

    peer_id: str               # libp2p peer ID (multihash)
    palace_pubkey_hex: str     # the palace's enrolled key
    addresses: tuple[str, ...] = ()
    last_seen_ms: int = 0


# Handler signature: (protocol_id, peer_info, request_bytes) -> response_bytes
ProtocolHandler = Callable[[ProtocolId, PeerInfo, bytes], Awaitable[bytes]]


class Transport(Protocol):
    """Pluggable transport interface."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def register_handler(self, protocol: ProtocolId, handler: ProtocolHandler) -> None: ...
    async def request(
        self,
        peer: PeerInfo,
        protocol: ProtocolId,
        request_bytes: bytes,
        *,
        timeout_s: float = 30.0,
    ) -> bytes: ...
    def known_peers(self) -> list[PeerInfo]: ...


# =============================================================================
# NoopTransport — for testing and dev
# =============================================================================


class NoopTransport:
    """In-process transport that loops requests back to local handlers.

    Good for testing the matching pipeline without any networking.
    """

    def __init__(self) -> None:
        self._handlers: dict[ProtocolId, ProtocolHandler] = {}
        self._peers: dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def register_handler(
        self, protocol: ProtocolId, handler: ProtocolHandler
    ) -> None:
        with self._lock:
            self._handlers[protocol] = handler

    async def request(
        self,
        peer: PeerInfo,
        protocol: ProtocolId,
        request_bytes: bytes,
        *,
        timeout_s: float = 30.0,
    ) -> bytes:
        with self._lock:
            handler = self._handlers.get(protocol)
        if handler is None:
            raise RuntimeError(f"no handler for {protocol.value}")
        return await handler(protocol, peer, request_bytes)

    def add_peer(self, peer: PeerInfo) -> None:
        with self._lock:
            self._peers[peer.peer_id] = peer

    def known_peers(self) -> list[PeerInfo]:
        with self._lock:
            return list(self._peers.values())


# =============================================================================
# Production transport (TODO)
# =============================================================================


class Libp2pTransport:
    """Real libp2p transport. Production-only.

    TODO: requires py-libp2p; depends on hardware-specific listen addresses,
    NAT traversal, mDNS for local discovery, Kademlia DHT for global.
    Skeleton only here.
    """

    def __init__(self, *, listen_addresses: tuple[str, ...] = ()) -> None:
        self._listen = listen_addresses
        self._handlers: dict[ProtocolId, ProtocolHandler] = {}

    async def start(self) -> None:
        raise NotImplementedError("Libp2pTransport requires py-libp2p; use NoopTransport in dev")

    async def stop(self) -> None:
        return

    def register_handler(self, protocol: ProtocolId, handler: ProtocolHandler) -> None:
        self._handlers[protocol] = handler

    async def request(
        self,
        peer: PeerInfo,
        protocol: ProtocolId,
        request_bytes: bytes,
        *,
        timeout_s: float = 30.0,
    ) -> bytes:
        raise NotImplementedError

    def known_peers(self) -> list[PeerInfo]:
        return []


# =============================================================================
# Module-level singleton
# =============================================================================


_TRANSPORT: Transport | None = None
_TRANSPORT_LOCK = threading.Lock()


def get_transport() -> Transport:
    """Return the active transport; defaults to NoopTransport."""
    global _TRANSPORT
    with _TRANSPORT_LOCK:
        if _TRANSPORT is None:
            _TRANSPORT = NoopTransport()
        return _TRANSPORT


def set_transport(transport: Transport) -> None:
    global _TRANSPORT
    with _TRANSPORT_LOCK:
        _TRANSPORT = transport


__all__ = [
    "Libp2pTransport",
    "NoopTransport",
    "PeerInfo",
    "ProtocolHandler",
    "ProtocolId",
    "Transport",
    "get_transport",
    "set_transport",
]
