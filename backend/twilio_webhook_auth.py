"""Webhook authentication for the command router's provider callbacks.

Moved verbatim out of commands_api.py (external audit 35-02, slice 2): the
shared-secret check for the ACS and custom-call webhooks
(_require_webhook_secret, which delegates to webhook_auth), and the Twilio
outbound-call signature check (_verify_outbound_twilio_signature) with its
helpers: the signed URL and query, the neoh_ref correlation, the form
normalization, the token loop (_validate_twilio_webhook_signature) and the
durable session-row fallback (_durable_twilio_call_state). The routes that
call them stay in commands_api.

commands_api re-exports every name, and its routes call them through
commands_api's own globals, so `monkeypatch.setattr(commands_api, "<name>",
...)` keeps taking effect for those route calls. Calls between the functions
of this module resolve here, not through commands_api: a test that replaces
_durable_twilio_call_state, or the tenant_tx it reads through, for the
signature check must patch this module.

Logs go to the "oracle.commands" logger, as they did from commands_api.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

from fastapi import HTTPException, Request

from call_platform_ctx import _call_platform_ctx
from db.connection import tenant_tx

logger = logging.getLogger("oracle.commands")


async def _require_webhook_secret(request: Request, env_name: str) -> str:
    """Header, body signature or signed callback URL; the old `?token=` static
    secret is refused (removed 2026-11-13; HOOK-3, webhook_auth)."""
    import webhook_auth

    return await webhook_auth.verify_webhook(request, env_name)


def _twilio_webhook_url(request: Request, suffix: str) -> str:
    public_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
    if public_base:
        return f"{public_base}{suffix}"

    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Unable to determine webhook URL for Twilio signature validation.",
        )
    return f"{scheme}://{host}{suffix}"


def _twilio_signed_suffix(request: Request, path: str) -> str:
    """The path plus the query Twilio was given (and signed): an outbound
    call's answer and status URLs carry Neoh's correlation (24.2)."""
    query = str(getattr(request.url, "query", "") or "")
    return f"{path}?{query}" if query else path


def _twilio_correlation(request: Request) -> str:
    from twilio_call_handler import TWILIO_CORRELATION_PARAM

    return str(request.query_params.get(TWILIO_CORRELATION_PARAM) or "")


def _normalize_twilio_form(form: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(key, str):
            key = str(key)
        if isinstance(value, str):
            normalized[key] = value
    return normalized


def _validate_twilio_webhook_signature(
    request: Request,
    form: Any,
    suffix: str,
    *,
    tokens: Sequence[str],
) -> None:
    """`tokens` are required: those of the account that carries the call
    (_verify_outbound_twilio_signature) — there is no default trust set."""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Twilio-Signature header.")

    tokens = [token for token in dict.fromkeys(tokens or ()) if token]
    if not tokens:
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook cannot be validated because no auth token is configured.",
        )

    params = _normalize_twilio_form(form)
    webhook_url = _twilio_webhook_url(request, suffix)

    try:
        from twilio.request_validator import RequestValidator
    except Exception as exc:
        logger.exception("twilio request validator import failed")
        raise HTTPException(
            status_code=503,
            detail="Twilio request validation is unavailable.",
        ) from exc

    for token in tokens:
        if RequestValidator(token).validate(webhook_url, params, signature):
            return

    logger.warning(
        "twilio webhook signature validation failed path=%s",
        suffix,
    )
    raise HTTPException(status_code=400, detail="Invalid Twilio signature.")


async def _durable_twilio_call_state(call_sid: str) -> Optional[dict[str, Any]]:
    """When Redis no longer names an outbound call (expired after an hour, an
    outage, a failover, an eviction): its brokerage from the session row the
    worker wrote with Twilio's reference (live_call_sessions, in the same
    transaction as the command's provider reference; indexed by
    provider_call_id, 0174), read as the platform as _twilio_call_brokerage
    reads it; its account by the outbound rail's own rule
    (twilio_call_handler.tenant_twilio_account). None when no single
    brokerage's session names the call. Raises when a row cannot be read."""
    from twilio_call_handler import tenant_twilio_account

    async with tenant_tx(_call_platform_ctx("call-status-webhook")) as conn:
        tenant = await conn.fetchval(
            "SELECT CASE WHEN count(DISTINCT tenant_id) = 1 THEN min(tenant_id::text) END "
            "FROM live_call_sessions WHERE provider_call_id = $1",
            call_sid)
    if not tenant:
        return None
    return {"tenant_id": str(tenant), "account_sid": await tenant_twilio_account(str(tenant)),
            "direction": "outbound", "via": "durable"}


