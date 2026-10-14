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
import random
import time
import uuid
from collections import defaultdict
from typing import Any, Optional

from fastapi import WebSocket

logger = logging.getLogger("oracle.ws_hub")

_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_user_sockets: dict[tuple[str, str], set[WebSocket]] = defaultdict(set)
# Per-replica socket ceiling. Each connection costs an idle watchdog, a reader
# task and a share of the tenant engine; past some number a replica degrades for
# everyone on it rather than for the marginal connection. 0 disables the cap.
MAX_CONNECTIONS_PER_REPLICA = int(os.getenv("ORACLE_WS_MAX_CONNECTIONS", "120"))

_CHANNEL = "oracle_ws_events"
_MAX_NOTIFY_BYTES = 7_500  # PostgreSQL NOTIFY payload hard limit is 8,000 bytes.
_instance_id = str(uuid.uuid4())
_pool: Any = None
_listener_connection: Any = None
_listener_started = False
# The LISTEN connection is a single long-lived socket. Until 2026-10-02 it was
# acquired once and never replaced: after a database failover or connection
# reset this replica silently stopped receiving every frame published by the
# others (including worker→API chat replies) while /health stayed green.
# _listener_healthy tracks the current socket; a supervisor reconnects it with
# jittered backoff and a probe catches half-dead sockets no callback reports.
_listener_healthy = False
_supervisor_task: Optional[asyncio.Task] = None
_listener_stats = {"reconnects": 0, "last_error": None, "down_since": None, "last_ok": None}
_PROBE_SECONDS = float(os.getenv("ORACLE_WS_LISTENER_PROBE_SECONDS", "15"))
_RECONNECT_MAX_SECONDS = 30.0

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


def total_connections() -> int:
    """Sockets held by this replica, across every tenant."""
    return sum(len(socks) for socks in _sockets.values())


def at_capacity() -> bool:
    """Whether this replica is already holding its configured maximum.

    Connections were previously uncapped, so the failure mode under load was
    memory pressure and scheduler starvation rather than a clear refusal. A
    refused connection the UI can explain is a better outcome than a degraded
    one nobody can attribute.
    """
    return MAX_CONNECTIONS_PER_REPLICA > 0 and total_connections() >= MAX_CONNECTIONS_PER_REPLICA


def tenants_with_sockets() -> list[str]:
    """Tenants this replica currently holds at least one socket for.

    The ambient producer iterates this instead of every known tenant, so work is
    only done for tenants that have someone watching.
    """
    return [tenant_id for tenant_id, socks in _sockets.items() if socks]


async def deliver_local(tenant_id: str, payload: dict) -> int:
    """Deliver to this replica's sockets only — no cross-replica publish.

    `broadcast()` also NOTIFYs so other replicas can reach their own sockets,
    which costs a pool acquire and a `pg_notify` per frame. Ambient content is
    generated independently on every replica, so publishing it would duplicate
    the frame for anyone connected elsewhere while adding database work per
    frame per replica. Use this for anything a replica generates for itself;
    use `broadcast()` for facts that originate in one place and must reach
    everyone.
    """
    return await _deliver_local(tenant_id, payload)


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


def firehose_copy(payload: dict, tenant_id: str) -> dict:
    """What the platform OPS firehose sees of a record-scoped frame: the full
    frame, annotated like every other mirrored frame (owner 2026-10-14: the
    ops console keeps its full copy; the firehose is platform-admin only)."""
    return {**payload, "source_tenant": tenant_id}


async def _send_users(tenant_id: str, user_ids: list[str], payload: dict) -> int:
    """Send one frame to the live sockets of these agents of one tenant.

    Agent ids compare case-insensitively (the token carries the canonical
    users.agent_id, but a legacy/dev identity may differ in case), and a
    socket registered without a user is never a recipient."""
    wanted = {u.lower() for u in user_ids if u}
    if not wanted:
        return 0
    targets: list[tuple[str, WebSocket]] = [
        (user, ws)
        for (tenant, user), socks in list(_user_sockets.items())
        if tenant == tenant_id and user.lower() in wanted
        for ws in list(socks)
    ]
    if not targets:
        return 0
    text = json.dumps(payload)
    delivered = 0
    for user, ws in targets:
        try:
            await ws.send_text(text)
            delivered += 1
        except Exception:  # noqa: BLE001
            unregister(tenant_id, ws, user)
    return delivered


async def _deliver_scoped_local(
    group: str, user_ids: Optional[list[str]], payload: dict, *, mirror: bool
) -> int:
    """This replica's half of a scoped frame: the named agents of `group`
    (or every socket of `group` when user_ids is None — a group admitted per
    record, such as a voice session), plus the firehose copy when mirrored."""
    if user_ids is None:
        delivered = await _send_group(group, payload)
    else:
        delivered = await _send_users(group, user_ids, payload)
    if mirror and group != FIREHOSE_TENANT_ID and FIREHOSE_TENANT_ID in _sockets:
        await _send_group(FIREHOSE_TENANT_ID, firehose_copy(payload, group))
    return delivered


