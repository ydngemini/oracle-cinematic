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
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
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
# A frame larger than one NOTIFY (a 4,000-character transcript chunk of
# Chinese or emoji, audit 27.6) crosses replicas in parts sent in one
# transaction, so they arrive together and in order. 1,500 characters stay
# under the limit even at 4 UTF-8 bytes each.
_PART_CHARS = 1_500
_MAX_PARTS = 32
_PART_TTL_SECONDS = 30.0
_MAX_PENDING_PARTS = 64
_pending_parts: dict[str, dict] = {}
# One stalled browser must not hold a broadcast up for everyone else (audit
# 27.7): sends run concurrently, each bounded; a socket that cannot take a
# frame in time is closed and dropped.
SEND_TIMEOUT_SECONDS = float(os.getenv("ORACLE_WS_SEND_TIMEOUT_SECONDS", "5"))
_send_stats = {"timeouts": 0, "failed": 0, "parts_sent": 0, "parts_dropped": 0}
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

# The platform tenant's sockets double as an all-tenant firehose: frames
# broadcast to any tenant are mirrored there (annotated with source_tenant) so
# the platform-admin OPS console watches live traffic across the whole fleet.
# Membership in this group is JWT-gated — server._resolve_websocket_identity only returns
# the platform tenant for a verified platform_admin token, never for the
# spoofable tenant_id query param.
FIREHOSE_TENANT_ID = os.getenv(
    "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)

# Call content never reaches the firehose (external audit 27.5, owner decision
# 2026-11-20). A frame sent as sensitive (broadcast_record(sensitive=True)) —
# and any frame whose type is call content, whoever sent it and on whichever
# path — is mirrored as operational metadata only (firehose_metadata): which
# event, which brokerage, when, and state/error flags. Never the transcript or
# dialogue, offer or price amounts, guidance, names, phone numbers, emails,
# addresses or record ids. Other frames keep their full copy (owner 2026-10-14).
CALL_CONTENT_TYPE_PREFIXES = ("VOICE_", "NEGOTIATION_", "CALL_")
FIREHOSE_METADATA_TYPE = "FIREHOSE_METADATA"
_METADATA_FLAGS = ("consent_recorded", "requires_agent_approval")
_METADATA_TIMES = ("created_at", "started_at")
_TRANSCRIPT_STATES = frozenset({"pending", "active", "complete", "failed", "deleted"})
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

# Groups admitted per record (the voice-session stream, voice_intel's
# voice_session_group). Their sockets are registered with their member and a
# frame reaches only the members named for it at push time (broadcast_record
# `stream=`): never the whole group, so a member who lost access since the
# socket's last re-check gets nothing more (ledger L03-17 blocker, owner
# decision 2026-11-20).
RECORD_STREAM_PREFIXES = ("voice:",)


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


_close_tasks: set = set()


async def _close_quietly(ws: WebSocket) -> None:
    # Not time-capped here: for a socket whose send buffer is full, the
    # websockets library's own close_timeout (10 s) ends in fail_connection(),
    # which drops the TCP connection and lets the /ws handler finish. Cutting
    # it short left the connection half-closed (27.7 review).
    try:
        await ws.close(code=1013)
    except Exception:  # noqa: BLE001 - it is already being dropped
        pass


def _schedule_close(ws: WebSocket) -> None:
    task = asyncio.create_task(_close_quietly(ws))
    _close_tasks.add(task)
    task.add_done_callback(_close_tasks.discard)


async def _send_one(ws: WebSocket, text: str) -> bool:
    try:
        await asyncio.wait_for(ws.send_text(text), timeout=SEND_TIMEOUT_SECONDS)
        return True
    except asyncio.TimeoutError:
        _send_stats["timeouts"] += 1
        logger.warning("WebSocket send timed out after %.1fs; dropping the slow socket", SEND_TIMEOUT_SECONDS)
        _schedule_close(ws)
        return False
    except Exception:  # noqa: BLE001 - socket died between snapshot and send
        _send_stats["failed"] += 1
        return False


async def _fan_out(targets: list, text: str, drop) -> int:
    """Send `text` to every socket at once; `drop(target)` unregisters a
    socket that failed or timed out. Order within one socket is unchanged:
    each socket receives this frame exactly once, in this call."""
    if not targets:
        return 0
    results = await asyncio.gather(*(_send_one(_socket_of(t), text) for t in targets))
    delivered = 0
    for target, ok in zip(targets, results, strict=True):
        if ok:
            delivered += 1
        else:
            drop(target)
    return delivered


def _socket_of(target) -> WebSocket:
    return target[1] if isinstance(target, tuple) else target


def send_stats() -> dict:
    """Timeouts, failed sends and multi-part frames on this replica."""
    return dict(_send_stats)


async def _send_group(tenant_id: str, payload: dict) -> int:
    """Send one frame to every live socket in one tenant group.

    Dead sockets are dropped from the registry rather than raising — the WS
    handler's own finally block is the authoritative unregister path; this is
    just fast-path cleanup. Returns the number of sockets actually reached.
    """
    targets = list(_sockets.get(tenant_id, ()))
    if not targets:
        return 0

    return await _fan_out(targets, json.dumps(payload), lambda ws: unregister(tenant_id, ws))


async def _send_user(tenant_id: str, user_id: str, payload: dict) -> int:
    """Send a private frame only to one authenticated agent's live sockets."""
    key = (tenant_id, user_id)
    targets = list(_user_sockets.get(key, ()))
    if not targets:
        return 0
    return await _fan_out(targets, json.dumps(payload), lambda ws: unregister(tenant_id, ws, user_id))


async def _deliver_local(tenant_id: str, payload: dict) -> int:
    """Deliver to sockets owned by this process, including the admin firehose
    (call content as metadata only, firehose_copy)."""
    delivered = await _send_group(tenant_id, payload)
    if tenant_id != FIREHOSE_TENANT_ID and FIREHOSE_TENANT_ID in _sockets:
        delivered += await _send_group(FIREHOSE_TENANT_ID, firehose_copy(payload, tenant_id))
    return delivered


async def _deliver_user_local(tenant_id: str, user_id: str, payload: dict) -> int:
    # Private assistant content is intentionally never mirrored to the platform
    # firehose. Platform operators can inspect health metrics, not conversations.
    return await _send_user(tenant_id, user_id, payload)


def is_call_content(payload: dict) -> bool:
    """Whether a frame's type is live-call content (transcript, offer, MAO,
    consent, voice note), by type alone, so no sender can forget it."""
    frame_type = payload.get("type")
    return isinstance(frame_type, str) and frame_type.startswith(CALL_CONTENT_TYPE_PREFIXES)


def firehose_metadata(payload: dict, tenant_id: str) -> dict:
    """The platform OPS firehose's view of a sensitive frame (audit 27.5,
    owner decision 2026-11-20): operational metadata only, built from an
    allowlist and type-checked value by value, so content put in an allowed
    key (a sentence in a flag, a name in a timestamp) still never passes.
    Its own type, so no dashboard mistakes it for the content frame."""
    frame_type = payload.get("type")
    meta: dict[str, Any] = {
        "type": FIREHOSE_METADATA_TYPE,
        "version": 1,
        "frame_type": frame_type if isinstance(frame_type, str) and _CODE.fullmatch(frame_type) else None,
        "source_tenant": tenant_id,
        "content_withheld": True,
        "mirrored_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in _METADATA_FLAGS:
        if isinstance(payload.get(key), bool):
            meta[key] = payload[key]
    for key in _METADATA_TIMES:
        value = payload.get(key)
        if isinstance(value, str) and _ISO_TIME.fullmatch(value):
            meta[key] = value
    state = payload.get("transcript_status")
    if isinstance(state, str) and state in _TRANSCRIPT_STATES:
        meta["transcript_status"] = state
    code = payload.get("extraction_code")
    if isinstance(code, str) and _CODE.fullmatch(code):
        meta["extraction_code"] = code  # an AI-budget error code, not content
    if payload.get("mao_unavailable_reason"):
        meta["mao_unavailable"] = True  # the flag, never the reason text
    return meta


def firehose_copy(payload: dict, tenant_id: str, *, sensitive: bool = False) -> dict:
    """What the platform OPS firehose sees of a mirrored frame: the full
    frame annotated with source_tenant (owner 2026-10-14), except that a
    sensitive frame or any call-content frame is metadata only (audit 27.5,
    owner decision 2026-11-20)."""
    if sensitive or is_call_content(payload):
        return firehose_metadata(payload, tenant_id)
    return {**payload, "source_tenant": tenant_id}


def is_record_stream(group: str) -> bool:
    return group.startswith(RECORD_STREAM_PREFIXES)


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
    return await _fan_out(targets, json.dumps(payload), lambda t: unregister(tenant_id, t[1], t[0]))


async def _deliver_scoped_local(
    group: str, user_ids: Optional[list[str]], payload: dict, *, mirror: bool,
    sensitive: bool = False,
) -> int:
    """This replica's half of a scoped frame: the named agents of `group`
    (or every socket of `group` when user_ids is None — the brokerage pool),
    plus the firehose copy when mirrored (metadata only when `sensitive`).
    A per-record stream group is only ever delivered to named members: a
    whole-group frame for one (an older replica's envelope) reaches nobody."""
    if user_ids is None:
        if is_record_stream(group):
            logger.warning("Refusing a whole-group frame %s for a per-record stream", payload.get("type"))
            return 0
        delivered = await _send_group(group, payload)
    else:
        delivered = await _send_users(group, user_ids, payload)
    if mirror and group != FIREHOSE_TENANT_ID and FIREHOSE_TENANT_ID in _sockets:
        await _send_group(FIREHOSE_TENANT_ID, firehose_copy(payload, group, sensitive=sensitive))
    return delivered


async def _publish_scoped(
    group: str, user_ids: Optional[list[str]], payload: dict, *, mirror: bool,
    sensitive: bool = False,
) -> None:
    if _pool is None or not _listener_started:
        return
    envelope: dict[str, Any] = {
        "origin": _instance_id,
        "tenant_id": group,
        "user_ids": user_ids,
        # A replica on the previous release reads only "mirror": a sensitive
        # frame says False there, so during a rolling deploy it is not
        # mirrored at all rather than mirrored in full.
        "mirror": mirror and not sensitive,
        "scoped_payload": payload,
    }
    if mirror and sensitive:
        envelope["mirror_metadata"] = True
    await _notify(_dumps_envelope(envelope), group.split(":", 1)[0])


async def _publish(tenant_id: str, payload: dict, user_id: Optional[str] = None) -> None:
    # Publishing does not need this replica's own listener — other replicas
    # still deserve the frame while ours is reconnecting.
    if _pool is None or not _listener_started:
        return
    envelope = _dumps_envelope(
        {
            "origin": _instance_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "payload": payload,
        }
    )
    await _notify(envelope, tenant_id)


def _dumps_envelope(obj: dict) -> str:
    """UTF-8 JSON (about half the size of ASCII escapes for non-ASCII text),
    falling back to ASCII escapes for text that is not valid UTF-8, such as a
    lone surrogate (27.7 review): a broadcast must never raise for it."""
    text = json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, separators=(",", ":"), default=str)
    return text


def _split_envelope(envelope: str) -> Optional[list[str]]:
    """One NOTIFY payload per part, or None when even parts cannot carry it."""
    pieces = [envelope[i:i + _PART_CHARS] for i in range(0, len(envelope), _PART_CHARS)]
    if len(pieces) > _MAX_PARTS:
        return None
    part_id = uuid.uuid4().hex
    return [json.dumps({"origin": _instance_id, "part": part_id, "i": i, "n": len(pieces), "d": piece},
                       separators=(",", ":"), ensure_ascii=False)
            for i, piece in enumerate(pieces)]


async def _notify(envelope: str, label: str) -> None:
    """NOTIFY one envelope, in parts (one transaction) when it is too big."""
    if len(envelope.encode("utf-8")) <= _MAX_NOTIFY_BYTES:
        parts = [envelope]
    else:
        parts = _split_envelope(envelope)
        if parts is None:
            logger.error("WebSocket event too large for cross-replica delivery even in parts (group=%s bytes=%d)",
                         label, len(envelope.encode("utf-8")))
            return
    try:
        async with _pool.acquire() as conn:
            if len(parts) == 1:
                await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, parts[0])
            else:
                async with conn.transaction():
                    for part in parts:
                        await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, part)
                _send_stats["parts_sent"] += len(parts)
    except Exception as exc:  # noqa: BLE001 - local delivery remains available
        logger.warning("WebSocket cross-replica publish failed: %s", exc)


