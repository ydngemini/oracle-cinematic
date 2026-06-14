"""
data_integrations/base.py — Abstract base class and shared machinery.

All integrations inherit DataSource. It provides:
  - Async HTTP via stdlib urllib in asyncio.to_thread (no extra deps)
  - Per-source RateLimiter with min-interval + jitter
  - RetryConfig-driven exponential backoff honouring Retry-After headers
  - Structured logging under oracle.di.<source_name>
  - normalize() hook that every source must implement
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class DataIntegrationError(Exception):
    pass


class RetryableError(DataIntegrationError):
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RateLimiter:
    min_interval: float = 1.0
    jitter: float = 0.3

    def __post_init__(self) -> None:
        self._last: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.min_interval - now
            wait += random.uniform(0, self.jitter)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


@dataclass
class RetryConfig:
    max_attempts: int = 5
    base_backoff: float = 2.0
    max_backoff: float = 120.0
    jitter: float = 1.0

    def backoff(self, attempt: int, hint: Optional[float] = None) -> float:
        if hint is not None:
            return hint + random.uniform(0, self.jitter)
        raw = self.base_backoff * (2 ** (attempt - 1))
        return min(raw, self.max_backoff) + random.uniform(0, self.jitter)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "urlerror")):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("timed out", "reset", "temporarily", "refused"))


def _parse_retry_after(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class DataSource(ABC):
    source_name: str = "unknown"
    _USER_AGENT: str = "Oracle-DataLayer/1.0 (+oracle-app.dev)"

    def __init__(
        self,
        rate_limiter: Optional[RateLimiter] = None,
        retry_config: Optional[RetryConfig] = None,
        cache: Optional[Any] = None,
    ) -> None:
        self._limiter = rate_limiter or RateLimiter()
        self._retry = retry_config or RetryConfig()
        self._cache = cache
        self._log = logging.getLogger(f"oracle.di.{self.source_name}")
        self._metrics: dict[str, int] = {
            "requests": 0, "retries": 0, "cache_hits": 0,
            "errors": 0, "normalized": 0,
        }

    @abstractmethod
    async def fetch(self, **kwargs: Any) -> Optional[dict]:
        ...

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        ...

    async def get(self, cache_key: str, **fetch_kwargs: Any) -> Optional[dict]:
        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                self._metrics["cache_hits"] += 1
                return cached

        raw = await self.fetch(**fetch_kwargs)
        if raw is None:
            return None

        result = self.normalize(raw)
        self._metrics["normalized"] += 1

        if self._cache:
            await self._cache.set(cache_key, result, ttl=self._cache_ttl())

        return result

    def metrics(self) -> dict:
        return {**self._metrics, "source": self.source_name}

    def _cache_ttl(self) -> int:
        return 86_400

    async def _get_json(
        self,
        url: str,
        headers: Optional[dict] = None,
        timeout: int = 15,
    ) -> Any:
        async def _do() -> Any:
            await self._limiter.acquire()
            self._metrics["requests"] += 1
            req = urllib.request.Request(
                url,
                headers={"User-Agent": self._USER_AGENT, **(headers or {})},
            )

            def _blocking():
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):
                        ra = _parse_retry_after(
                            e.headers.get("Retry-After") if e.headers else None
                        )
                        raise RetryableError(f"HTTP {e.code} from {self.source_name}", ra)
                    raise

            body = await asyncio.to_thread(_blocking)
            return json.loads(body.decode("utf-8", errors="replace"))

        return await self._with_retries(_do, label=f"GET {self.source_name}")

    async def _with_retries(self, coro_factory, *, label: str) -> Any:
        attempt = 0
        while True:
            try:
                return await coro_factory()
            except Exception as exc:
                attempt += 1
                retryable = isinstance(exc, RetryableError) or _is_transient(exc)
                if not retryable or attempt > self._retry.max_attempts:
                    self._metrics["errors"] += 1
                    self._log.error(
                        "%s failed permanently (attempt %d): %s", label, attempt, exc
                    )
                    raise
                self._metrics["retries"] += 1
                hint = getattr(exc, "retry_after", None)
                wait = self._retry.backoff(attempt, hint)
                self._log.warning(
                    "%s failed (attempt %d/%d): %s — retry in %.1fs",
                    label, attempt, self._retry.max_attempts, exc, wait,
                )
                await asyncio.sleep(wait)