async def _publish_scoped(
    group: str, user_ids: Optional[list[str]], payload: dict, *, mirror: bool
) -> None:
    if _pool is None or not _listener_started:
        return
    envelope = json.dumps(
        {
            "origin": _instance_id,
            "tenant_id": group,
            "user_ids": user_ids,
            "mirror": mirror,
            "scoped_payload": payload,
        },
        separators=(",", ":"),
        default=str,
    )
    if len(envelope.encode("utf-8")) > _MAX_NOTIFY_BYTES:
        logger.error(
            "Scoped WebSocket event too large for cross-replica delivery (group=%s bytes=%d)",
            group.split(":", 1)[0],
            len(envelope.encode("utf-8")),
        )
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, envelope)
    except Exception as exc:  # noqa: BLE001 - local delivery remains available
        logger.warning("WebSocket cross-replica publish failed: %s", exc)


async def _publish(tenant_id: str, payload: dict, user_id: Optional[str] = None) -> None:
    # Publishing does not need this replica's own listener — other replicas
    # still deserve the frame while ours is reconnecting.
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
    scoped = None
    try:
        envelope = json.loads(raw)
        if envelope.get("origin") == _instance_id:
            return
        if "scoped_payload" in envelope:
            # v2 (record-scoped) envelope. It carries no "payload" key on
            # purpose: a replica still running the previous release reads it
            # as malformed and drops it, rather than delivering a scoped frame
            # to the whole brokerage during a rolling deploy.
            scoped = _parse_scoped(envelope)
        else:
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
    if scoped is not None:
        group, user_ids, payload, mirror = scoped
        await _deliver_scoped_local(group, user_ids, payload, mirror=mirror)
    elif user_id:
        await _deliver_user_local(tenant_id, user_id, payload)
    else:
        await _deliver_local(tenant_id, payload)


def _parse_scoped(envelope: dict) -> tuple[str, Optional[list[str]], dict, bool]:
    group = str(envelope["tenant_id"])
    user_ids = envelope.get("user_ids")
    if user_ids is not None and (
        not isinstance(user_ids, list) or not all(isinstance(u, str) for u in user_ids)
    ):
        raise TypeError("user_ids is not a list of strings")
    payload = envelope["scoped_payload"]
    if not isinstance(payload, dict):
        raise TypeError("scoped_payload is not an object")
    return group, user_ids, payload, bool(envelope.get("mirror"))


def _notification_callback(
    _connection: Any,
    _pid: int,
    _channel: str,
    payload: str,
) -> None:
    asyncio.create_task(_receive_notification(payload))


def _on_termination(_connection: Any) -> None:
    """asyncpg calls this when the listener's socket closes."""
    global _listener_healthy
    if _listener_healthy:
        logger.warning("WebSocket cross-replica listener connection lost; reconnecting")
    _mark_down("connection terminated")


def _mark_down(reason: str) -> None:
    global _listener_healthy
    _listener_healthy = False
    _listener_stats["last_error"] = reason
    if _listener_stats["down_since"] is None:
        _listener_stats["down_since"] = time.time()


async def _attach(pool: Any) -> bool:
    """Acquire a fresh connection and LISTEN on it. True on success."""
    global _listener_connection, _listener_healthy
    old = _listener_connection
    _listener_connection = None
    if old is not None:
        try:
            await asyncio.wait_for(pool.release(old), timeout=5)
        except Exception:  # noqa: BLE001 - a dead socket may not release cleanly
            try:
                old.terminate()
            except Exception:  # noqa: BLE001
                pass
    connection = None
    try:
        connection = await asyncio.wait_for(pool.acquire(), timeout=10)
        await connection.add_listener(_CHANNEL, _notification_callback)
        connection.add_termination_listener(_on_termination)
    except Exception as exc:  # noqa: BLE001 - retried by the supervisor
        _mark_down(type(exc).__name__)
        if connection is not None:
            try:
                await pool.release(connection)
            except Exception:  # noqa: BLE001
                pass
        return False
    _listener_connection = connection
    _listener_healthy = True
    _listener_stats["last_ok"] = time.time()
    _listener_stats["down_since"] = None
    _listener_stats["last_error"] = None
    return True


async def _probe() -> bool:
    connection = _listener_connection
    if connection is None or connection.is_closed():
        return False
    try:
        await asyncio.wait_for(connection.fetchval("SELECT 1"), timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _supervise() -> None:
    """Keep exactly one healthy LISTEN connection for the life of the process."""
    delay = 1.0
    while _listener_started and _pool is not None:
        if _listener_healthy and await _probe():
            delay = 1.0
            await asyncio.sleep(_PROBE_SECONDS)
            continue
        if _listener_healthy:
            logger.warning("WebSocket listener probe failed; reconnecting")
            _mark_down("probe failed")
        # Jittered so every replica does not reconnect on the same second
        # when the database comes back.
        await asyncio.sleep(delay * random.uniform(0.5, 1.5))
        if not _listener_started or _pool is None:
            return
        if await _attach(_pool):
            _listener_stats["reconnects"] += 1
            logger.info("WebSocket cross-replica listener reconnected (instance=%s)", _instance_id)
            delay = 1.0
        else:
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)


