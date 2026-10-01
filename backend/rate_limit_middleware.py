"""Global rate limiting middleware for Oracle API.

Uses Redis for distributed rate limiting across replicas.
Falls back to in-memory limiting when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import uuid
import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("oracle.rate_limit")

# Signed provider callbacks (Telnyx, Plivo, Stripe). They arrive from a few
# shared provider egress addresses, so the anonymous per-IP allowance (100/min)
# refused every delivery past the hundredth in a minute — and a refused webhook
# is RETRIED by the provider, so throttling multiplied the load (Mission 8
# burst test: 95% of 2,850 signed deliveries at 60/s got 429). Each handler
# verifies its signature before doing any work; this ceiling only bounds a
# single source hammering the verifier.
WEBHOOK_PATH_PREFIXES = ("/api/messaging/webhooks/", "/api/telephony/webhooks/", "/billing/webhook")

# Rate limit configuration (requests per minute per network identity).  The
# general API limit is intentionally conservative for anonymous callers.  A
# signed-in CRM session receives its own tenant/user bucket below so a normal
# workspace boot (and React's parallel data loaders) cannot exhaust a shared
# office-NAT allowance.
RATE_LIMITS = {
    # Per source IP. A brokerage signs in from ONE office address: at 10/min,
    # 15 of 25 agents arriving together were locked out (Mission 8). Guessing
    # a single account is bounded separately, per account, in auth.py.
    "/auth/login": int(os.getenv("ORACLE_LOGIN_IP_RATE_LIMIT", "60")),
    "/auth/register": 5,
    "/auth/forgot": 3,
    "/auth/reset": 3,
    "/auth/": 120,
    "/api/ai/chat": 20,
    "/api/public/lead-intake/": 30,
    **{prefix: int(os.getenv("ORACLE_WEBHOOK_RATE_LIMIT", "6000")) for prefix in WEBHOOK_PATH_PREFIXES},
    "/api/crm/tour": 5,
    # AI tour generation. Previously enforced by a module-level list in
    # server.py, which made the ceiling process-global: every tenant on a
    # replica shared it, and each replica enforced its own copy, so N replicas
    # allowed N× the intended rate while one busy tenant could consume the whole
    # allowance for everyone. Here it is per-principal and cross-replica.
    "/api/generate-tour": int(os.getenv("ORACLE_TOUR_RATE_LIMIT", "10")),
    "/api/": 100,  # Default for all other API endpoints
}

def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read an int env var at import time without letting a blank or malformed
    value take down the whole API — an unset-but-present ``FOO=`` line copied
    from .env.prod.example would otherwise raise during `import server`."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — falling back to %d", name, raw, default
        )
        return max(minimum, default)


AUTHENTICATED_API_RATE_LIMIT = _env_int(
    "ORACLE_AUTHENTICATED_API_RATE_LIMIT", 600, minimum=100
)

# CORS preflights get their own generous per-IP bucket rather than being charged
# against the real request's quota. They still get a ceiling: an unmetered method
# would be the cheapest way to walk the whole middleware chain for free.
PREFLIGHT_RATE_LIMIT = _env_int(
    "ORACLE_PREFLIGHT_RATE_LIMIT", 600, minimum=60
)

WINDOW_SECONDS = 60

# In-memory fallback (per-process, not distributed)
_ip_requests: dict[str, list[float]] = defaultdict(list)
_endpoint_requests: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
_memory_lock = asyncio.Lock()

# Redis client (optional, for distributed rate limiting)
_redis_client = None


_DEV_VALUES = {"dev", "development", "local"}
_MANAGED_INGRESS_ENV_VARS = (
    "CONTAINER_APP_NAME",
    "CONTAINER_APP_ENV_DNS_SUFFIX",
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_EXECUTION_ENV",
)
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _trust_proxy_headers() -> bool:
    configured = os.getenv("ORACLE_TRUST_PROXY_HEADERS")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    if os.getenv("ORACLE_ENV", "").strip().lower() not in _DEV_VALUES:
        return True
    return any(os.getenv(name) for name in _MANAGED_INGRESS_ENV_VARS)


def _is_internal_ip(candidate: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(candidate in network for network in _INTERNAL_NETWORKS)


async def _init_redis():
    global _redis_client
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        logger.info("Rate limiter using PostgreSQL distributed windows")
        _redis_client = None
        return
    try:
        import redis.asyncio as aioredis
        # Short timeouts: every API request consults this client. With none, a
        # Valkey that stopped answering held each request until the OS gave up
        # — throughput fell from ~240 to 2 requests per 20 s (Mission 8).
        _redis_client = aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=0.5, socket_timeout=1.0,
        )
        await _redis_client.ping()
        logger.info("Rate limiter connected to Redis")
        return _redis_client
    except Exception as e:
        logger.warning("Rate limiter using in-memory fallback: %s", e)
        _redis_client = None
        return None


async def get_redis_client():
    """Return the shared Redis connection, initializing it when necessary."""
    if _redis_client is None:
        await _init_redis()
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def _rate_identity(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """One bucket per network a client actually controls (review WEB-8).

    ``::ffff:198.51.100.7`` is the IPv4 client 198.51.100.7, so it shares that
    bucket. An IPv6 end site is routinely handed a whole /64, so per-address
    buckets gave a single client 2**64 of them on the anonymous login, reset
    and lead-intake limits; the /64 is the identity."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _get_client_ip(request: Request) -> str:
    remote = request.client.host if request.client else "unknown"
    if not _trust_proxy_headers():
        try:
            return _rate_identity(ipaddress.ip_address(remote))
        except ValueError:
            return remote
    forwarded = request.headers.get("X-Forwarded-For", "")[:1_024]
    # Managed ingresses append hops on the right. Walk that chain from the
    # trusted edge inward and ignore private/internal proxy addresses.
    for raw_candidate in reversed(forwarded.split(",")):
        candidate = raw_candidate.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not _is_internal_ip(address):
            return _rate_identity(address)
    try:
        return _rate_identity(ipaddress.ip_address(remote))
    except ValueError:
        return remote