def _take_part(envelope: dict) -> Optional[str]:
    """Collect one part; the whole envelope once every part has arrived.
    Incomplete frames expire after _PART_TTL_SECONDS; at most
    _MAX_PENDING_PARTS frames wait at once."""
    part_id, i, n, data = envelope["part"], envelope["i"], envelope["n"], envelope["d"]
    if (not isinstance(part_id, str) or not isinstance(data, str) or not isinstance(i, int)
            or not isinstance(n, int) or isinstance(i, bool) or not 0 <= i < n <= _MAX_PARTS):
        raise TypeError("malformed part")
    now = time.monotonic()
    for stale in [k for k, v in _pending_parts.items() if now - v["at"] > _PART_TTL_SECONDS]:
        _pending_parts.pop(stale, None)
        _send_stats["parts_dropped"] += 1
    entry = _pending_parts.get(part_id)
    if entry is None:
        if len(_pending_parts) >= _MAX_PENDING_PARTS:
            _pending_parts.pop(next(iter(_pending_parts)), None)
            _send_stats["parts_dropped"] += 1
        entry = _pending_parts[part_id] = {"n": n, "got": {}, "at": now}
    if entry["n"] != n:
        raise TypeError("part count changed")
    entry["got"][i] = data
    if len(entry["got"]) < n:
        return None
    _pending_parts.pop(part_id, None)
    return "".join(entry["got"][k] for k in range(n))


