"""One WorkflowEngine per (tenant, replica), reference-counted.

Every `/ws` connection used to construct its own `WorkflowEngine`, which builds
its own `PropertyGraph` and seeds it from the tenant's leads. Fifty agents in one
brokerage therefore paid for fifty engines, fifty harvest/analysis task sets and
fifty seed queries — to produce fifty copies of the same pipeline view, because
the underlying leads are tenant-scoped and identical for all of them.

The engine is now acquired per tenant and shared. Its output reaches clients
through `ws_hub`, so a frame produced once is delivered to every socket the
tenant has on this replica.

**Reference counting, not a cache.** An engine runs background work, so it must
stop when the last viewer leaves — otherwise a tenant that logged out an hour ago
still harvests. But a page reload disconnects and reconnects within a second, and
tearing the engine down only to re-seed it immediately is worse than keeping it.
Hence the linger: release schedules a shutdown, and a reconnect inside the window
cancels it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("oracle.tenant_engines")

# How long an engine stays alive after its last socket closes. Sized for a page
# reload or a brief network blip, not for keeping idle tenants running.
ENGINE_LINGER_SECONDS = float(os.getenv("ORACLE_ENGINE_LINGER_SECONDS", "30"))


class _Entry:
    __slots__ = ("engine", "refcount", "shutdown_handle", "started")

    def __init__(self, engine: Any):
        self.engine = engine
        self.refcount = 0
        self.shutdown_handle: Optional[asyncio.Task] = None
        self.started = False


_entries: dict[str, _Entry] = {}
# Guards the acquire/release transitions. Engine construction and start are
# awaited, so two sockets connecting concurrently would otherwise both see "no
# engine" and build one.
_lock = asyncio.Lock()


async def acquire(
    tenant_id: str,
    *,
    factory,
) -> Any:
    """Return the tenant's engine, creating and starting it if needed.

    `factory` is a zero-argument callable returning a new engine; passing it in
    rather than importing WorkflowEngine here keeps this module free of the
    agent stack and therefore importable from tests without it.
    """
    async with _lock:
        entry = _entries.get(tenant_id)
        if entry is None:
            entry = _Entry(factory())
            _entries[tenant_id] = entry

        # A reconnect inside the linger window reclaims the running engine.
        if entry.shutdown_handle is not None:
            entry.shutdown_handle.cancel()
            entry.shutdown_handle = None
            logger.debug("Reclaimed lingering engine for tenant %s", tenant_id)

        entry.refcount += 1

        if not entry.started:
            entry.started = True
            # start() runs the engine's own background loops; it is awaited
            # inside the lock so a second caller cannot observe a half-started
            # engine.
            await entry.engine.start()
            logger.info(
                "Tenant engine started — tenant=%s refcount=%d", tenant_id, entry.refcount
            )
        return entry.engine


async def release(tenant_id: str) -> None:
    """Drop one reference; schedule shutdown when the last one goes."""
    async with _lock:
        entry = _entries.get(tenant_id)
        if entry is None:
            return
        entry.refcount = max(0, entry.refcount - 1)
        if entry.refcount > 0 or entry.shutdown_handle is not None:
            return
        entry.shutdown_handle = asyncio.create_task(_shutdown_after_linger(tenant_id))


async def _shutdown_after_linger(tenant_id: str) -> None:
    try:
        await asyncio.sleep(ENGINE_LINGER_SECONDS)
    except asyncio.CancelledError:
        return  # reclaimed by a reconnect

    async with _lock:
        entry = _entries.get(tenant_id)
        # Re-check under the lock: a socket may have arrived between the sleep
        # finishing and the lock being granted.
        if entry is None or entry.refcount > 0:
            return
        _entries.pop(tenant_id, None)

    try:
        await entry.engine.stop()
    except Exception as exc:  # noqa: BLE001 — shutdown must not raise
        logger.debug("engine.stop() raised for tenant %s: %s", tenant_id, exc)
    logger.info("Tenant engine stopped — tenant=%s", tenant_id)


async def shutdown_all() -> None:
    """Stop every engine. Called from the app lifespan on process exit."""
    async with _lock:
        entries = list(_entries.items())
        _entries.clear()

    for tenant_id, entry in entries:
        if entry.shutdown_handle is not None:
            entry.shutdown_handle.cancel()
        try:
            await entry.engine.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("engine.stop() raised for tenant %s: %s", tenant_id, exc)


def stats() -> dict[str, dict[str, Any]]:
    """Per-tenant engine state, for /api/admin/runtime-load."""
    return {
        tenant_id: {
            "refcount": entry.refcount,
            "started": entry.started,
            "lingering": entry.shutdown_handle is not None,
        }
        for tenant_id, entry in _entries.items()
    }


def engine_count() -> int:
    return len(_entries)
