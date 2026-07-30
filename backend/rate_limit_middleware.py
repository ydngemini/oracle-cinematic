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
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("oracle.rate_limit")

# Rate limit configuration (requests per minute per IP)
RATE_LIMITS = {
    "/auth/login": 10,
    "/auth/register": 5,
    "/auth/forgot": 3,
    "/auth/reset": 3,
    "/api/ai/chat": 20,
    "/api/crm/tour": 5,
    "/api/": 100,  # Default for all other API endpoints
}

# Burst allowance (extra requests allowed in short bursts)
BURST_MULTIPLIER = 1.5
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
        _redis_client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
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


def _get_client_ip(request: Request) -> str:
    remote = request.client.host if request.client else "unknown"
    if not _trust_proxy_headers():
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
        if not _is_internal_ip(address):
            return str(address)
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
    return "/api/"


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


async def _check_rate_limit_redis(ip: str, endpoint: str, limit: int) -> tuple[bool, int]:
    if _redis_client is None:
        return await _check_rate_limit_postgres(ip, endpoint, limit)

    key = f"rate:{endpoint}:{ip}"
    try:
        pipe = _redis_client.pipeline()
        now = time.time()
        window_start = now - WINDOW_SECONDS

        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, WINDOW_SECONDS + 1)

        results = await pipe.execute()
        current_count = results[1]

        if current_count >= limit:
            return False, current_count

        return True, current_count + 1
    except Exception as e:
        logger.error("Redis rate limit failed: %s", e)
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

        ip = _get_client_ip(request)
        limit = _get_limit_for_path(path)

        bucket = _get_bucket_for_path(path)
        allowed, count = await _check_rate_limit_redis(ip, bucket, limit)

        if not allowed:
            logger.warning("Rate limit exceeded: ip=%s path=%s limit=%d count=%d", ip, path, limit, count)
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
