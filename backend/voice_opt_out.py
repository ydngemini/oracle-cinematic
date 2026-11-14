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
import logging
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


async def _honour(state: Optional[Mapping[str, Any]], call_ref: str) -> None:
    try:
        await record_outbound_opt_out(state or {})
        logger.info("Outbound callee opted out; number suppressed: call=%s", call_ref)
    except OptOutNotRecorded:
        logger.exception("Outbound callee opted out but the suppression was NOT recorded: call=%s", call_ref)


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
