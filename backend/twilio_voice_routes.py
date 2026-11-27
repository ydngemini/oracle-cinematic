"""Twilio outbound voice webhooks: the TwiML answer, the TTS audio, the call
status callback and the speech turn (moved out of commands_api in external
audit 35-02, slice 4, verbatim; no behaviour change).

commands_api includes this module's router where the routes used to be
declared, so their paths (/api/commands/webhooks/twilio, .../tts/{token}.mp3,
.../status, .../speech), methods, dependencies and order among the HTTP routes
are unchanged. logger is still 'oracle.commands'.

Monkeypatching: the routes call their helpers (_spoken, _twilio_call_brokerage,
_record_twilio_call_on_command, _voice_refused_twiml, the signature check
_verify_outbound_twilio_signature and tenant_tx) through THIS module's
globals, so a test that replaces one of them must patch twilio_voice_routes.
commands_api does not re-export those names, so a stale patch on commands_api
raises AttributeError instead of silently missing the call. The one
commands_api name the routes need, accept_call_outcome (which the Plivo
callback shares), is reached through the forwarder below, which looks it up on
commands_api when called: a patch of commands_api.accept_call_outcome still
replaces what the status callback calls.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Mapping, Optional
from xml.sax.saxutils import escape as _xml_escape

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from call_platform_ctx import _call_platform_ctx
from db.connection import tenant_tx
from llm_gateway import SpendRefused
from twilio_webhook_auth import _twilio_correlation, _verify_outbound_twilio_signature

logger = logging.getLogger("oracle.commands")

# No prefix and no tags of its own: commands_api.router (prefix /api/commands,
# tags ["commands"]) includes it, and its prefix and tags apply as before.
router = APIRouter()


async def accept_call_outcome(*args: Any, **kwargs: Any) -> Any:
    """commands_api.accept_call_outcome, looked up when called (see the module
    docstring). Imported at call time: commands_api imports this module."""
    import commands_api

    return await commands_api.accept_call_outcome(*args, **kwargs)


# ── Twilio webhook (TwiML) ─────────────────────────────────────────────────────

@router.post("/webhooks/twilio", include_in_schema=False)
async def twilio_webhook(request: Request):
    """Return signed, state-bound TwiML for an approved outbound Twilio call."""
    from outreach_compliance import AI_VOICE_DISCLOSURE
    from twilio_call_handler import (
        TwilioCallStateUnavailable,
        create_twilio_bridge_token,
        create_twilio_media_stream_url,
        resolve_outbound_twilio_call_state,
        twilio_qwen_enabled,
    )

    form = await request.form()
    # Signature first, from state that exists before the call (or the
    # session row): only a verified request waits for the CallSid's state.
    await _verify_outbound_twilio_signature(request, form, "/api/commands/webhooks/twilio")
    call_sid = str(form.get("CallSid") or "")
    # By CallSid, else by the signed pre-submission correlation (24.2).
    state = await resolve_outbound_twilio_call_state(
        call_sid,
        _twilio_correlation(request),
        wait_for_initialization=True,
    )
    if state is not None and state.get("via") == "correlation":
        await _record_twilio_call_on_command(state, call_sid)
    if state is None:
        logger.error("Rejecting unmanaged Twilio call: sid=%s", call_sid)
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">This call cannot be connected safely. Goodbye.</Say>
    <Hangup/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    greeting = (
        AI_VOICE_DISCLOSURE
        + " I'm NEOH, your real estate AI assistant. How can I help you today?"
    )
    # The number Twilio dialled, which is what the TTS allowlist is keyed on —
    # not the caller's, so a test number cannot be spoofed into the feature by
    # whoever is calling.
    to_number = str(form.get("To") or "")
    greeting_speech = await _spoken(greeting, to_number=to_number)
    if twilio_qwen_enabled(state):
        # The call's own reference in the path: the handshake is resolved to
        # this call, and its account's tokens, before it is accepted (23.2).
        try:
            stream_url = await create_twilio_media_stream_url(call_sid)
            bridge_token = create_twilio_bridge_token(call_sid)
        except (ValueError, TwilioCallStateUnavailable):
            # Never Twilio's application error: the callee has heard the
            # disclosure, so say why the call ends, as the inbound path does.
            logger.exception("Twilio media binding unavailable; ending the call politely: sid=%s", call_sid)
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {greeting_speech}
    <Say voice="Polly.Joanna">The assistant is unavailable right now. Goodbye.</Say>
    <Hangup/>
</Response>"""
            return Response(content=twiml, media_type="application/xml")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {greeting_speech}
    <Connect>
        <Stream url="{_xml_escape(stream_url)}">
            <Parameter name="bridge_token" value="{_xml_escape(bridge_token)}"/>
        </Stream>
    </Connect>
    <Say voice="Polly.Joanna">The realtime assistant is unavailable. Goodbye.</Say>
