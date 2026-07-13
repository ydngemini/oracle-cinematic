"""Mandatory two-tier integration cache with request deduplication.

Redis is an optional L1; PostgreSQL remains the durable L2.  Canonical request
hashes intentionally exclude credentials, source-specific TTLs are centralized,
and stale-while-revalidate keeps upstream outages from blanking the product.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
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
    "avm": 7 * 86_400,
    "mls": 24 * 3_600,
    "municipal_violations": 30 * 60,
}

STALE_TTL = {
    "mls": 7 * 86_400,
    "municipal_violations": 3 * 86_400,
    "avm": 30 * 86_400,
    "default": 7 * 86_400,
}

_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "x_api_key",
        "x_app_token",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
    }
)


def _normalized_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _without_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if _normalized_key(key) not in _SECRET_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_without_secrets(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def canonical_request_hash(source: str, request: Mapping[str, Any]) -> str:
    """Hash a stable, credential-free representation of an upstream request."""
    material = {
        "source": source.strip().lower(),
        "request": _without_secrets(dict(request)),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_cache_key(source: str, request: Mapping[str, Any]) -> str:
    return f"{source.strip().lower()}:{canonical_request_hash(source, request)}"


class IntegrationCache:
    """Cache facade shared by every external connector."""

    _locks: dict[str, asyncio.Lock] = {}
    _refresh_tasks: set[asyncio.Task] = set()

    def __init__(self, pg_pool, redis_client=None):
        if pg_pool is None:
            raise ValueError("IntegrationCache requires a PostgreSQL pool")
        self._pg = pg_pool
        self._redis = redis_client
        self._metrics: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "stale_hits": 0,
            "writes": 0,
            "deduplicated": 0,
            "refresh_errors": 0,
        }

    @classmethod
    async def create(cls, pg_pool) -> "IntegrationCache":
        redis_client = None
        if _REDIS_AVAILABLE:
            try:
                redis_client = aioredis.from_url(
                    _REDIS_URL,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await redis_client.ping()
                logger.info("Redis L1 cache connected at %s", _REDIS_URL)
            except Exception as exc:  # noqa: BLE001 - PG is authoritative fallback
                logger.warning("Redis unavailable (%s) — PG-only cache", exc)
                redis_client = None
        return cls(pg_pool, redis_client)

    @classmethod
    def _lock_for(cls, key: str) -> asyncio.Lock:
        lock = cls._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[key] = lock
        return lock

    @staticmethod
    def _payload(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    async def _pg_row(self, key: str, *, include_stale: bool) -> Optional[dict[str, Any]]:
        condition = (
            "(expires_at IS NULL OR expires_at > now())"
            if not include_stale
            else "(stale_until IS NULL OR stale_until > now())"
        )
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT payload, expires_at, stale_until, source_name, request_hash
                    FROM di_cache
                    WHERE cache_key = $1 AND {condition}
                    """,
                    key,
                )
                if row:
                    await conn.execute(
                        """
                        UPDATE di_cache
                           SET hit_count=hit_count+1, last_hit_at=now()
                         WHERE cache_key=$1
                        """,
                        key,
                    )
        except Exception as exc:  # noqa: BLE001 - cache failure is observable degradation
            logger.warning("PG cache GET failed for %s: %s", key, exc)
            return None
        if not row:
            return None
        return {
            "payload": self._payload(row["payload"]),
            "expires_at": row["expires_at"],
            "stale_until": row["stale_until"],
            "source_name": row["source_name"],
            "request_hash": row["request_hash"],
        }

    async def get(self, key: str) -> Optional[dict]:
        if self._redis:
            try:
                raw = await self._redis.get(f"di:{key}")
                if raw:
                    self._metrics["hits"] += 1
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Redis GET failed for %s: %s", key, exc)

        row = await self._pg_row(key, include_stale=False)
        if row:
            payload = row["payload"]
            self._metrics["hits"] += 1
            if self._redis:
                try:
                    await self._redis.setex(f"di:{key}", 3_600, json.dumps(payload))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Redis warm-cache SET failed for %s: %s", key, exc)
            return payload

        self._metrics["misses"] += 1
        return None

    async def get_stale(self, key: str) -> Optional[dict]:
        row = await self._pg_row(key, include_stale=True)
        if not row:
            return None
        expires_at = row.get("expires_at")
        if expires_at is None or expires_at > datetime.now(timezone.utc):
            return None
        self._metrics["stale_hits"] += 1
        return row["payload"]

    async def set(
        self,
        key: str,
        value: dict,
        ttl: int = 86_400,
        *,
        source: Optional[str] = None,
        request_hash: Optional[str] = None,
        stale_ttl: Optional[int] = None,
    ) -> None:
        ttl = max(1, int(ttl))
        source_name = (source or key.partition(":")[0] or "unknown")[:120]
        request_digest = request_hash or (
            key.rsplit(":", 1)[-1] if len(key.rsplit(":", 1)[-1]) == 64 else None
        )
        stale_seconds = max(ttl, int(stale_ttl or STALE_TTL.get(source_name, STALE_TTL["default"])))
        blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

        if self._redis:
            try:
                await self._redis.setex(f"di:{key}", ttl, blob)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Redis SET failed for %s: %s", key, exc)

        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO di_cache (
                        cache_key, payload, expires_at, stale_until, source_name,
                        request_hash, updated_at
                    ) VALUES (
                        $1, $2::jsonb,
                        now() + ($3 || ' seconds')::interval,
                        now() + ($4 || ' seconds')::interval,
                        $5, $6, now()
                    )
                    ON CONFLICT (cache_key) DO UPDATE
                      SET payload = EXCLUDED.payload,
                          expires_at = EXCLUDED.expires_at,
                          stale_until = EXCLUDED.stale_until,
                          source_name = EXCLUDED.source_name,
                          request_hash = EXCLUDED.request_hash,
                          updated_at = now()
                    """,
                    key,
                    blob,
                    str(ttl),
                    str(stale_seconds),
                    source_name,
                    request_digest,
                )
            self._metrics["writes"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("PG cache SET failed for %s: %s", key, exc)

    async def _refresh(
        self,
        *,
        key: str,
        source: str,
        digest: str,
        fetcher: Callable[[], Awaitable[dict]],
        ttl: int,
        stale_ttl: int,
    ) -> dict:
        value = await fetcher()
        if not isinstance(value, dict):
            raise TypeError("integration fetcher must return a dictionary")
        await self.set(
            key,
            value,
            ttl,
            source=source,
            request_hash=digest,
            stale_ttl=stale_ttl,
        )
        return value

    async def get_or_fetch(
        self,
        source: str,
        request: Mapping[str, Any],
        fetcher: Callable[[], Awaitable[dict]],
        *,
        ttl: Optional[int] = None,
        stale_ttl: Optional[int] = None,
    ) -> dict:
        """Return cached data or run one deduplicated upstream request.

        If a stale-but-servable value exists, it is returned immediately and a
        single tracked background refresh starts.  A refresh failure preserves
        the stale row and increments observable metrics.
        """
        digest = canonical_request_hash(source, request)
        key = f"{source}:{digest}"
        source_ttl = int(ttl or TTL.get(source, 86_400))
        source_stale_ttl = int(stale_ttl or STALE_TTL.get(source, STALE_TTL["default"]))

        cached = await self.get(key)
        if cached is not None:
            return cached

        lock = self._lock_for(key)
        if lock.locked():
            self._metrics["deduplicated"] += 1
        async with lock:
            cached = await self.get(key)
            if cached is not None:
                return cached
            stale = await self.get_stale(key)
            if stale is not None:
                task = asyncio.create_task(
                    self._background_refresh(
                        key=key,
                        source=source,
                        digest=digest,
                        fetcher=fetcher,
                        ttl=source_ttl,
                        stale_ttl=source_stale_ttl,
                    ),
                    name=f"cache-refresh-{source}-{digest[:8]}",
                )
                self._refresh_tasks.add(task)
                task.add_done_callback(self._refresh_tasks.discard)
                return stale
            return await self._refresh(
                key=key,
                source=source,
                digest=digest,
                fetcher=fetcher,
                ttl=source_ttl,
                stale_ttl=source_stale_ttl,
            )

    async def _background_refresh(self, **kwargs: Any) -> None:
        try:
            await self._refresh(**kwargs)
        except Exception as exc:  # noqa: BLE001 - stale value remains available
            self._metrics["refresh_errors"] += 1
            logger.warning("stale cache refresh failed for %s: %s", kwargs.get("key"), exc)

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    async def invalidate(self, key: str) -> None:
        if self._redis:
            try:
                await self._redis.delete(f"di:{key}")
            except Exception:  # noqa: BLE001
                pass
        try:
            async with self._pg.acquire() as conn:
                await conn.execute("DELETE FROM di_cache WHERE cache_key = $1", key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache invalidate failed for %s: %s", key, exc)

    async def invalidate_prefix(self, prefix: str) -> None:
        if self._redis:
            try:
                keys = await self._redis.keys(f"di:{prefix}*")
                if keys:
                    await self._redis.delete(*keys)
            except Exception:  # noqa: BLE001
                pass
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    "DELETE FROM di_cache WHERE cache_key LIKE $1", f"{prefix}%"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache invalidate_prefix failed for %s*: %s", prefix, exc)
