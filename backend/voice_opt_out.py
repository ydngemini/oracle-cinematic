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
import fcntl
import json
import logging
import os
import tempfile
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
#: LIMITS (stated honestly; a shared durable store is an owner decision):
#:   * the spool is per CONTAINER: in production the block covers sends made
#:     from the same api container only — the other api replica and the worker
#:     container (missions, automations) do not see a pending STOP until the
#:     write lands; health is per replica too;
#:   * an App Platform redeploy wipes the container disk, losing a STOP that
#:     is still pending at that moment (the health component pages first).
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
    container) and not
    yet written: the outreach gate treats it as suppressed (fail closed)."""
    key = (str(tenant_id), contact)
    return key in _PENDING or key in _spooled()


def pending_count() -> int:
    return len(_spooled() | _PENDING)


async def replay_spool() -> dict[str, int]:
    """Write every spooled opt-out; remove only the ones written. Called by
    reconciliation.run_sweep. Idempotent (ON CONFLICT DO NOTHING). A STOP
    spooled while this runs is never dropped."""
    queued = sorted(_spooled() | _PENDING)
    written = set()
    for tenant_id, contact in queued:
        try:
            await record_outbound_opt_out({"tenant_id": tenant_id, "callee": contact})
        except OptOutNotRecorded:
            continue
        written.add((tenant_id, contact))
    try:
        _remove(written)
    except OSError:
        logger.exception("Replayed spoken opt-outs but the spool could not be updated")
    return {"replayed": len(written), "still_pending": len(queued) - len(written)}


#: After the fast retries, the OWNING process keeps trying with capped
#: backoff. In production the media websockets (and so honour()) run in the
#: api service while reconciliation runs in a separate worker container with
#: its own disk, so the sweep cannot replay this process's spool; this loop
#: is what gets a pending STOP written.
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
            if not delays and slow == SLOW_RETRY_CAP:
                logger.error("Outbound opt-out still not recorded; retrying every %.0fs: call=%s",
                             SLOW_RETRY_CAP, call_ref)
            continue
        try:
            _remove({key})
        except OSError:
            logger.exception("Spoken opt-out recorded but the spool could not be updated: call=%s", call_ref)
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
    spool at once (safe even if this process dies), block in memory, and
    retry in the background after the call has ended."""
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
