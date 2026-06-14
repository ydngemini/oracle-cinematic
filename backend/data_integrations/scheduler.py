"""
data_integrations/scheduler.py — Async worker pool with circuit breakers.

Runs enrichment jobs (geocode, flood, school district) for leads
missing data. Backpressure via Semaphore, circuit breaker per source.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("oracle.di.scheduler")

DEFAULT_CONCURRENCY = 10
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_COOLDOWN = 300


class JobType(enum.Enum):
    ENRICH_LEADS = "enrich_leads"
    ASSESSOR_HARVEST = "assessor_harvest"
    GIS_HARVEST = "gis_harvest"
    GEOCODE_LEADS = "geocode_leads"
    USPS_NORMALIZE = "usps_normalize"


@dataclass
class Job:
    job_type: JobType
    source: str
    payload: dict = field(default_factory=dict)
    priority: int = 5
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __lt__(self, other: "Job") -> bool:
        return self.priority < other.priority


@dataclass
class CircuitBreaker:
    source: str
    threshold: int = CIRCUIT_BREAKER_THRESHOLD
    cooldown: float = CIRCUIT_COOLDOWN
    _errors: int = 0
    _opened_at: Optional[float] = None

    def record_success(self) -> None:
        self._errors = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._errors += 1
        if self._errors >= self.threshold:
            self._opened_at = time.monotonic()
            logger.warning("Circuit OPEN for '%s' after %d errors", self.source, self._errors)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at > self.cooldown:
            self._opened_at = None
            self._errors = 0
            return False
        return True


class EnrichmentWorkerPool:
    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY):
        self._concurrency = concurrency
        self._sem = asyncio.Semaphore(concurrency)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._completed = 0
        self._failed = 0

    async def start(self, num_workers: int = 4) -> None:
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"di-worker-{i}")
            for i in range(num_workers)
        ]

    async def stop(self) -> None:
        self._running = False
        for _ in self._workers:
            await self._queue.put((0, None, None))
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def drain(self) -> None:
        await self._queue.join()

    async def submit(self, job: Job, handler: Callable[..., Coroutine[Any, Any, None]]) -> None:
        await self._queue.put((job.priority, job, handler))

    def _breaker(self, source: str) -> CircuitBreaker:
        if source not in self._breakers:
            self._breakers[source] = CircuitBreaker(source=source)
        return self._breakers[source]

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                priority, job, handler = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            if job is None:
                self._queue.task_done()
                break

            breaker = self._breaker(job.source)
            if breaker.is_open():
                self._queue.task_done()
                continue

            async with self._sem:
                try:
                    await handler(job)
                    breaker.record_success()
                    self._completed += 1
                except Exception as exc:
                    breaker.record_failure()
                    self._failed += 1
                    logger.error("Worker %d job %s failed: %s", worker_id, job.job_id, exc)
                finally:
                    self._queue.task_done()

    def stats(self) -> dict:
        return {
            "completed": self._completed,
            "failed": self._failed,
            "queued": self._queue.qsize(),
            "running": self._running,
        }