async def _verify_outbound_twilio_signature(
    request: Request,
    form: Any,
    path: str,
) -> None:
    """Check an outbound call's webhook against the tokens of the account
    that carries the call (external audit 23.2) — before anything is waited
    for or acted on.

    The account comes from Neoh's own state: the CallSid's Redis state or the
    pre-submission correlation (neoh_ref), each read once; when Redis names
    neither (expired, or unreachable), the durable session row the worker
    wrote (_durable_twilio_call_state), whose account the request's
    AccountSid must then equal. The platform's account is checked with
    TWILIO_AUTH_TOKEN and TWILIO_AUTH_TOKEN_PREVIOUS; any other account only
    with that brokerage's stored token, whose account must match.
    Bring-your-own Twilio is supported: PUT /providers/twilio stores the
    credential the outbound rail dials with.

    Refused: a missing or bad signature (400); a reference paired with
    another call (crossed), a call nothing names, or an unreadable state or
    credential (503, as telephony_api._route_twilio_tokens refuses) — never
    checked against another account's token. After the signature verifies,
    the reference is bound to this CallSid (first binder wins), so it cannot
    serve another call later.
    """
    if not request.headers.get("X-Twilio-Signature", ""):
        raise HTTPException(status_code=400, detail="Missing X-Twilio-Signature header.")
    from twilio_call_handler import (
        TwilioCallReferenceCrossed,
        TwilioCallStateUnavailable,
        bind_twilio_correlation,
        outbound_twilio_signing_state,
        twilio_signing_tokens,
    )

    call_sid = str(form.get("CallSid") or "")
    correlation = _twilio_correlation(request)
    claimed = str(form.get("AccountSid") or "")
    try:
        try:
            state = await outbound_twilio_signing_state(call_sid, correlation)
        except TwilioCallReferenceCrossed:
            raise
        except Exception as exc:  # noqa: BLE001 - Redis unreadable: the durable rows decide
            logger.warning("twilio webhook: call state unreadable (%s); using the session row path=%s sid=%s",
                           type(exc).__name__, path, call_sid)
            state = None
        if state is None:
            state = await _durable_twilio_call_state(call_sid)
            if state is None:
                raise TwilioCallStateUnavailable("no outbound call state or session names this call")
            if claimed != state["account_sid"]:
                # Without Redis the account is the rail's rule for the
                # brokerage; the signed request must name that same account.
                logger.warning("twilio webhook refused: AccountSid is not the brokerage's account path=%s sid=%s",
                               path, call_sid)
                raise HTTPException(status_code=400, detail="Invalid Twilio signature.")
        tokens = await twilio_signing_tokens(state)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - unresolvable is refused, never another account's token
        reason = str(exc) if isinstance(exc, TwilioCallStateUnavailable) else type(exc).__name__
        logger.warning("twilio webhook refused: no signing account for the call path=%s sid=%s reason=%s",
                       path, call_sid, reason)
        raise HTTPException(
            status_code=503,
            detail="Twilio webhook cannot be validated for this call's account.",
        ) from exc
    if claimed and claimed != str(state.get("account_sid") or ""):
        logger.warning("twilio webhook refused: AccountSid is not the call's account path=%s sid=%s",
                       path, call_sid)
        raise HTTPException(status_code=400, detail="Invalid Twilio signature.")
    _validate_twilio_webhook_signature(request, form, _twilio_signed_suffix(request, path), tokens=tokens)
    if correlation and state.get("via") != "durable":
        try:
            bound = await bind_twilio_correlation(correlation, call_sid)
        except Exception as exc:  # noqa: BLE001 - unbindable is refused, as unreadable
            logger.warning("twilio webhook refused: reference not bindable (%s) path=%s sid=%s",
                           type(exc).__name__, path, call_sid)
            raise HTTPException(status_code=503, detail="Twilio call state is unavailable.") from exc
        if not bound:
            logger.warning("twilio webhook refused: reference already serves another call path=%s sid=%s",
                           path, call_sid)
            raise HTTPException(status_code=503,
                                detail="Twilio webhook cannot be validated for this call's account.")
