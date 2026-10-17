"""The last observed outcome of each property-data provider call (0160,
ledger L19-05), so health reports what actually happened instead of UNKNOWN.

avm_client calls record() after every RentCast / ATTOM request. It stores a
provider name, a fixed failure kind and a counter — never an address, a
status body or error text — and it never raises or delays the valuation by
more than its own short timeout: losing an outcome row costs a health signal,
not a customer answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("oracle.provider_health")

PROVIDERS = ("rentcast", "attom", "platform_smtp", "platform_twilio_sms", "platform_twilio_voice")
#: The platform email relay (0164, L10-11): recorded by
#: command_providers.send_smtp_email when a tenant has no mail server of its own.
PLATFORM_SMTP = "platform_smtp"
#: The platform Twilio account (0165, L10-12): recorded by send_twilio_sms and
#: place_twilio_call when a tenant has no Twilio account of its own. Texts and
#: calls are recorded apart — an account that can text may not be able to call.
PLATFORM_TWILIO_SMS = "platform_twilio_sms"
PLATFORM_TWILIO_VOICE = "platform_twilio_voice"
OK = "ok"
FAILURE_KINDS = ("credential_rejected", "rate_limited", "http_error", "timeout", "unavailable", "bad_response")
_TIMEOUT_S = 1.5
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"

# Literal statements only (bandit B608).
_RECORD_OK = """
    INSERT INTO provider_health AS h (provider, last_ok_at, consecutive_failures, updated_at)
    VALUES ($1, now(), 0, now())
    ON CONFLICT (provider) DO UPDATE
       SET last_ok_at = now(), consecutive_failures = 0, updated_at = now()
"""
_RECORD_FAILURE = """
    INSERT INTO provider_health AS h (provider, last_failure_at, last_failure_kind, consecutive_failures, updated_at)
    VALUES ($1, now(), $2, 1, now())
    ON CONFLICT (provider) DO UPDATE
       SET last_failure_at = now(), last_failure_kind = $2,
           consecutive_failures = h.consecutive_failures + 1, updated_at = now()
"""


def kind_for(status_entry: Optional[dict]) -> Optional[str]:
    """Map an avm_client source_status entry to an outcome, or None when the
    call was not made (not configured, no address, credential cooldown)."""
    import source_status as ss

    if not status_entry:
        return None
    status, reason = status_entry.get("status"), status_entry.get("reason")
    if status == ss.CHECKED:
        return OK
    if reason == ss.CREDENTIAL_REJECTED:
        return "credential_rejected"
    if reason == ss.RATE_LIMITED:
        return "rate_limited"
    if reason == ss.UPSTREAM_HTTP_ERROR:
        return "http_error"
    if reason == ss.BAD_RESPONSE:
        return "bad_response"
    if status == getattr(ss, "TIMED_OUT", "timed_out"):
        return "timeout"
    if status == getattr(ss, "UNAVAILABLE", "unavailable"):
        return "unavailable"
    return None


async def record(provider: str, kind: Optional[str]) -> None:
    """Upsert one outcome. Never raises."""
    if provider not in PROVIDERS or kind is None:
        return
    if kind != OK and kind not in FAILURE_KINDS:
        return
    try:
        await asyncio.wait_for(_write(provider, kind), timeout=_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a health signal must never fail a valuation
        logger.debug("provider outcome not recorded: %s %s", provider, kind, exc_info=True)


_READ = "SELECT last_ok_at, last_failure_at, last_failure_kind, consecutive_failures FROM provider_health WHERE provider = $1"


async def last_outcome(provider: str) -> Optional[dict]:
    """The stored outcome row, or None when nothing was recorded or it could
    not be read (the caller treats both as "not verified")."""
    from db.connection import get_pool, tenant_tx
    from tenancy import Role, TenantContext

    if provider not in PROVIDERS or get_pool() is None:
        return None
    ctx = TenantContext(agent_id="provider-health", role=Role.PLATFORM_ADMIN,
                        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", _PLATFORM_TENANT))
    try:
        async with tenant_tx(ctx) as conn:
            row = await asyncio.wait_for(conn.fetchrow(_READ, provider), timeout=_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — unread health is "not verified", never an error page
        logger.debug("provider outcome not read: %s", provider, exc_info=True)
        return None
    return dict(row) if row else None


def state(configured: bool, outcome: Optional[dict]) -> str:
    """The delivery-page state of a platform sender: not_configured |
    not_yet_verified | verified_by_send | failing."""
    if not configured:
        return "not_configured"
    if verified(outcome):
        return "verified_by_send"
    if outcome and outcome.get("consecutive_failures"):
        return "failing"
    return "not_yet_verified"


def verified(outcome: Optional[dict]) -> bool:
    """A provider is verified by its own last calls: at least one success and
    no failure since (consecutive_failures resets on success)."""
    return bool(outcome and outcome.get("last_ok_at") and not outcome.get("consecutive_failures"))


async def _write(provider: str, kind: str) -> None:
    from db.connection import get_pool, tenant_tx
    from tenancy import Role, TenantContext

    if get_pool() is None:
        return
    ctx = TenantContext(agent_id="provider-health", role=Role.PLATFORM_ADMIN,
                        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", _PLATFORM_TENANT))
    async with tenant_tx(ctx) as conn:
        if kind == OK:
            await conn.execute(_RECORD_OK, provider)
        else:
            await conn.execute(_RECORD_FAILURE, provider, kind)