async def _receive_notification(raw: str) -> None:
    scoped = None
    try:
        envelope = json.loads(raw)
        if envelope.get("origin") == _instance_id:
            return
        if "part" in envelope:
            # One part of a frame too large for one NOTIFY (27.6). It has no
            # tenant_id on purpose: a replica on the previous release drops it
            # as malformed, as it dropped the oversized frame before.
            whole = _take_part(envelope)
            if whole is not None:
                await _receive_notification(whole)
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
        group, user_ids, payload, mirror, sensitive = scoped
        await _deliver_scoped_local(group, user_ids, payload, mirror=mirror, sensitive=sensitive)
    elif user_id:
        await _deliver_user_local(tenant_id, user_id, payload)
    else:
        await _deliver_local(tenant_id, payload)


def _parse_scoped(envelope: dict) -> tuple[str, Optional[list[str]], dict, bool, bool]:
    group = str(envelope["tenant_id"])
    user_ids = envelope.get("user_ids")
    if user_ids is not None and (
        not isinstance(user_ids, list) or not all(isinstance(u, str) for u in user_ids)
    ):
        raise TypeError("user_ids is not a list of strings")
    payload = envelope["scoped_payload"]
    if not isinstance(payload, dict):
        raise TypeError("scoped_payload is not an object")
    metadata_only = bool(envelope.get("mirror_metadata"))
    return group, user_ids, payload, bool(envelope.get("mirror")) or metadata_only, metadata_only


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
    tenant_id: str, user_ids: list[str], payload: dict, *, mirror: bool = True,
    sensitive: bool = False,
) -> int:
    """Deliver one frame, across replicas, to these agents of one tenant only
    (one NOTIFY for all of them). The platform firehose gets a full copy
    unless mirror=False (private content, like broadcast_user), and only
    operational metadata when `sensitive` or the frame is call content
    (audit 27.5). An empty list reaches nobody."""
    recipients = sorted({u for u in user_ids if u}, key=str.lower)
    if not recipients:
        return 0
    delivered = await _deliver_scoped_local(tenant_id, recipients, payload, mirror=mirror,
                                            sensitive=sensitive)
    await _publish_scoped(tenant_id, recipients, payload, mirror=mirror, sensitive=sensitive)
    return delivered


