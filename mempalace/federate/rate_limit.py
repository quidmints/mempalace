"""
Match-request rate limiting.

Per R3 §3.3: a malicious peer could exhaust our compute by spraying
match requests. Defense:

  - Token bucket per enrolled palace key (default: 10 requests / hour)
  - Idempotency cache: requests with same (peer, request_id) within
    the cache TTL are returned the same response without re-running

Idempotency uses match_cache view (Part 5.x) for the actual cache;
this module owns the rate-limiter side.

Spec ref: R3 §3.3.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Per-key token bucket."""

    capacity: int
    fill_rate_per_s: float
    tokens: float
    last_refill_ms: int


class RateLimiter:
    """Token-bucket rate limiter keyed by (palace_pubkey, route).

    Default policy: 10 requests / hour for /match-request,
    100 requests / hour for /manifest, no limit for /heartbeat.
    """

    DEFAULT_POLICIES: dict[str, tuple[int, float]] = {
        "match_request": (10, 10.0 / 3600.0),
        "manifest": (100, 100.0 / 3600.0),
        "evidence": (50, 50.0 / 3600.0),
        "heartbeat": (10_000, 100.0),  # effectively unlimited
    }

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()
        self._idempotency: dict[tuple[str, str], tuple[int, bytes]] = {}
        self._idempotency_ttl_ms = 5 * 60 * 1000  # 5 minutes

    # ---- rate limiting -----------------------------------------------------

    def check(self, peer_pubkey: str, route: str) -> bool:
        """Return True if a token was available and consumed."""
        if route not in self.DEFAULT_POLICIES:
            return True  # unknown route — allow by default
        capacity, fill_rate = self.DEFAULT_POLICIES[route]
        now_ms = int(time.time() * 1000)

        with self._lock:
            key = (peer_pubkey, route)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=capacity,
                    fill_rate_per_s=fill_rate,
                    tokens=float(capacity),
                    last_refill_ms=now_ms,
                )
                self._buckets[key] = bucket
            else:
                # Refill
                elapsed_s = (now_ms - bucket.last_refill_ms) / 1000.0
                bucket.tokens = min(
                    bucket.capacity,
                    bucket.tokens + elapsed_s * bucket.fill_rate_per_s,
                )
                bucket.last_refill_ms = now_ms

            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True

    # ---- idempotency cache --------------------------------------------------

    def idempotency_lookup(
        self, peer_pubkey: str, request_id: str
    ) -> bytes | None:
        """Return a cached response if (peer, request_id) was seen recently."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            entry = self._idempotency.get((peer_pubkey, request_id))
            if entry is None:
                return None
            stored_at, response = entry
            if (now_ms - stored_at) > self._idempotency_ttl_ms:
                self._idempotency.pop((peer_pubkey, request_id), None)
                return None
            return response

    def idempotency_store(
        self, peer_pubkey: str, request_id: str, response: bytes
    ) -> None:
        with self._lock:
            self._idempotency[(peer_pubkey, request_id)] = (
                int(time.time() * 1000),
                response,
            )

    def reap_expired(self) -> int:
        now_ms = int(time.time() * 1000)
        count = 0
        with self._lock:
            for key in list(self._idempotency.keys()):
                stored_at, _ = self._idempotency[key]
                if (now_ms - stored_at) > self._idempotency_ttl_ms:
                    self._idempotency.pop(key)
                    count += 1
        return count


# =============================================================================
# Module-level singleton
# =============================================================================


_LIMITER = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _LIMITER


__all__ = ["RateLimiter", "TokenBucket", "get_rate_limiter"]
