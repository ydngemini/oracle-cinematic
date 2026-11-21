"""A spoken "stop" on an OUTBOUND AI call is honoured (prompt 09, T09.15,
AC09.05, ledger L09-05).

Every outbound AI call opens with outreach_compliance.AI_VOICE_DISCLOSURE:
"... just say 'stop' and I'll end the call and won't contact you again." The
inbound intake path honoured that (inbound_voice.finalize_inbound_voice_call),
but the outbound bridges kept no transcript and had no opt-out handling at all
(L09-04), so the promise was not kept.

Outbound calls still keep no transcript. Each caller utterance is checked on
its own, in memory, as it is transcribed, with the same matcher the inbound
path uses (inbound_voice._requested_opt_out: an opt-out phrase, or a stop
keyword that is a whole clause — "I'm not interested, stop." yes, "stop by the
house on Tuesday" no; tests/test_voice_opt_out.py). On a match the callee's
number is suppressed brokerage-wide on every channel — the row the outreach
gate reads before any contact (outreach_compliance.gather_state) — and the
bridge ends the call. Only the number and a fixed source sentence are stored,
never what was said.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("oracle.voice_opt_out")

_ACTOR = "outbound-voice-opt-out"
#: Fixed text: what the row says about where the opt-out came from. Never the
#: callee's words.
SOURCE_TEXT = "outbound AI call: callee asked not to be contacted"


class OptOutNotRecorded(RuntimeError):
    """The callee opted out but the suppression could not be written."""


def spoken_opt_out(utterance: str) -> bool:
    """True when one caller utterance is a request not to be contacted."""
    from inbound_voice import _requested_opt_out

    return bool(utterance) and _requested_opt_out([{"role": "caller", "text": utterance}])


def is_outbound(state: Optional[Mapping[str, Any]]) -> bool:
    return bool(state) and state.get("direction") == "outbound"


async def record_outbound_opt_out(state: Mapping[str, Any]) -> None:
    """Suppress the callee's number for the call's brokerage, all channels,
    as 'stop_keyword' (0015: a consumer-initiated opt-out; a spoken "stop" is
    the same request by another reasonable method). Idempotent. Raises
    OptOutNotRecorded when it cannot: the bridge still ends the call, and the
    failure is logged loudly rather than passing as if it were kept."""
    from db.connection import tenant_tx
    from outreach_compliance import Channel, normalize_contact
    from tenancy import Role, TenantContext

    tenant_id = str(state.get("tenant_id") or "")
    contact = normalize_contact(str(state.get("callee") or ""), Channel.VOICE)
    if not tenant_id or not contact:
        raise OptOutNotRecorded("outbound opt-out: the call has no tenant or callee number")
    ctx = TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    try:
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """INSERT INTO outreach_suppression (tenant_id, contact, channel, reason, source_text)
                   VALUES ($1::uuid, $2, '*', 'stop_keyword', $3)
                   ON CONFLICT (tenant_id, contact, channel) WHERE lifted_at IS NULL DO NOTHING""",
                tenant_id, contact, SOURCE_TEXT,
            )
    except Exception as exc:  # noqa: BLE001 — re-raised as the one documented failure
        raise OptOutNotRecorded("outbound opt-out could not be written") from exc


#: Audit 23.5 / 34.5: a spoken STOP whose suppression write fails used to be
#: logged and forgotten. Now: one attempt in the call; on failure the tenant +
#: normalised number (never what was said) are spooled at once and held in
#: memory; the outreach gate treats a pending STOP as suppressed; the owning
#: process retries until the write succeeds (fast, then capped backoff) and
#: resumes a leftover spool at startup in every process role; reconciliation.run_sweep replays it
#: when it runs in the same container; component_health reports it.
#: The spool alone is per CONTAINER (re-review of PR #84), so a pending STOP is
#: ALSO mirrored into the shared Valkey store (owner instruction 2026-11-20;
#: see "The shared store" below): every api replica and the worker see it, and
#: the worker's sweep writes it even after the api container that heard it was
#: redeployed and its disk wiped.
RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)
#: Absolute and OUTSIDE the source tree by default (never committed, never in
#: the image); point ORACLE_OPT_OUT_SPOOL at a mounted volume where one exists.
SPOOL_PATH = Path(os.environ.get(
    "ORACLE_OPT_OUT_SPOOL", str(Path(tempfile.gettempdir()) / "neoh-outbound.opt-out-spool.jsonl")))
_PENDING: set[tuple[str, str]] = set()
#: (stat signature, keys) of the spool as last read; re-read when it changes,
#: so every worker process IN THE SAME CONTAINER sees a pending STOP.
_SPOOL_SEEN: tuple = (None, None)


def _key(state: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    from outreach_compliance import Channel, normalize_contact

    tenant_id = str(state.get("tenant_id") or "")
    contact = normalize_contact(str(state.get("callee") or ""), Channel.VOICE)
    return (tenant_id, contact) if tenant_id and contact else None


@contextmanager
def _spool_lock():
    """Cross-process lock: appends and rewrites never interleave, so one
    worker's rewrite can never drop another worker's concurrent append."""
    SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(SPOOL_PATH) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_spool() -> list[tuple[str, str]]:
    try:
        lines = SPOOL_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
            out.append((str(item["tenant_id"]), str(item["contact"])))
        except (ValueError, KeyError, TypeError):
            continue  # a torn line from a crash
    return out


def _spooled() -> set[tuple[str, str]]:
    global _SPOOL_SEEN
    try:
        info = SPOOL_PATH.stat()
        signature = (info.st_ino, info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        return set()
    if _SPOOL_SEEN[0] != signature:
        _SPOOL_SEEN = (signature, set(_read_spool()))
    return set(_SPOOL_SEEN[1])


def _spool(key: tuple[str, str]) -> None:
    """Append, fsynced, mode 0600, under the cross-process lock."""
    with _spool_lock():
        fd = os.open(SPOOL_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps({"tenant_id": key[0], "contact": key[1]}) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    _PENDING.add(key)


def _remove(keys: set[tuple[str, str]]) -> None:
    """Drop exactly `keys` (written at last) from the spool and memory. Reads
    the CURRENT file under the lock, so STOPs spooled meanwhile — by this
    process or another — are kept."""
    if not keys:
        return
    _PENDING.difference_update(keys)
    with _spool_lock():
        if not SPOOL_PATH.exists():
            return
        keep = [k for k in _read_spool() if k not in keys]
        tmp = SPOOL_PATH.with_name(SPOOL_PATH.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, "".join(json.dumps({"tenant_id": t, "contact": c}) + "\n" for t, c in keep).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, SPOOL_PATH)


def pending_opt_out(tenant_id: str, contact: str) -> bool:
    """True while this number's spoken STOP is spooled (by any process in this
    container) and not yet written. LOCAL only: send paths use
    opt_out_status(), which also reads the shared store."""
    key = (str(tenant_id), contact)
    return key in _PENDING or key in _spooled()


def pending_count() -> int:
    """Pending STOPs in THIS container's spool and memory."""
    return len(_spooled() | _PENDING)


