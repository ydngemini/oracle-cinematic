"""Distributed rate limiting using Azure Managed Redis.

Provides cross-replica rate limiting for AI chat:
- Sliding window: 20 requests per minute per agent
- Concurrent limit: 2 active assistant responses per agent
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from tenancy import TenantContext

_REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_REDIS_AVAILABLE = False
_Redis = None

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
    _Redis = aioredis.Redis
except ImportError:
    _REDIS_AVAILABLE = False

_redis_client = None
_redis_lock: Optional[asyncio.Lock] = asyncio.Lock()


def _get_redis() -> Optional[Redis]:
    """Get or create the shared Redis client."""
    global _redis_client, _redis_lock
    if not _REDIS_AVAILABLE:
        return None
    if _redis_client is not None:
        return _redis_client
    if _redis_lock is None:
        import asyncio
        _redis_lock = asyncio.Lock()
    return _redis_client


async def _init_redis() -> Optional[Redis]:
    """Initialize Redis connection for rate limiting."""
    global _redis_client, _redis_lock
    if not _REDIS_AVAILABLE or not _REDIS_URL:
        return None
    if _redis_client is not None:
        return _redis_client
    if _redis_lock is None:
        _redis_lock = asyncio.Lock()
    async with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            _redis_client = aioredis.from_url(
                _REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None
            return None


async def close_redis() -> None:
    """Close Redis connection on shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


class DistributedRateLimiter:
    """Redis-backed distributed rate limiter for AI chat.

    Uses sliding window log for request rate limiting and atomic counters
    for concurrency limiting. All operations are atomic and work across
    multiple backend replicas connected to the same Azure Cache for Redis.
    """

    def __init__(self, redis: Any):
        self._redis = redis

    @staticmethod
    def _rate_key(ctx: TenantContext, window_seconds: int) -> str:
        """Key for sliding window rate limiting (per agent, per time window)."""
        window = int(time.time() // window_seconds)
        return f"ai-chat:ratelimit:{ctx.tenant_id}:{ctx.agent_id}:{window}"

    @staticmethod
    def _concurrency_key(ctx: TenantContext) -> str:
        """Key for active response counter (per agent)."""
        return f"ai-chat:active:{ctx.tenant_id}:{ctx.agent_id}"

    @staticmethod
    def _request_id_key(ctx: TenantContext, request_id: str) -> str:
        """Key for idempotency / duplicate detection."""
        return f"ai-chat:req:{ctx.tenant_id}:{ctx.agent_id}:{request_id}"

    async def check_rate_limit(
        self,
        ctx: TenantContext,
        *,
        max_requests: int = 20,
        window_seconds: int = 60,
    ) -> tuple[bool, int]:
        """Check if request is within rate limit.

        Returns (allowed, current_count). Uses sliding window log with Redis
        sorted set for accurate cross-replica limiting.
        """
        key = self._rate_key(ctx, window_seconds)
        now = time.time()
        window_start = now - window_seconds

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds + 1)
        results = await pipe.execute()

        current_count = results[1] + 1
        return current_count <= max_requests, current_count

    async def check_concurrency_limit(
        self,
        ctx: TenantContext,
        *,
        max_active: int = 2,
    ) -> tuple[bool, int]:
        """Check if another response can be started.

        Returns (allowed, current_active). Uses atomic INCR with TTL.
        """
        key = self._concurrency_key(ctx)
        current = await self._redis.incr(key)
        if current == 1:
            await self._redis.expire(key, 3600)
        if current <= max_active:
            return True, current
        # INCR is the reservation. A rejected caller must return its slot or it
        # will block subsequent requests until the one-hour safety TTL expires.
        await self.release_concurrency(ctx)
        return False, current

    async def release_concurrency(self, ctx: TenantContext) -> int:
        """Decrement active response counter.

        Returns new count after decrement.
        """
        key = self._concurrency_key(ctx)
        current = await self._redis.decr(key)
        if current <= 0:
            await self._redis.delete(key)
            return 0
        return current

    async def check_duplicate_request(
        self,
        ctx: TenantContext,
        request_id: str,
        *,
        ttl_seconds: int = 3600,
    ) -> tuple[bool, Optional[str]]:
        """Check if request_id was already processed.

        Returns (is_duplicate, existing_message_id). Sets key if new.
        """
        key = self._request_id_key(ctx, request_id)
        existing = await self._redis.get(key)
        if existing:
            return True, existing
        await self._redis.setex(key, ttl_seconds, "processing")
        return False, None

    async def mark_request_completed(
        self,
        ctx: TenantContext,
        request_id: str,
        message_id: str,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        """Mark request as completed with its message ID."""
        key = self._request_id_key(ctx, request_id)
        await self._redis.setex(key, ttl_seconds, message_id)

    async def mark_request_failed(
        self,
        ctx: TenantContext,
        request_id: str,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        """Mark request as failed (allows retry)."""
        key = self._request_id_key(ctx, request_id)
        await self._redis.delete(key)


@asynccontextmanager
async def distributed_rate_limiter() -> AsyncIterator[Optional[DistributedRateLimiter]]:
    """Context manager for distributed rate limiter.

    Yields None if Redis unavailable (falls back to DB-only limiting).
    """
    redis = await _init_redis()
    if redis is None:
        yield None
        return
    limiter = DistributedRateLimiter(redis)
    try:
        yield limiter
    finally:
        pass