def _get_limit_for_path(path: str) -> int:
    for endpoint, limit in RATE_LIMITS.items():
        if path.startswith(endpoint):
            return limit
    return RATE_LIMITS["/api/"]


def _get_bucket_for_path(path: str) -> str:
    for endpoint in RATE_LIMITS:
        if path.startswith(endpoint):
            return endpoint
    return "/other/"


def _authenticated_principal(request: Request) -> str | None:
    """Return an opaque rate-limit identity for a valid browser/API session.

    Merely supplying a different cookie must never create a fresh quota, so the
    token is signature/expiry checked before its subject is used.  The returned
    value is hashed to keep tenant and agent identifiers out of Redis keys and
    logs.
    """

    authorization = request.headers.get("authorization", "").strip()
    raw_token = ""
    if authorization.lower().startswith("bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
    elif request.cookies.get("oracle_session"):
        raw_token = request.cookies["oracle_session"]
    if not raw_token:
        return None

    try:
        # Lazy import avoids making the middleware/auth module relationship a
        # startup cycle.  decode_token validates the configured JWT algorithm,
        # signature, issuer/audience (when configured), and expiry.
        from auth import decode_token

        claims = decode_token(raw_token)
    except HTTPException:  # invalid auth is handled by the route itself
        return None

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        return None
    tenant = str(claims.get("tenant_id") or "unscoped").strip()
    digest = hashlib.sha256(f"{tenant}:{subject}".encode("utf-8")).hexdigest()
    return f"principal:{digest}"


async def _check_rate_limit_memory(ip: str, endpoint: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    window_start = now - WINDOW_SECONDS

    async with _memory_lock:
        requests = _endpoint_requests[endpoint][ip]
        # Filter to requests within window
        requests[:] = [t for t in requests if t > window_start]
        current_count = len(requests)

        if current_count >= limit:
            return False, current_count

        requests.append(now)
        return True, current_count + 1


# KEYS[1] window key; ARGV: now, window_start, limit, member, ttl.
# Returns {allowed (1/0), count after this request}.
_SLIDING_WINDOW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[2])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then
  return {0, count}
end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return {1, count + 1}
"""


# Circuit breaker. After a Valkey failure the limiter goes straight to the
# PostgreSQL window for this long, instead of paying a failed round trip (and
# its timeout) on every request; then it tries Valkey again. It also brings a
# process that booted while Valkey was down back onto Valkey — before, a
# failed startup ping meant PostgreSQL until the next restart.
_BREAKER_SECONDS = 10.0
_redis_retry_at = 0.0


def _trip_breaker(exc: Exception) -> None:
    global _redis_retry_at
    if time.monotonic() >= _redis_retry_at:
        logger.warning("Valkey unavailable (%s); rate limits use PostgreSQL for %.0fs",
                       exc, _BREAKER_SECONDS)
    _redis_retry_at = time.monotonic() + _BREAKER_SECONDS


async def _check_rate_limit_redis(ip: str, endpoint: str, limit: int) -> tuple[bool, int]:
    if time.monotonic() < _redis_retry_at:
        return await _check_rate_limit_postgres(ip, endpoint, limit)
    if _redis_client is None:
        if not os.environ.get("REDIS_URL", "").strip():
            return await _check_rate_limit_postgres(ip, endpoint, limit)
        if await _init_redis() is None:
            _trip_breaker(RuntimeError("reconnect failed"))
            return await _check_rate_limit_postgres(ip, endpoint, limit)

    key = f"rate:{endpoint}:{ip}"
    try:
        now = time.time()
        # Atomic check-then-add. The old pipeline ZADDed every request,
        # REJECTED ones included, so a client that kept retrying stayed pinned
        # at the limit indefinitely (600 allowed in 130 s at 25 req/s, Mission
        # 8) — unlike the PostgreSQL fallback, which counts only admissions.
        # The member is unique per request: `str(now)` collided across
        # replicas in the same microsecond and undercounted.
        allowed, count = await _redis_client.eval(
            _SLIDING_WINDOW_LUA, 1, key, now, now - WINDOW_SECONDS, limit,
            f"{now}:{uuid.uuid4().hex[:8]}", WINDOW_SECONDS + 1,
        )
        return bool(allowed), int(count)
    except Exception as e:
        _trip_breaker(e)
        return await _check_rate_limit_postgres(ip, endpoint, limit)


async def _check_rate_limit_postgres(ip: str, endpoint: str, limit: int) -> tuple[bool, int]:
    """Atomic cross-replica fixed-window limit using the existing production DB."""
    from config import IS_DEV
    from db.connection import get_pool

    pool = get_pool()
    if pool is None:
        if IS_DEV:
            return await _check_rate_limit_memory(ip, endpoint, limit)
        logger.error("Distributed rate limiter unavailable: database pool is offline")
        return False, limit

    identity_hash = hashlib.sha256(ip.encode("utf-8", errors="replace")).hexdigest()
    window_start = int(time.time() // WINDOW_SECONDS) * WINDOW_SECONDS
    try:
        async with pool.acquire() as conn:
            if window_start % 900 == 0:
                await conn.execute(
                    "DELETE FROM api_rate_limit_windows WHERE expires_at < now()"
                )
            count = await conn.fetchval(
                """
                INSERT INTO api_rate_limit_windows
                    (identity_hash, endpoint_bucket, window_start, request_count, expires_at)
                VALUES ($1, $2, to_timestamp($3), 1, to_timestamp($3) + interval '2 minutes')
                ON CONFLICT (identity_hash, endpoint_bucket, window_start)
                DO UPDATE SET request_count = api_rate_limit_windows.request_count + 1
                WHERE api_rate_limit_windows.request_count < $4
                RETURNING request_count
                """,
                identity_hash,
                endpoint,
                window_start,
                limit,
            )
        return (count is not None, int(count or limit))
    except Exception:
        logger.exception("PostgreSQL rate limit check failed")
        if IS_DEV:
            return await _check_rate_limit_memory(ip, endpoint, limit)
        return False, limit


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Global rate limiting middleware."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.exempt_paths = {
            "/health",
            "/ready",
            "/metrics",
            "/favicon.ico",
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path

        # Skip exempt paths
        if path in self.exempt_paths:
            return await call_next(request)

        # Skip WebSocket (handled separately)
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # Skip static assets
        if path.startswith("/static/") or path.endswith((".js", ".css", ".png", ".jpg", ".ico", ".svg")):
            return await call_next(request)

        is_preflight = request.method == "OPTIONS"
        if is_preflight:
            # A preflight is a browser permission probe, not an application
            # action, so charging it against the endpoint's bucket would halve a
            # cross-origin client's effective quota. It still gets its own
            # ceiling — an entirely unmetered method is the cheapest way to walk
            # the full middleware chain and route resolution for free.
            limit = PREFLIGHT_RATE_LIMIT
            bucket = "OPTIONS"
            identity = _get_client_ip(request)
        else:
            limit = _get_limit_for_path(path)
            bucket = _get_bucket_for_path(path)
            identity = _get_client_ip(request)
            if path.startswith("/api/"):
                # Resolve the principal for EVERY /api/ path, not only the
                # generic bucket. A path with its own RATE_LIMITS entry — chat,
                # tours — previously stayed keyed on client IP even for a fully
                # authenticated user, so an office behind one NAT shared 20 chat
                # messages a minute between all of them.
                #
                # The per-path limits themselves were never wrong; the identity
                # was. 20/min is a sensible allowance *per agent*, so the bucket
                # and limit stay put and only the identity changes.
                principal = _authenticated_principal(request)
                if principal:
                    identity = principal
                    if bucket == "/api/":
                        # The catch-all exists as a ceiling for unattributed
                        # traffic; a caller we can name gets the higher
                        # documented allowance instead.
                        bucket = "/api/authenticated"
                        limit = AUTHENTICATED_API_RATE_LIMIT

            # Anonymous callers are unchanged: identity stays the client IP, so
            # an unauthenticated flood is still contained per source.

        allowed, count = await _check_rate_limit_redis(identity, bucket, limit)

        if not allowed:
            logger.warning(
                "Rate limit exceeded: identity=%s path=%s limit=%d count=%d",
                identity,
                path,
                limit,
                count,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please wait before trying again.",
                    "code": "RATE_LIMITED",
                    "retry_after": WINDOW_SECONDS,
                },
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response