# ── The shared store ─────────────────────────────────────────────────────────
#: Owner instruction 2026-11-20 ('continue all' on the open decisions; design
#: chosen by the Claude session). A pending STOP lives in BOTH places:
#:   * the local spool (above), as before;
#:   * Valkey, through the process's shared client (rate_limit_middleware:
#:     0.5 s connect / 1 s socket timeouts), every operation bounded by
#:     SHARED_TIMEOUT_SECONDS:
#:       key    voice:opt_out_pending:<tenant uuid>:<digest>
#:       digest privacy_lifecycle.contact_hmac — the per-tenant keyed hash the
#:              erasure tombstones use; never the number. With no master key
#:              configured (development only: production requires one) the
#:              digest is the normalised number, as the local spool holds it.
#:       value  a Fernet token (key derived from the master key, as
#:              acs_call_handler seals its Valkey state) over the tenant and
#:              number, so the worker can write the row; plain JSON without a
#:              master key, as the spool.
#:       index  the set voice:opt_out_pending:index of "<tenant>:<digest>", so
#:              the sweep lists entries without SCAN.
#:       TTL    SHARED_TTL_SECONDS (30 days), RE-SET by every retry or sweep
#:              that still cannot write the row (the owner retries at least
#:              every SLOW_RETRY_CAP seconds, the worker sweeps every 15 min):
#:              it lapses only for an entry nobody has retried for 30 days.
#: Order: spool, then Valkey, then honour() returns. An entry is removed from
#: Valkey only AFTER the suppression row's transaction has committed.
#: Readers (opt_out_status): this container's spool, then Valkey. REDIS_URL set
#: but Valkey unreadable -> UNKNOWN, never CLEAR: the send gate holds SMS and
#: voice (fail closed, as plivo_call_handler refuses a call it cannot track);
#: email never consulted pending STOPs and is unaffected. REDIS_URL unset ->
#: no shared store: this container's spool only, as before.
#: Valkey DOWN when a STOP is honoured: the spool still blocks in this
#: container and the owner's retries (and this container's sweep) re-write the
#: entry when Valkey answers. Valkey LOSING its data (a restart without
#: persistence): other replicas miss the STOP until the owner's next retry
#: re-writes it (at most SLOW_RETRY_CAP seconds); the owner's own gate never
#: does.
PENDING, CLEAR, UNKNOWN = "pending", "clear", "unknown"
SHARED_PREFIX = "voice:opt_out_pending:"
SHARED_INDEX = SHARED_PREFIX + "index"
SHARED_TTL_SECONDS = 30 * 24 * 3600
SHARED_TIMEOUT_SECONDS = 1.5
_SEAL_LABEL = b"oracle:voice-opt-out-pending:v1\x00"
_UNREADABLE_LOG_EVERY = 60.0
_last_unreadable_log = 0.0