</Response>"""
        logger.info("Twilio Qwen Media Stream authorized in TwiML: sid=%s", call_sid)
        return Response(content=twiml, media_type="application/xml")

    gather_attribs = (f'input="speech" action="{_twilio_speech_action_url(state)}" method="POST" '
                      'timeout="5" speechTimeout="auto"')

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {greeting_speech}
    <Gather {gather_attribs}>
        <Say voice="Polly.Joanna">I'm listening.</Say>
    </Gather>
    <Say voice="Polly.Joanna">I didn't catch that. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _twilio_speech_action_url(state: Mapping[str, Any]) -> str:
    """The <Gather> action URL, carrying the call's own reference (its
    state's correlation) so /speech can choose the call's signing account
    before anything else (23.2). Twilio signs it, query included."""
    from twilio_call_handler import TWILIO_CORRELATION_PARAM

    url = f"{os.getenv('ORACLE_PUBLIC_BASE_URL', '').rstrip('/')}/api/commands/webhooks/twilio/speech"
    correlation = str((state or {}).get("correlation") or "")
    if correlation:
        url = f"{url}?{TWILIO_CORRELATION_PARAM}={urllib.parse.quote(correlation, safe='')}"
    return _xml_escape(url)


async def _spoken(text: str, *, to_number: str) -> str:
    """One line of TwiML speech: ElevenLabs when permitted, Polly otherwise.

    Returns a `<Play>` element for a call on the TTS test allowlist and a
    `<Say>` for everything else — including every failure path inside
    voice_tts, which returns None rather than raising. The caller has to hear
    something: the first line of every call is the FCC-required AI disclosure,
    and silence there is both a broken call and a compliance failure.
    """
    import voice_tts

    url = await voice_tts.play_url(text, to_number=to_number)
    if url:
        return f'<Play>{_xml_escape(url)}</Play>'
    return f'<Say voice="Polly.Joanna">{_xml_escape(text)}</Say>'


@router.get("/webhooks/twilio/tts/{token}.mp3", include_in_schema=False)
async def twilio_tts_audio(token: str):
    """Serve a rendered line to Twilio.

    Unauthenticated because Twilio fetches it directly, which is safe here: the
    token is a SHA-256 prefix of the audio itself, so it is unguessable and
    reveals nothing but a line the caller is about to hear anyway. Entries
    expire minutes after the call leg that created them.
    """
    import voice_tts

    audio = await voice_tts.cache.get(token)
    if audio is None:
        raise HTTPException(status_code=404, detail="Audio expired.")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


#: Twilio call statuses that carry an outcome. initiated/ringing/queued say
#: nothing the session or the first response needs.
_TWILIO_OUTCOME_STATUSES = frozenset({"in-progress", "answered", "completed", "busy", "failed",
                                      "no-answer", "canceled"})
_TWILIO_TERMINAL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


async def _record_twilio_call_on_command(state: Mapping[str, Any], call_sid: str) -> None:
    """A call found through its pre-submission correlation (24.2): put its
    CallSid on the command if the worker has not, so a submission that timed
    out after Twilio placed the call can be confirmed by the reconciliation
    sweep (_ask_provider) instead of staying unknown. Best effort."""
    command_id, tenant = state.get("command_id"), state.get("tenant_id")
    if not (command_id and tenant and call_sid):
        return
    try:
        async with tenant_tx(_call_platform_ctx("twilio-correlation")) as conn:
            await conn.execute(
                "UPDATE command_executions SET provider = 'twilio', provider_reference = $3, updated_at = now() "
                "WHERE id = $1::uuid AND tenant_id = $2::uuid AND command_type = 'CALL' "
                "AND provider_reference IS NULL AND state IN ('executing', 'reconciliation_required')",
                str(command_id), str(tenant), call_sid)
    except Exception:  # noqa: BLE001 - the call itself is managed; this is the audit trail
        logger.warning("CallSid %s not recorded on command %s", call_sid, command_id, exc_info=True)


async def _twilio_call_brokerage(call_sid: str, correlation: str = "") -> str:
    """The brokerage whose outbound Twilio call this is: from the call state the
    worker wrote, else from the call session recorded with it. '' when
    neither knows it yet (the callback beat the worker; 24.2)."""
    from twilio_call_handler import load_twilio_call_state, load_twilio_correlation

    try:
        state = await load_twilio_call_state(call_sid)
        if not (state and state.get("direction") == "outbound") and correlation:
            # The worker has not recorded the CallSid yet (24.2): the signed
            # correlation names the call's brokerage from before it existed.
            state = await load_twilio_correlation(correlation)
            if state:
                await _record_twilio_call_on_command(state, call_sid)
    except Exception:  # noqa: BLE001 - the command row below still knows
        logger.warning("Twilio call state unreadable for %s; using the command record", call_sid,
                       exc_info=True)
        state = None
    if state and state.get("direction") == "outbound" and state.get("tenant_id"):
        return str(state["tenant_id"])
    async with tenant_tx(_call_platform_ctx("call-status-webhook")) as conn:
        tenant = await conn.fetchval(
            # Written with the command's provider reference, in one
            # transaction; indexed by provider_call_id (0174).
            "SELECT tenant_id::text FROM live_call_sessions WHERE provider_call_id = $1 LIMIT 1",
            call_sid)
    return str(tenant or "")


@router.post("/webhooks/twilio/status", include_in_schema=False)
async def twilio_status_webhook(request: Request):
    """Signed Twilio outbound call status → the same durable outcome path as
    Plivo (external audit 24.3).

    This route used to update live_call_sessions and clean up Redis only, so a
    Twilio call's answer never reached record_call_outcome: no first-response
    delivery, no lead_engaged meter, no carrier line on the client's timeline.
    Now the outcome is stored in provider_call_outcomes before Twilio is
    answered and applied through record_call_outcome, once, whether it arrives
    before or after the worker writes the session.
    """
    from twilio_call_handler import cleanup_twilio_call, cleanup_twilio_correlation

    form = await request.form()
    await _verify_outbound_twilio_signature(request, form, "/api/commands/webhooks/twilio/status")
    call_sid = str(form.get("CallSid") or "")
    correlation = _twilio_correlation(request)
    call_status = str(form.get("CallStatus") or "").strip().lower()
    if call_sid and call_status in _TWILIO_OUTCOME_STATUSES:
        try:
            duration: Optional[int] = int(str(form.get("CallDuration") or "").strip())
        except ValueError:
            duration = None
        if duration is not None and not 0 <= duration < 2**31:
            duration = None       # 0174's CHECK would refuse it on every retry
        try:
            brokerage = await _twilio_call_brokerage(call_sid, correlation)
            if not brokerage:
                # Whose call this is is not known yet, so it cannot be stored
                # against anyone. 503: Twilio retries (TWILIO_STATUS_RETRY).
                logger.warning("Twilio call outcome not attributable yet; asking for a retry: sid=%s status=%s",
                             call_sid, call_status)
                return Response(status_code=503)
            await accept_call_outcome("twilio", call_sid, call_status, duration, tenant_id=brokerage,
                                      aliases=[correlation] if correlation else ())
        except Exception:  # noqa: BLE001 - answered 503, never acknowledged into oblivion
            logger.error("Twilio call outcome NOT recorded; asking for a retry: sid=%s status=%s",
                         call_sid, call_status, exc_info=True)
            return Response(status_code=503)
        if call_status in _TWILIO_TERMINAL_STATUSES:
            try:
                await cleanup_twilio_call(call_sid)
                if correlation:
                    await cleanup_twilio_correlation(correlation)
            except Exception:  # noqa: BLE001 - the state's TTL ends it
                logger.warning("Twilio call state cleanup failed: sid=%s", call_sid, exc_info=True)
    logger.info(
        "Twilio call status received: sid=%s status=%s",
        call_sid,
        call_status or "unknown",
    )
    return Response(status_code=204)


@router.post("/webhooks/twilio/speech", include_in_schema=False)
async def twilio_speech(request: Request):
    """Handle speech recognized by Twilio Gather. Calls the AI, returns new TwiML."""
    form = await request.form()
    # Signed over the action URL, its call reference included (23.2).
    await _verify_outbound_twilio_signature(request, form, "/api/commands/webhooks/twilio/speech")
    speech_text = (form.get("SpeechResult") or "").strip()
    call_sid = form.get("CallSid", "")

    # The transcript itself is deliberately NOT logged. Call transcripts have
    # their own retention policy (ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS, see
    # platform_policy.PUBLIC_PROPERTY_DATA_POLICY); printing 100 characters of
    # what a caller said to stdout put that content in the container log stream
    # instead, where it lives under the log group's retention and nobody's
    # transcript policy. The length is enough to debug a silent leg.
    logger.info(
        "twilio speech webhook: call_sid=%s speech_chars=%d",
        call_sid, len(speech_text),
    )

    if not speech_text:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Sorry, I didn't hear you. Goodbye.</Say>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    from ai_chat_agent import _generate_voice_reply
    from twilio_call_handler import load_twilio_call_state

    # Whose AI budget this reply is spent from (T18.07): the brokerage the
    # call's state names. A turn that cannot be attributed — no state, a state
    # with no brokerage, or a state that cannot be read (unknown is not "no
    # brokerage") — is not answered by a model: spend no brokerage can be held
    # for is never a pass.
    try:
        state = await load_twilio_call_state(call_sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("twilio speech webhook: call state unreadable (%s)", type(exc).__name__)
        state = None
    tenant_id = str((state or {}).get("tenant_id") or "")
    if not tenant_id:
        logger.info("twilio speech webhook: call_sid=%s has no brokerage; ending the call", call_sid)
        return _voice_refused_twiml()

    ai_reply = "I'm sorry, I'm having trouble processing that. Could you try again?"
    try:
        ai_reply = await _generate_voice_reply(call_sid, speech_text, tenant_id=tenant_id)
    except SpendRefused as refused:
        logger.info("twilio speech webhook: call_sid=%s stopped by the AI budget (%s)", call_sid, refused.code)
        return _voice_refused_twiml()
    except Exception:
        import logging
        logging.getLogger("oracle.twilio").exception("Twilio AI reply failed")

    gather_attribs = (f'input="speech" action="{_twilio_speech_action_url(state)}" method="POST" '
                      'timeout="5" speechTimeout="auto"')

    # The reply is the line worth hearing in a voice demo — the greeting is
    # fixed text, this is the conversation. Synthesis happens after
    # _generate_voice_reply has already spent its budget, so voice_tts keeps a
    # short timeout of its own and falls back to Say rather than extending the
    # wait a caller is already sitting through.
    reply_speech = await _spoken(ai_reply, to_number=str(form.get("To") or ""))

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {reply_speech}
    <Gather {gather_attribs}>
        <Say voice="Polly.Joanna"></Say>
    </Gather>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _voice_refused_twiml() -> Response:
    """The line a caller hears when a reply cannot be spent for them (T18.07),
    then the call ends. Said with <Say>, not voice_tts: no text-to-speech is
    bought to say it."""
    from ai_chat_agent import VOICE_REFUSED_LINE

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">{_xml_escape(VOICE_REFUSED_LINE)}</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")
