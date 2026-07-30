"""Tenant-keyed WebSocket hub with PostgreSQL cross-replica fan-out.

Each process owns only its local socket objects.  ``LISTEN/NOTIFY`` carries a
small, tenant-scoped envelope between ECS replicas, while the local registry
delivers it to browser sockets.  PostgreSQL is already a required platform
dependency, so this avoids an unaudited second broker and keeps long-running
job/negotiation telemetry working when the backend service scales past one
task.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from typing import Any, Optional

from fastapi import WebSocket

logger = logging.getLogger("oracle.ws_hub")

_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_user_sockets: dict[tuple[str, str], set[WebSocket]] = defaultdict(set)
_CHANNEL = "oracle_ws_events"
_MAX_NOTIFY_BYTES = 7_500  # PostgreSQL NOTIFY payload hard limit is 8,000 bytes.
_instance_id = str(uuid.uuid4())
_pool: Any = None
_listener_connection: Any = None
_listener_started = False

# The platform tenant's sockets double as an all-tenant firehose: every frame
# broadcast to any tenant is mirrored there (annotated with source_tenant) so
# the platform-admin OPS console watches live traffic across the whole fleet.
# Membership in this group is JWT-gated — server._resolve_websocket_identity only returns
# the platform tenant for a verified platform_admin token, never for the
# spoofable tenant_id query param.
FIREHOSE_TENANT_ID = os.getenv(
    "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)


def register(tenant_id: str, ws: WebSocket, user_id: Optional[str] = None) -> None:
    _sockets[tenant_id].add(ws)
    if user_id:
        _user_sockets[(tenant_id, user_id)].add(ws)


def unregister(tenant_id: str, ws: WebSocket, user_id: Optional[str] = None) -> None:
    _sockets[tenant_id].discard(ws)
    if not _sockets[tenant_id]:
        del _sockets[tenant_id]
    if user_id:
        key = (tenant_id, user_id)
        _user_sockets[key].discard(ws)
        if not _user_sockets[key]:
            del _user_sockets[key]


def connection_counts() -> dict[str, int]:
    """Live socket count per tenant group — surfaced on /api/admin/system."""
    return {tenant_id: len(socks) for tenant_id, socks in _sockets.items()}


async def _send_group(tenant_id: str, payload: dict) -> int:
    """Send one frame to every live socket in one tenant group.

    Dead sockets are dropped from the registry rather than raising — the WS
    handler's own finally block is the authoritative unregister path; this is
    just fast-path cleanup. Returns the number of sockets actually reached.
    """
    targets = list(_sockets.get(tenant_id, ()))
    if not targets:
        return 0

    text = json.dumps(payload)
    delivered = 0
    for ws in targets:
        try:
            await ws.send_text(text)
            delivered += 1
        except Exception:  # noqa: BLE001 — socket died between snapshot and send
            unregister(tenant_id, ws)
    return delivered


async def _send_user(tenant_id: str, user_id: str, payload: dict) -> int:
    """Send a private frame only to one authenticated agent's live sockets."""
    key = (tenant_id, user_id)
    targets = list(_user_sockets.get(key, ()))
    if not targets:
        return 0
    text = json.dumps(payload)
    delivered = 0
    for ws in targets:
        try:
            await ws.send_text(text)
            delivered += 1
        except Exception:  # noqa: BLE001
            unregister(tenant_id, ws, user_id)
    return delivered


async def _deliver_local(tenant_id: str, payload: dict) -> int:
    """Deliver to sockets owned by this process, including the admin firehose."""
    delivered = await _send_group(tenant_id, payload)
    if tenant_id != FIREHOSE_TENANT_ID and FIREHOSE_TENANT_ID in _sockets:
        delivered += await _send_group(
            FIREHOSE_TENANT_ID, {**payload, "source_tenant": tenant_id}
        )
    return delivered