class SharedStoreUnavailable(RuntimeError):
    """REDIS_URL is set but Valkey cannot be reached."""


def shared_store_configured() -> bool:
    return bool(os.environ.get("REDIS_URL", "").strip())


async def _shared_client() -> Any:
    from rate_limit_middleware import get_redis_client

    client = await get_redis_client()
    if client is None:
        raise SharedStoreUnavailable("Valkey is configured but unreachable")
    return client


def _master_key() -> str:
    return os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")


def _member(key: tuple[str, str]) -> str:
    tenant_id, contact = key
    if _master_key():
        from privacy_lifecycle import contact_hmac

        return f"{tenant_id}:{contact_hmac(tenant_id, contact)}"
    return f"{tenant_id}:{contact}"


def _cipher() -> Any:
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(_SEAL_LABEL + _master_key().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal(key: tuple[str, str]) -> str:
    if not _master_key():
        return json.dumps({"v": 1, "tenant_id": key[0], "contact": key[1]})
    body = json.dumps({"tenant_id": key[0], "contact": key[1]}, separators=(",", ":")).encode("utf-8")
    return json.dumps({"v": 1, "sealed": _cipher().encrypt(body).decode("ascii")})


def _unseal(raw: Any) -> Optional[tuple[str, str]]:
    from cryptography.fernet import InvalidToken

    try:
        item = json.loads(raw)
        if "sealed" in item:
            if not _master_key():
                return None
            item = json.loads(_cipher().decrypt(str(item["sealed"]).encode("ascii")))
        return str(item["tenant_id"]), str(item["contact"])
    except (ValueError, KeyError, TypeError, InvalidToken):
        return None


async def _bounded(coro) -> Any:
    return await asyncio.wait_for(coro, SHARED_TIMEOUT_SECONDS)


async def _mirror(key: tuple[str, str]) -> bool:
    """Write the shared entry, or re-assert it (re-setting its TTL). False
    when no shared store is configured. Raises when Valkey fails."""
    if not shared_store_configured():
        return False
    member = _member(key)

    async def write() -> None:
        client = await _shared_client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.set(SHARED_PREFIX + member, _seal(key), ex=SHARED_TTL_SECONDS)
            pipe.sadd(SHARED_INDEX, member)
            pipe.expire(SHARED_INDEX, SHARED_TTL_SECONDS)
            await pipe.execute()

    await _bounded(write())
    return True


async def _ensure_mirrored(key: tuple[str, str], call_ref: str) -> bool:
    """_mirror that never raises (a failure is retried on the next pass)."""
    try:
        return await _mirror(key)
    except Exception as exc:  # noqa: BLE001 — Valkey down: the spool still blocks here; retried
        logger.error("Pending spoken opt-out is NOT in the shared store (%s): other replicas cannot see "
                     "it until a retry re-writes it: call=%s", type(exc).__name__, call_ref)
        return False


async def _unmirror(keys: set[tuple[str, str]]) -> None:
    """Remove shared entries. Only ever called after the rows committed."""
    if not keys or not shared_store_configured():
        return
    members = sorted(_member(k) for k in keys)

    async def delete() -> None:
        client = await _shared_client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(*[SHARED_PREFIX + m for m in members])
            pipe.srem(SHARED_INDEX, *members)
            await pipe.execute()

    await _bounded(delete())


async def _written(keys: set[tuple[str, str]], call_ref: str) -> None:
    """The suppression rows for `keys` have COMMITTED: drop them from this
    container's spool and memory, then from the shared store. Never raises:
    an entry left behind only over-blocks a number that is already
    suppressed, and the next sweep or retry writes it (a no-op) and drops it."""
    if not keys:
        return
    try:
        _remove(keys)
    except OSError:
        logger.exception("Spoken opt-out recorded but the spool could not be updated: call=%s", call_ref)
    try:
        await _unmirror(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spoken opt-out recorded but its shared entry could not be removed yet (%s); "
                       "the next sweep removes it: call=%s", type(exc).__name__, call_ref)


async def opt_out_status(tenant_id: str, contact: str) -> str:
    """PENDING when this number's spoken STOP is waiting to be written — in
    this container's spool or in the shared store (any replica, the worker);
    CLEAR when neither holds it; UNKNOWN when the shared store is configured
    but cannot be read. Callers must never treat UNKNOWN as CLEAR."""
    global _last_unreadable_log
    if pending_opt_out(tenant_id, contact):
        return PENDING
    if not contact or not shared_store_configured():
        return CLEAR
    name = SHARED_PREFIX + _member((str(tenant_id), contact))

    async def read() -> Any:
        client = await _shared_client()
        return await client.exists(name)

    try:
        found = await _bounded(read())
    except Exception as exc:  # noqa: BLE001 — unreadable is UNKNOWN, never CLEAR
        now = time.monotonic()
        if now - _last_unreadable_log >= _UNREADABLE_LOG_EVERY:
            _last_unreadable_log = now
            logger.error("Shared spoken opt-out store unreadable (%s): the outreach gate holds SMS and "
                         "voice until it answers", type(exc).__name__)
        return UNKNOWN
    return PENDING if found else CLEAR


async def shared_summary() -> dict[str, Any]:
    """For health: the shared store's pending count (every replica's STOPs)
    and how many of THIS container's pending STOPs it is missing."""
    if not shared_store_configured():
        return {"configured": False}
    local = _spooled() | _PENDING

    async def read() -> tuple[int, int]:
        client = await _shared_client()
        pending = int(await client.scard(SHARED_INDEX) or 0)
        missing = 0
        for key in local:
            if not await client.exists(SHARED_PREFIX + _member(key)):
                missing += 1
        return pending, missing

    try:
        pending, missing = await _bounded(read())
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        return {"configured": True, "reachable": False, "reason": type(exc).__name__}
    return {"configured": True, "reachable": True, "pending": pending, "unmirrored": missing}


async def replay_spool() -> dict[str, int]:
    """Write every opt-out spooled in this container; remove only the ones
    written (spool, then shared store, after the commit). One still not
    written is re-written to the shared store (Valkey may have been down when
    it was spooled, or lost it since). Called by reconciliation.run_sweep.
    Idempotent (ON CONFLICT DO NOTHING). A STOP spooled while this runs is
    never dropped."""
    queued = sorted(_spooled() | _PENDING)
    written = set()
    for tenant_id, contact in queued:
        try:
            await record_outbound_opt_out({"tenant_id": tenant_id, "callee": contact})
        except OptOutNotRecorded:
            await _ensure_mirrored((tenant_id, contact), "sweep")
            continue
        written.add((tenant_id, contact))
    await _written(written, "sweep")
    return {"replayed": len(written), "still_pending": len(queued) - len(written)}


async def replay_shared(only_tenant: Optional[str] = None) -> dict[str, Any]:
    """The worker's sweep over the SHARED store: write every pending STOP —
    including one whose api container is gone — and remove each entry only
    after its row has committed. An entry it cannot write keeps (re-sets) its
    TTL; one it cannot read (another master key, a damaged value) is kept,
    never written as something else, and lapses at its TTL. Raises when the
    store cannot be read (the caller reports it)."""
    if not shared_store_configured():
        return {"configured": False}
    client = await _bounded(_shared_client())
    members = sorted(str(m) for m in await _bounded(client.smembers(SHARED_INDEX)))
    replayed = still = unreadable = gone = 0
    for member in members:
        if only_tenant and not member.startswith(f"{only_tenant}:"):
            continue
        raw = await _bounded(client.get(SHARED_PREFIX + member))
        if raw is None:  # written and removed meanwhile, or lapsed
            await _bounded(client.srem(SHARED_INDEX, member))
            gone += 1
            continue
        key = _unseal(raw)
        if key is None or _member(key) != member:
            unreadable += 1
            logger.error("A shared pending spoken opt-out cannot be read; kept (it still blocks)")
            continue
        try:
            await record_outbound_opt_out({"tenant_id": key[0], "callee": key[1]})
        except OptOutNotRecorded:
            await _bounded(client.expire(SHARED_PREFIX + member, SHARED_TTL_SECONDS))
            still += 1
            continue
        await _written({key}, "shared-sweep")
        replayed += 1
    return {"replayed": replayed, "still_pending": still, "unreadable": unreadable, "gone": gone}


#: After the fast retries, the OWNING process keeps trying with capped
#: backoff. In production the media websockets (and so honour()) run in the
#: api service while reconciliation runs in a separate worker container with
#: its own disk: the worker's sweep reaches a pending STOP only through the
#: shared store (replay_shared), so with Valkey down or not configured this
#: loop is what gets it written.
SLOW_RETRY_FIRST, SLOW_RETRY_CAP = 60.0, 300.0


async def _retry_later(key: tuple[str, str], call_ref: str) -> None:
    """After the call has ended: fast retries, then capped backoff until the
    write succeeds (or this process exits; resume_pending() restarts it)."""
    delays = list(RETRY_DELAYS)
    slow = SLOW_RETRY_FIRST
    while True:
        if delays:
            await asyncio.sleep(delays.pop(0))
        else:
            await asyncio.sleep(slow)
            slow = min(slow * 2, SLOW_RETRY_CAP)
        if key not in _PENDING and key not in _spooled():
            return  # written meanwhile (replay or another retry)
        try:
            await record_outbound_opt_out({"tenant_id": key[0], "callee": key[1]})
        except OptOutNotRecorded:
            # Re-assert the shared entry every pass: Valkey may have been down
            # when it was spooled, or lost its data since.
            await _ensure_mirrored(key, call_ref)
            if not delays and slow == SLOW_RETRY_CAP:
                logger.error("Outbound opt-out still not recorded; retrying every %.0fs: call=%s",
                             SLOW_RETRY_CAP, call_ref)
            continue
        await _written({key}, call_ref)
        logger.info("Spooled outbound opt-out recorded on retry: call=%s", call_ref)
        return


def resume_pending() -> int:
    """Called at startup in every process role: restart the retry loop for any
    STOP left in this container's spool by a previous process. Returns how
    many were resumed. Never raises: an unreadable spool must not stop boot
    (the health component reports it)."""
    try:
        keys = _spooled()
    except (OSError, ValueError) as exc:
        logger.error("Spoken opt-out spool unreadable at startup (%s); health will report it",
                     type(exc).__name__)
        return 0
    for key in keys:
        _PENDING.add(key)
        _background(_retry_later(key, "resumed-at-startup"))
    if keys:
        logger.error("Resumed %d spoken opt-out(s) still waiting to be recorded", len(keys))
    return len(keys)


async def _honour(state: Optional[Mapping[str, Any]], call_ref: str) -> None:
    """ONE attempt inside the call (the bridge hangs up right after, so a
    caller who said "stop" is never kept on the line by retries). On failure:
    spool at once (safe even if this process dies), block in memory, write
    the shared entry (every replica and the worker then see it; bounded by
    SHARED_TIMEOUT_SECONDS), and retry in the background after the call has
    ended."""
    state = state or {}
    try:
        await record_outbound_opt_out(state)
        logger.info("Outbound callee opted out; number suppressed: call=%s", call_ref)
        return
    except OptOutNotRecorded:
        logger.warning("Outbound opt-out write failed at call time: call=%s", call_ref)
    key = _key(state)
    if key is None:
        logger.error("Outbound callee opted out but the call has no tenant or number to spool: call=%s", call_ref)
        return
    try:
        _spool(key)
        logger.error("Outbound callee opted out; suppression NOT yet recorded — spooled, blocked by the "
                     "outreach gate meanwhile, retrying after the call: call=%s", call_ref)
    except OSError:
        _PENDING.add(key)  # at least this process keeps honouring it
        logger.exception("Outbound opt-out could not be written OR spooled (kept in memory only): call=%s",
                         call_ref)
    if await _ensure_mirrored(key, call_ref):
        logger.info("Pending outbound opt-out written to the shared store: call=%s", call_ref)
    _background(_retry_later(key, call_ref))


async def honour(state: Optional[Mapping[str, Any]], call_ref: str) -> None:
    """Record the opt-out for an outbound call; never raises. The caller (a
    bridge) ends the call right after, whether or not this succeeded.

    Shielded (RUN-06): the bridges cancel the losing media task the moment
    either side hangs up, and a caller who says "stop" and hangs up at once
    used to cancel the suppression write mid-flight. The write now runs to its
    end even when the call's task is cancelled; the cancellation still reaches
    the bridge."""
    await asyncio.shield(_background(_honour(state, call_ref)))


#: Shielded writes in flight, kept referenced so a cancelled caller cannot let
#: the event loop garbage-collect the task before it finishes.
_IN_FLIGHT: set = set()


def _background(coro) -> "asyncio.Task":
    task = asyncio.ensure_future(coro)
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


def is_inbound(state: Optional[Mapping[str, Any]]) -> bool:
    return bool(state) and state.get("direction") == "inbound"


async def honour_inbound(state: Optional[Mapping[str, Any]], call_ref: str) -> bool:
    """Record an INBOUND caller's spoken opt-out as soon as it is said
    (external audit RUN-02). Never raises and does not end the call; returns
    whether it was recorded. Finalize records it again at hang-up.

    Shielded (RUN-06): a hang-up that cancels the bridge's task does not
    cancel the write already under way."""
    import asyncio

    task = asyncio.ensure_future(_honour_inbound(state, call_ref))
    _INBOUND_IN_FLIGHT.add(task)
    task.add_done_callback(_INBOUND_IN_FLIGHT.discard)
    return await asyncio.shield(task)


#: Kept referenced so a cancelled caller cannot let the task be collected.
_INBOUND_IN_FLIGHT: set = set()


async def _honour_inbound(state: Optional[Mapping[str, Any]], call_ref: str) -> bool:
    from inbound_voice import InboundOptOutNotRecorded, record_inbound_opt_out

    try:
        recorded = await record_inbound_opt_out(call_ref, state)
    except InboundOptOutNotRecorded:
        logger.exception("Inbound caller opted out but the suppression was NOT recorded mid-call "
                         "(finalize will retry it): call=%s", call_ref)
        return False
    if recorded:
        logger.info("Inbound caller opted out; number suppressed mid-call: call=%s", call_ref)
    return recorded
