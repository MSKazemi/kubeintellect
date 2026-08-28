"""Per-caller API rate limiting (enterprise A16).

Until 2026-08-28 there was no limiter on any route. A single caller — a retry loop in a script,
a misconfigured CI job, a stolen readonly key — could issue unbounded `POST /v1/chat/completions`
requests, and each one is an LLM call against the operator's own spend and a `kubectl` fan-out
against their API server. The spend guard in `autonomy/budget.py` bounds what the *watchtower*
does autonomously; nothing bounded what a client asks for.

Design, and the four decisions worth stating:

* **Keyed by identity, not by IP.** Behind an Ingress every request arrives from one address, so
  an IP-keyed limiter would throttle the whole tenancy the moment one client misbehaved. The key
  is a SHA-256 of the bearer token — the raw key is never stored, never logged, and never leaves
  the request. IP is the fallback only when auth is disabled (local development), where there is
  no identity to key on.
* **Probe paths are exempt, and that is a safety property, not a convenience.** `/healthz` and
  `/readyz` are read by the kubelet. A limiter that can answer 429 to a liveness probe is a
  limiter that can restart the pod under exactly the load it exists to survive — strictly worse
  than no limiter. `/metrics` is exempt for the same reason: a throttled scrape target silently
  becomes an unmonitored one.
* **It sheds load inside CORS, not outside it.** Mounted so that `CORSMiddleware` wraps it, a 429
  still carries `Access-Control-Allow-Origin`; mounted outside, a browser client would see an
  opaque network error rather than the status that explains it. `RequestLoggingMiddleware` wraps
  both, so a rejection appears in the access log — an invisible limiter cannot be operated.
* **The bucket table is bounded.** The middleware runs before route authentication, so an
  unauthenticated caller can present arbitrary bearer tokens; an unbounded dict keyed on them is
  a memory-exhaustion vector dressed as a defence. Capped at ``RATE_LIMIT_MAX_TRACKED`` with
  least-recently-seen eviction.

⚠️ **Two limits an operator must know, because neither is fixed by tuning the numbers.**

1. *The counters are per replica.* This process holds its own buckets, so N replicas behind one
   Service admit up to N × ``RATE_LIMIT_PER_MIN`` per caller. The limit is a per-replica fair-use
   bound, not a fleet-wide quota; a fleet-wide one needs shared state (Redis, or Postgres at one
   round trip per request), which is a deliberate trade this build has not made.
2. *This is fair use, not DDoS defence.* Eviction under a token-rotating flood can drop a
   legitimate caller's bucket, which costs them their accumulated burst, not their access.
   Volumetric abuse belongs at the ingress, which can drop before anything reaches Python.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Never rate-limited. Probe paths, because a 429 to the kubelet restarts the pod under load;
#: the metrics path, because a throttled scrape target becomes an unmonitored one.
EXEMPT_PATHS = frozenset({
    "/healthz", "/readyz", "/metrics",
    "/v1/healthz", "/v1/readyz",
})


@dataclass
class _Bucket:
    """A token bucket: ``tokens`` refills at ``per_min / 60`` per second, capped at the burst."""

    tokens: float
    updated: float


def caller_key(request: Request) -> str:
    """Identify the caller without retaining its credential.

    The bearer token is hashed, so the bucket table cannot leak a working API key if it is ever
    dumped in a heap snapshot or a debug log. Falls back to the peer address only when there is
    no token to key on at all.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ").strip()
        if token:
            return "k:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    client = request.client
    return "ip:" + (client.host if client else "unknown")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket limiter, one bucket per caller, bounded table, 429 with ``Retry-After``."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    # ── the decision, separated from the HTTP plumbing so it is directly testable ──
    def allow(self, key: str, now: float) -> tuple[bool, float]:
        """Spend one token for ``key``. Returns ``(allowed, retry_after_seconds)``."""
        capacity = float(max(settings.RATE_LIMIT_BURST, 1))
        refill_per_s = max(settings.RATE_LIMIT_PER_MIN, 1) / 60.0

        bucket = self._buckets.get(key)
        if bucket is None:
            self._evict_if_full()
            bucket = _Bucket(tokens=capacity, updated=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(now - bucket.updated, 0.0)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_s)
            bucket.updated = now
        self._buckets.move_to_end(key)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        # Seconds until one whole token exists again — the honest Retry-After, so a client
        # backs off by the amount that will actually succeed instead of hot-looping.
        return False, max((1.0 - bucket.tokens) / refill_per_s, 1.0)

    def _evict_if_full(self) -> None:
        """Drop least-recently-seen buckets so the table cannot grow without bound."""
        limit = max(settings.RATE_LIMIT_MAX_TRACKED, 1)
        while len(self._buckets) >= limit:
            evicted, _ = self._buckets.popitem(last=False)
            logger.warning(
                "rate_limit: bucket table full at %d — evicted the least-recently-seen caller "
                "(%s…). Volumetric abuse belongs at the ingress, not here.", limit, evicted[:10],
            )

    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.RATE_LIMIT_ENABLED or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        allowed, retry_after = self.allow(caller_key(request), time.monotonic())
        if allowed:
            return await call_next(request)

        logger.warning(
            f"rate_limit: 429 {request.method} {request.url.path} — "
            f"over {settings.RATE_LIMIT_PER_MIN}/min (burst {settings.RATE_LIMIT_BURST})"
        )
        return JSONResponse(
            status_code=429,
            content={"detail": (
                f"Rate limit exceeded: {settings.RATE_LIMIT_PER_MIN} requests/minute per API key "
                f"(burst {settings.RATE_LIMIT_BURST}). Retry after {int(retry_after)}s."
            )},
            headers={
                "Retry-After": str(int(retry_after)),
                "X-RateLimit-Limit": str(settings.RATE_LIMIT_PER_MIN),
                "X-RateLimit-Remaining": "0",
            },
        )
