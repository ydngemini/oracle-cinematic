"""Opt-out detection on a voice transcript, in both directions.

This matcher has two ways to be wrong and both are expensive:

  * miss a real opt-out — a TCPA violation, since inbound "stop" is valid by
    "any reasonable method" and nothing downstream re-asks;
  * fire on ordinary conversation — permanently marks a genuine inbound seller
    do-not-contact, discards the answers they just gave, and no path in
    inbound_voice ever clears the flag.

Scanning the joined transcript for bare keywords fails the second way; matching
only whole-utterance keywords fails the first. Clause scope is what separates
"I'm not interested, stop." from "stop by the house on Tuesday".
"""

from __future__ import annotations

import pytest

from inbound_voice import _requested_opt_out


def _caller(*turns: str):
    return [{"role": "caller", "text": t} for t in turns]


@pytest.mark.parametrize(
    "utterance",
    [
        "stop",
        "Stop.",
        "I'm not interested, stop.",
        "I said stop",
        "Please stop!",
        "No — stop.",
        "Yeah, cancel.",
        "unsubscribe",
        "opt-out",
        "Take me off your list.",
        "Do not call me again.",
        "Lose my number.",
        "Quit calling me.",
        "Never contact me again please.",
    ],
)
def test_real_opt_outs_are_honoured(utterance):
    assert _requested_opt_out(_caller(utterance)) is True, utterance


@pytest.mark.parametrize(
    "utterance",
    [
        # The exact false positives that motivated narrowing the matcher.
        "I need to sell before the end of the summer.",
        "I want to cancel my listing with my current agent.",
        # `stop` and `remove` as ordinary verbs with objects.
        "Can you stop by the house on Tuesday?",
        "We had to stop the renovation halfway through.",
        "They want us to remove the old shed before closing.",
        "The quit claim deed is with my attorney.",
        # Seller answering questions — the whole point of the call.
        "Three bedrooms, two baths, and the roof is about ten years old.",
        "My end goal is to close by September.",
    ],
)
def test_ordinary_conversation_is_not_an_opt_out(utterance):
    assert _requested_opt_out(_caller(utterance)) is False, utterance


def test_only_the_caller_side_counts():
    """The AI reads the disclosure script; it must not opt the caller out."""
    transcript = [
        {"role": "assistant", "text": "If you'd like me to stop, just say stop."},
        {"role": "caller", "text": "That's fine, go ahead."},
    ]
    assert _requested_opt_out(transcript) is False


def test_opt_out_later_in_the_call_still_counts():
    transcript = _caller(
        "Sure, it's a three bedroom on Maple.",
        "Actually, stop.",
    )
    assert _requested_opt_out(transcript) is True


def test_empty_transcript_is_not_an_opt_out():
    assert _requested_opt_out([]) is False


def test_a_hang_up_cannot_cancel_the_suppression_write(monkeypatch):
    """RUN-06: the bridge cancels the media task as the caller hangs up; the
    opt-out write it was awaiting used to be cancelled with it."""
    import asyncio

    import voice_opt_out

    written = []

    async def slow_record(state):
        await asyncio.sleep(0.05)
        written.append(state["callee"])

    monkeypatch.setattr(voice_opt_out, "record_outbound_opt_out", slow_record)

    async def scenario():
        call = asyncio.ensure_future(voice_opt_out.honour({"callee": "+13025550101"}, "call-1"))
        await asyncio.sleep(0.01)
        call.cancel()                      # the caller hung up mid-write
        try:
            await call
        except asyncio.CancelledError:
            pass
        assert call.cancelled(), "the cancellation still reaches the bridge"
        await asyncio.sleep(0.1)
        return written

    assert asyncio.run(scenario()) == ["+13025550101"]


# ── record_outbound_opt_out never passes a failed write off as recorded ─────

_TENANT = "11111111-1111-1111-1111-111111111111"


class _UnexpectedDriverError(Exception):
    """Not a database error the code names: what the catch-all is for."""


def _write_that_raises(monkeypatch, *, at):
    """record_outbound_opt_out's transaction, failing unexpectedly either on
    entering it ('enter': no connection to hand out) or in the INSERT."""
    from contextlib import asynccontextmanager

    import db.connection

    attempted: list[str] = []

    class _Conn:
        async def execute(self, sql, *args):
            attempted.append(sql)
            raise _UnexpectedDriverError("driver blew up")

    @asynccontextmanager
    async def tx(_ctx):
        if at == "enter":
            raise _UnexpectedDriverError("pool closed")
        yield _Conn()

    monkeypatch.setattr(db.connection, "tenant_tx", tx)
    return attempted


@pytest.mark.parametrize("at", ["enter", "insert"])
def test_an_unexpected_write_error_is_raised_as_not_recorded_never_swallowed(monkeypatch, at):
    """The catch-all in record_outbound_opt_out re-raises any failure as the
    one documented failure, OptOutNotRecorded, chained to the original: the
    caller (honour, the sweeps) then spools and retries the STOP. Returning
    normally would be a false "recorded" for a suppression never written."""
    import asyncio

    import voice_opt_out

    attempted = _write_that_raises(monkeypatch, at=at)
    with pytest.raises(voice_opt_out.OptOutNotRecorded, match="could not be written") as err:
        asyncio.run(voice_opt_out.record_outbound_opt_out({"tenant_id": _TENANT, "callee": "+13025550142"}))
    assert isinstance(err.value.__cause__, _UnexpectedDriverError)
    assert len(attempted) == (1 if at == "insert" else 0)


def test_an_unexpected_write_error_leaves_the_stop_pending(monkeypatch):
    """End to end through the spool replay: the STOP is not counted as
    replayed and stays pending, so the outreach gate keeps blocking it."""
    import asyncio

    import voice_opt_out
    from outreach_compliance import Channel, normalize_contact

    monkeypatch.delenv("REDIS_URL", raising=False)       # the local spool on its own
    number = normalize_contact("+13025550142", Channel.VOICE)
    voice_opt_out._PENDING.add((_TENANT, number))         # conftest gives each test its own
    _write_that_raises(monkeypatch, at="insert")
    assert asyncio.run(voice_opt_out.replay_spool()) == {"replayed": 0, "still_pending": 1}
    assert voice_opt_out.pending_opt_out(_TENANT, number)