async def _broadcast_stream(group: str, user_ids: list[str], payload: dict) -> int:
    """Deliver to the sockets of a per-record stream (the voice-session socket)
    that belong to these members only, across replicas. Never the whole group
    and never mirrored: the firehose already got its metadata through the
    dashboards' copy (broadcast_record)."""
    recipients = sorted({u for u in user_ids if u}, key=str.lower)
    if not recipients:
        return 0
    delivered = await _deliver_scoped_local(group, recipients, payload, mirror=False)
    await _publish_scoped(group, recipients, payload, mirror=False)
    return delivered


def _stream_members(audience: Optional[list[str]], *, computed: bool, actor: Optional[str],
                    creator: Optional[str], only: Optional[list[str]]) -> list[str]:
    """Who on a per-record stream receives this frame, decided NOW, at push time
    (ledger L03-17 blocker, owner decision 2026-11-20): the record's audience
    when it has one. With no specific audience (a pool client or lead, a lead
    whose client was erased, an unlinked call) the stricter of the dashboards'
    rule (the actor, broadcast_record) and the pull paths' rule (the call's
    creator, record_access.may_read_call_content): only a member who is both.
    An audience that could not be computed reaches nobody (fail closed)."""
    if not computed:
        return []
    if audience is None:
        if not actor or not creator or actor.lower() != creator.lower():
            return []
        members = [actor]
    else:
        members = list(audience)
    if only is not None:
        allowed = {u.lower() for u in only if u}
        members = [u for u in members if u and u.lower() in allowed]
    return members


