"""
data_integrations/cache.py — Two-tier integration cache (Redis L1 / PostgreSQL L2).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("oracle.di.cache")

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

TTL = {
    "fema_flood": 30 * 86_400,
    "census_acs": 365 * 86_400,
    "census_tiger": 365 * 86_400,
    "geocode": 90 * 86_400,
    "usps": 30 * 86_400,
    "school_district": 180 * 86_400,
    "county_assessor": 7 * 86_400,
    "state_gis": 7 * 86_400,
}


class IntegrationCache:
    def __init__(self, pg_pool, redis_client=None):
        self._pg = pg_pool
        self._redis = redis_client

    @classmethod
    async def create(cls, pg_pool) -> "IntegrationCache":
        redis_client = None
        if _REDIS_AVAILABLE:
            try:
                redis_client = await aioredis.from_url(
                    _REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await redis_client.ping()
                logger.info("Redis L1 cache connected at %s", _REDIS_URL)
            except Exception as e:
                logger.warning("Redis unavailable (%s) — PG-only cache", e)
                redis_client = None
        return cls(pg_pool, redis_client)

    async def get(self, key: str) -> Optional[dict]:
        if self._redis:
            try:
                raw = await self._redis.get(f"di:{key}")
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.debug("Redis GET failed for %s: %s", key, e)

        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT payload FROM di_cache
                    WHERE cache_key = $1
                      AND (expires_at IS NULL OR expires_at > now())
                    """,
                    key,
                )
            if row:
                if self._redis:
                    try:
                        await self._redis.setex(f"di:{key}", 3600, json.dumps(row["payload"]))
                    except Exception:
                        pass
                return row["payload"]
        except Exception as e:
            logger.warning("PG cache GET failed for %s: %s", key, e)

        return None

    async def set(self, key: str, value: dict, ttl: int = 86_400) -> None:
        blob = json.dumps(value)

        if self._redis:
            try:
                await self._redis.setex(f"di:{key}", ttl, blob)
            except Exception as e:
                logger.debug("Redis SET failed for %s: %s", key, e)

        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO di_cache (cache_key, payload, expires_at, updated_at)
                    VALUES ($1, $2::jsonb, now() + ($3 || ' seconds')::interval, now())
                    ON CONFLICT (cache_key) DO UPDATE
                      SET payload = EXCLUDED.payload,
                          expires_at = EXCLUDED.expires_at,
                          updated_at = now()
                    """,
                    key, blob, str(ttl),
                )
        except Exception as e:
            logger.warning("PG cache SET failed for %s: %s", key, e)

    async def invalidate(self, key: str) -> None:
        if self._redis:
            try:
                await self._redis.delete(f"di:{key}")
            except Exception:
                pass
        try:
            async with self._pg.acquire() as conn:
                await conn.execute("DELETE FROM di_cache WHERE cache_key = $1", key)
        except Exception as e:
            logger.warning("Cache invalidate failed for %s: %s", key, e)

    async def invalidate_prefix(self, prefix: str) -> None:
        if self._redis:
            try:
                keys = await self._redis.keys(f"di:{prefix}*")
                if keys:
                    await self._redis.delete(*keys)
            except Exception:
                pass
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    "DELETE FROM di_cache WHERE cache_key LIKE $1",
                    f"{prefix}%",
                )
        except Exception as e:
            logger.warning("Cache invalidate_prefix failed for %s*: %s", prefix, e)
