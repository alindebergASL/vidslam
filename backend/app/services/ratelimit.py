"""In-process token-bucket rate limiting for budget-burning endpoints.

The MVP runs a single backend process, so an in-memory bucket map is adequate.
The limiter surface is intentionally tiny — check(key) -> (allowed,
retry_after_seconds) — so a Redis-backed implementation can replace it for
multi-process deploys without touching the route dependencies.

Keying: requests are bucketed per client. A logged-in client is identified by a
hash of its session cookie; anonymous callers fall back to source IP. With the
MVP's single shared password every browser carries the same cookie value, so in
practice this is one bucket per deployment — still enough to stop a runaway
frontend loop or curl storm from burning provider budget.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..config import get_settings


@dataclass
class _Bucket:
    tokens: float
    last: float


class TokenBucketLimiter:
    def __init__(self, now=time.monotonic) -> None:
        # `now` is injectable so tests can drive the clock deterministically.
        self._now = now
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, capacity: int, refill_per_minute: float) -> tuple[bool, int]:
        """Try to take one token from `key`'s bucket.

        Returns (allowed, retry_after_seconds). retry_after is 0 when allowed,
        otherwise the whole seconds until at least one token will be available.
        """
        rate = refill_per_minute / 60.0
        now = self._now()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(capacity), last=now)
                self._buckets[key] = b
            b.tokens = min(float(capacity), b.tokens + (now - b.last) * rate)
            b.last = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0
            if rate <= 0:
                return False, 60
            need = 1.0 - b.tokens
            return False, max(1, int(need / rate + 0.999))

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_limiter: TokenBucketLimiter | None = None


def get_limiter() -> TokenBucketLimiter:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucketLimiter()
    return _limiter


def _client_key(request: Request) -> str:
    cookie = request.cookies.get("avs_session")
    if cookie:
        return hashlib.sha256(cookie.encode()).hexdigest()[:16]
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def rate_limit(bucket: str):
    """FastAPI dependency: one token per call from the client's `bucket`.

    Raises 429 with a Retry-After header when the bucket is empty. Disabled
    entirely when RATE_LIMIT_ENABLED=false (e.g. load tests).
    """

    def dep(request: Request) -> None:
        s = get_settings()
        if not s.rate_limit_enabled:
            return
        key = f"{bucket}:{_client_key(request)}"
        allowed, retry_after = get_limiter().check(
            key,
            capacity=s.rate_limit_generation_burst,
            refill_per_minute=s.rate_limit_generation_per_minute,
        )
        if not allowed:
            raise HTTPException(
                429,
                detail=(
                    f"Rate limit exceeded for {bucket} — try again in ~{retry_after}s. "
                    "These endpoints call paid providers, so they're throttled per client."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    return dep