async def broadcast_record(
    tenant_id: str,
    payload: dict,
    *,
    client_id: Any = None,
    contact_id: Any = None,
    lead_id: Any = None,
    actor: Optional[str] = None,
    only: Optional[list[str]] = None,
    mirror: bool = True,
    sensitive: bool = False,
    stream: Optional[str] = None,
    creator: Optional[str] = None,
) -> int:
    """Deliver a frame that carries a client's or contact's content (a call,
    a text, a note) only to the members who may read that record (CONTRACT
    C3.6; app_record_audience, 0145): broker owners, the assignee and share
    holders — or everyone when the record is in the brokerage pool or the
    frame links no client or contact (as `broadcast()` did). A frame about a
    lead (`lead_id`, with no client or contact) follows the client whose house
    it is (0143/0151); a harvested prospect's goes to everyone.

    The audience is computed ONCE per frame (one query), then delivered to
    those agents' sockets on every replica. `only` narrows it further (the
    call-notes frame goes to the requester and the assignee, among those who
    may still read the client). If the audience cannot be computed, the frame
    goes only to `actor` — the user whose request produced it, who has just
    been authorized for the record — and otherwise to nobody (fail closed).
    The platform firehose gets a full copy unless mirror=False.

    `sensitive=True` (live-call transcript, counter offer, MAO): such a frame
    is NEVER delivered to the whole brokerage. Where the audience is "every
    member" (a pool client or lead — including a lead whose private client
    was deleted or erased, ON DELETE SET NULL) it goes only to `actor`, the
    member handling the call (owner decision 2026-11-16, audit 27.1/27.4).
    The platform firehose gets operational metadata only, never the content
    (owner decision 2026-11-20, audit 27.5).

    `stream` (sensitive frames only): a per-record stream group (the call's
    voice-session socket) also receives the frame, on the sockets of the
    members who may read the call now (_stream_members) — the same audience,
    so a member who lost access since the socket's last re-check gets nothing
    more; `creator` is the call's live_call_sessions.created_by."""
    import record_access

    if stream is not None and (not sensitive or not is_record_stream(stream)):
        raise ValueError("a record stream carries call content: sensitive=True and a record-stream group")
    computed = True
    try:
        if lead_id is not None and client_id is None and contact_id is None:
            audience = await record_access.linked_audience(tenant_id, None, None, lead_id=lead_id)
        else:
            audience = await record_access.linked_audience(tenant_id, client_id, contact_id)
    except Exception as exc:  # noqa: BLE001 — never widen on failure
        logger.warning("Record audience unavailable (%s); frame %s sent to the actor only",
                       type(exc).__name__, payload.get("type"))
        audience = [actor] if actor else []
        computed = False
    delivered = await _deliver_record(tenant_id, payload, audience, actor=actor, only=only,
                                      mirror=mirror, sensitive=sensitive)
    if stream is not None:
        delivered += await _broadcast_stream(
            stream,
            _stream_members(audience, computed=computed, actor=actor, creator=creator, only=only),
            payload,
        )
    return delivered


async def _deliver_record(tenant_id: str, payload: dict, audience: Optional[list[str]], *,
                          actor: Optional[str], only: Optional[list[str]], mirror: bool,
                          sensitive: bool) -> int:
    """broadcast_record's dashboard delivery, once the audience is known."""
    if audience is None:
        if sensitive:
            # Never the whole brokerage: the member on the call, and only if an
            # `only` list (when given) names them too.
            caller = (actor or "").lower()
            recipients = [actor] if actor and (only is None or caller in {u.lower() for u in only if u}) else []
        elif only is None and mirror:
            return await broadcast(tenant_id, payload)
        else:
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
    return await broadcast_users(tenant_id, recipients, payload, mirror=mirror, sensitive=sensitive)
