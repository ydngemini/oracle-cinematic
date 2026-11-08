"""Which delivery channels this agent definitively cannot use yet, and what
each needs (prompt 07, T07.04, AC07.01; ledger L07-03).

Mirrors the preconditions commands_api's execution paths fail closed on — the
same reads, in the same words — never the delivery page's validated /
unvalidated states: a saved-but-unvalidated account can still carry a send,
and the first send is how it gets proven (L10-11, L10-12). So:

  sms       ORACLE_MESSAGING_PROVIDER (Telnyx by default): an active hosted
            messaging order on this agent's voice-verified business number;
            on the Twilio rail, a Twilio account somewhere (tenant row or env).
  voice     a verified outbound caller ID (the one thing an AI-placed call may
            trust), then the account for the route's own rail: Plivo (tenant
            row or PLIVO_* env) or Twilio (tenant row, TWILIO_* env, or the
            custom call URL the Twilio path falls back to).
  email     a mail credential this agent can use (their own, the brokerage
            default or a shared one — _load_provider_credential's own order),
            or the platform relay in the environment.
  calendar  the Google row the calendar command loads: this agent's own label,
            unexpired or refreshable. An expired access token with a refresh
            token is not a disconnected calendar.

Everything here is read, never decided: the credential helpers are the
executor's own selection without the decrypt. A read that fails leaves that
channel out — unknown is not a gap (C6).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("oracle.channel_setup")

CHANNELS = ("email", "sms", "voice", "calendar")


def _env(*names: str) -> bool:
    return all(os.getenv(n, "").strip() for n in names)


def _env_twilio() -> bool:
    return _env("TWILIO_ACCOUNT_SID") and (_env("TWILIO_AUTH_TOKEN") or _env("TWILIO_API_KEY", "TWILIO_API_SECRET"))


def _env_plivo() -> bool:
    return _env("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")


def _env_smtp_relay() -> bool:
    return _env("ORACLE_SMTP_FROM_EMAIL") or _env("ORACLE_SMTP_USERNAME")


async def _email(ctx: Any) -> Optional[str]:
    import commands_api

    _sender, credential_key = commands_api._resolve_email_provider()
    if await commands_api.has_provider_credential(ctx, credential_key, account_label=ctx.agent_id):
        return None
    if credential_key != "smtp":
        return None  # another sender's own settings decide; nothing definitive to say
    if _env_smtp_relay():
        return None
    return ("email isn't set up: no mail server is connected for this brokerage and the platform "
            "has no sender. Connect one under Providers.")


async def _sms(ctx: Any) -> Optional[str]:
    import commands_api
    import messaging_data
    from messaging_provider import messaging_provider_name

    if messaging_provider_name() == "telnyx":
        route = await messaging_data.get_messaging_route(ctx)
        number = await messaging_data.get_public_business_number(ctx)
        status = (route or {}).get("hosted_order_status")
        if route is not None and number is not None and status == "active":
            return None
        if route is not None and status in ("pending_verification", "processing", "pending_documents",
                                            "manual_action_required"):
            return (f"texting isn't ready yet: text messages for your business number are still being "
                    f"set up ({status}); nothing can be sent until that finishes.")
        return ("texting isn't set up: connect and finish setting up text messages for your business "
                "number before sending.")
    if _env_twilio() or await commands_api.has_provider_credential(ctx, "twilio", account_label=ctx.agent_id):
        return None
    return "texting isn't set up: no Twilio account is connected for this brokerage and the platform has none."


async def _voice(ctx: Any) -> Optional[str]:
    import commands_api
    import inbound_voice

    if not await inbound_voice.get_verified_caller_id(ctx):
        return "Neoh calls aren't set up: connect and verify your business number before placing calls."
    route = await inbound_voice.get_telephony_route(ctx)
    provider = str((route or {}).get("provider") or "twilio")
    if provider == "plivo":
        if _env_plivo() or await commands_api.has_provider_credential(ctx, "plivo", account_label=ctx.agent_id):
            return None
        return ("Neoh calls aren't set up: this business number is on Plivo and no Plivo account is "
                "connected for this brokerage or the platform.")
    if (_env_twilio() or _env("ORACLE_CUSTOM_CALL_API_URL")
            or await commands_api.has_provider_credential(ctx, "twilio", account_label=ctx.agent_id)):
        return None
    return ("Neoh calls aren't set up: no Twilio account is connected for this brokerage or the "
            "platform.")


async def _calendar(ctx: Any) -> Optional[str]:
    import commands_api

    if await commands_api.has_google_calendar(ctx, ctx.agent_id):
        return None
    return ("the calendar isn't connected: scheduling needs your Google Calendar connected under "
            "Providers (or reconnected, if its connection has expired).")


_CHECKS: dict[str, Callable[[Any], Awaitable[Optional[str]]]] = {
    "email": _email, "sms": _sms, "voice": _voice, "calendar": _calendar,
}


async def gaps(ctx: Any) -> dict[str, str]:
    """channel -> the setup it needs, for every channel this actor definitively
    cannot use right now. A channel whose state could not be read is absent."""
    out: dict[str, str] = {}
    for channel, check in _CHECKS.items():
        try:
            reason = await check(ctx)
        except Exception:  # noqa: BLE001 — unknown is not a gap
            logger.debug("channel setup state for %s not readable", channel, exc_info=True)
            continue
        if reason:
            out[channel] = reason
    return out