async def _deliver_user_local(tenant_id: str, user_id: str, payload: dict) -> int:
    # Private assistant content is intentionally never mirrored to the platform
    # firehose. Platform operators can inspect health metrics, not conversations.
    return await _send_user(tenant_id, user_id, payload)


async def _publish(tenant_id: str, payload: dict, user_id: Optional[str] = None) -> None:
    if _pool is None or not _listener_started:
        return
    envelope = json.dumps(
        {
            "origin": _instance_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "payload": payload,
        },
        separators=(",", ":"),
        default=str,
    )
    if len(envelope.encode("utf-8")) > _MAX_NOTIFY_BYTES:
        logger.error(
            "WebSocket event too large for cross-replica delivery (tenant=%s bytes=%d)",
            tenant_id,
            len(envelope.encode("utf-8")),
        )
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, envelope)
    except Exception as exc:  # noqa: BLE001 - local delivery remains available
        logger.warning("WebSocket cross-replica publish failed: %s", exc)


async def _receive_notification(raw: str) -> None:
    try:
        envelope = json.loads(raw)
        if envelope.get("origin") == _instance_id:
            return
        tenant_id = str(envelope["tenant_id"])
        user_id = envelope.get("user_id")
        if user_id is not None and not isinstance(user_id, str):
            raise TypeError("user_id is not a string")
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise TypeError("payload is not an object")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring malformed WebSocket notification: %s", exc)
        return
    if user_id:
        await _deliver_user_local(tenant_id, user_id, payload)
    else:
        await _deliver_local(tenant_id, payload)


def _notification_callback(
    _connection: Any,
    _pid: int,
    _channel: str,
    payload: str,
) -> None:
    asyncio.create_task(_receive_notification(payload))


async def start(pool: Optional[Any] = None) -> bool:
    """Start the dedicated PostgreSQL listener once per backend process."""
    global _pool, _listener_connection, _listener_started
    if _listener_started:
        return True
    if pool is None:
        from db.connection import get_pool

        pool = get_pool()
    if pool is None:
        logger.warning("WebSocket hub running local-only because the DB pool is unavailable.")
        return False
    connection = None
    try:
        connection = await pool.acquire()
        await connection.add_listener(_CHANNEL, _notification_callback)
    except Exception as exc:  # noqa: BLE001 - app can still serve local sockets
        logger.warning("WebSocket PostgreSQL listener failed to start: %s", exc)
        if connection is not None:
            try:
                await pool.release(connection)
            except Exception as release_exc:  # noqa: BLE001
                logger.debug("Failed to release WebSocket listener after startup error: %s", release_exc)
        return False
    _pool = pool
    _listener_connection = connection
    _listener_started = True
    logger.info("WebSocket cross-replica listener online (instance=%s)", _instance_id)
    return True


async def stop() -> None:
    """Release the dedicated listener connection during application shutdown."""
    global _pool, _listener_connection, _listener_started
    connection = _listener_connection
    pool = _pool
    _listener_connection = None
    _listener_started = False
    _pool = None
    if connection is None or pool is None:
        return
    try:
        await connection.remove_listener(_CHANNEL, _notification_callback)
    except Exception as exc:  # noqa: BLE001
        logger.debug("WebSocket listener removal failed: %s", exc)
    try:
        await pool.release(connection)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebSocket listener connection release failed: %s", exc)


async def broadcast(tenant_id: str, payload: dict) -> int:
    """Deliver locally and publish once for every other backend replica."""
    delivered = await _deliver_local(tenant_id, payload)
    await _publish(tenant_id, payload)
    return delivered


async def broadcast_user(tenant_id: str, user_id: str, payload: dict) -> int:
    """Deliver a private frame across replicas to one tenant agent only."""
    if not user_id:
        raise ValueError("user_id is required for a private WebSocket broadcast")
    delivered = await _deliver_user_local(tenant_id, user_id, payload)
    await _publish(tenant_id, payload, user_id)
    return delivered