def listener_status() -> dict:
    """Component state for health reporting (no internals beyond counts)."""
    if not _listener_started:
        state = "UNAVAILABLE"
    elif _listener_healthy:
        state = "HEALTHY"
    else:
        state = "RECOVERING"
    down = _listener_stats["down_since"]
    return {"state": state, "reconnects": _listener_stats["reconnects"],
            "down_seconds": round(time.time() - down, 1) if down else 0}


async def start(pool: Optional[Any] = None) -> bool:
    """Start the dedicated PostgreSQL listener once per backend process."""
    global _pool, _listener_started, _supervisor_task
    if _listener_started:
        return True
    if pool is None:
        from db.connection import get_pool

        pool = get_pool()
    if pool is None:
        logger.warning("WebSocket hub running local-only because the DB pool is unavailable.")
        return False
    if not await _attach(pool):
        logger.warning("WebSocket PostgreSQL listener failed to start: %s", _listener_stats["last_error"])
        return False
    _pool = pool
    _listener_started = True
    _supervisor_task = asyncio.create_task(_supervise(), name="ws-hub-listener-supervisor")
    logger.info("WebSocket cross-replica listener online (instance=%s)", _instance_id)
    return True


async def stop() -> None:
    """Release the dedicated listener connection during application shutdown."""
    global _pool, _listener_connection, _listener_started, _listener_healthy, _supervisor_task
    task = _supervisor_task
    _supervisor_task = None
    connection = _listener_connection
    pool = _pool
    _listener_connection = None
    _listener_started = False
    _listener_healthy = False
    _pool = None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if connection is None or pool is None:
        return
    try:
        connection.remove_termination_listener(_on_termination)
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


async def broadcast_users(
    tenant_id: str, user_ids: list[str], payload: dict, *, mirror: bool = True
) -> int:
    """Deliver one frame, across replicas, to these agents of one tenant only
    (one NOTIFY for all of them). The platform firehose gets a full copy
    unless mirror=False (private content, like broadcast_user). An empty list
    reaches nobody."""
    recipients = sorted({u for u in user_ids if u}, key=str.lower)
    if not recipients:
        return 0
    delivered = await _deliver_scoped_local(tenant_id, recipients, payload, mirror=mirror)
    await _publish_scoped(tenant_id, recipients, payload, mirror=mirror)
    return delivered


async def broadcast_group(group: str, payload: dict) -> int:
    """Deliver to every socket of a group whose members were admitted per
    record (the voice-session stream, server.voice_telemetry_websocket), never
    mirrored to the firehose: the same frame already reaches it once through
    broadcast_record, and `broadcast()` would also fan it out tenant-wide."""
    delivered = await _deliver_scoped_local(group, None, payload, mirror=False)
    await _publish_scoped(group, None, payload, mirror=False)
    return delivered


async def broadcast_record(
    tenant_id: str,
    payload: dict,
    *,
    client_id: Any = None,
    contact_id: Any = None,
    actor: Optional[str] = None,
    only: Optional[list[str]] = None,
    mirror: bool = True,
) -> int:
    """Deliver a frame that carries a client's or contact's content (a call,
    a text, a note) only to the members who may read that record (CONTRACT
    C3.6; app_record_audience, 0145): broker owners, the assignee and share
    holders — or everyone when the record is in the brokerage pool or the
    frame links no client or contact (as `broadcast()` did).

    The audience is computed ONCE per frame (one query), then delivered to
    those agents' sockets on every replica. `only` narrows it further (the
    call-notes frame goes to the requester and the assignee, among those who
    may still read the client). If the audience cannot be computed, the frame
    goes only to `actor` — the user whose request produced it, who has just
    been authorized for the record — and otherwise to nobody (fail closed).
    The platform firehose gets a full copy unless mirror=False."""
    import record_access

    try:
        audience = await record_access.linked_audience(tenant_id, client_id, contact_id)
    except Exception as exc:  # noqa: BLE001 — never widen on failure
        logger.warning("Record audience unavailable (%s); frame %s sent to the actor only",
                       type(exc).__name__, payload.get("type"))
        audience = [actor] if actor else []
    if audience is None:
        if only is None and mirror:
            return await broadcast(tenant_id, payload)
        recipients = list(only) if only is not None else None
    elif only is None:
        recipients = audience
    else:
        allowed = {u.lower() for u in audience}
        recipients = [u for u in only if u and u.lower() in allowed]
    if recipients is None:  # the pool, unmirrored
        delivered = await _deliver_scoped_local(tenant_id, None, payload, mirror=False)
        await _publish_scoped(tenant_id, None, payload, mirror=False)
        return delivered
    return await broadcast_users(tenant_id, recipients, payload, mirror=mirror)
