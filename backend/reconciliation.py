"""Bounded reconciliation of states that would otherwise never resolve.

"Unknown" is a legitimate state — when the network dies at the worst moment
Neoh cannot know whether a provider acted, and it must not guess (a blind
retry places a second call; a false "failed" invites someone to resend). But
unknown must not mean *forever*. This sweep, run by the scheduler
(periodic.provider_reconciliation), resolves what can be resolved from the
provider's own records, bounds every retry, and leaves the rest visibly
waiting for a human:

* commands stuck in `executing` (a worker died mid-submission) → reconciliation
* commands in `reconciliation_required` with a provider id → asked at the
  provider, at most RECONCILE_MAX_ATTEMPTS times
* email_outbox rows still `queued` → delivery re-enqueued (claim prevents dups)
* AI chat turns stuck `pending`/`streaming` → failed (they locked users out)
* reconstruction jobs `running` with no progress → failed, needs attention
  (source media is never touched)
* agent call intents past their expiry → expired
* number purchases `intended`/`unknown` → adopted or concluded; superseded
  numbers released

Every query is bounded (LIMIT) and every provider call has a timeout.
Nothing here sends a message, places a call or charges anyone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import Role, TenantContext

log = logging.getLogger("oracle.reconciliation")

RECONCILE_MAX_ATTEMPTS = 6
EXECUTING_STALE_MINUTES = 15
CHAT_STUCK_MINUTES = 15
RECON_STALL_HOURS = float(os.getenv("ORACLE_RECON_STALL_HOURS", "6"))
_BATCH = 100

# Provider statuses that prove the side effect happened / definitely did not.
_TWILIO_CALL_HAPPENED = {"queued", "initiated", "ringing", "in-progress", "completed", "busy", "no-answer"}
_TWILIO_CALL_NEVER = {"failed", "canceled"}
_TWILIO_SMS_HAPPENED = {"accepted", "queued", "sending", "sent", "delivered", "undelivered", "read"}
_TWILIO_SMS_NEVER = {"failed", "canceled"}
_TELNYX_HAPPENED = {"queued", "sending", "sent", "delivered", "delivery_unconfirmed", "delivery_failed"}
_TELNYX_NEVER = {"sending_failed"}


def _platform() -> TenantContext:
    return TenantContext(agent_id="reconciliation", tenant_id=os.getenv(
        "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"), role=Role.PLATFORM_ADMIN)


async def run_sweep(*, only_tenant: Optional[str] = None) -> dict[str, Any]:
    """`only_tenant` confines every step to one tenant (tests and targeted
    operator runs); production sweeps everything."""
    import recovery_mode

    out: dict[str, Any] = {}
    ctx = _platform()
    async with tenant_tx(ctx) as conn:
        out["executing_to_reconcile"] = _n(await conn.execute(
            """
            UPDATE command_executions
               SET state='reconciliation_required',
                   reconciliation_reason='the worker stopped while submitting; delivery unknown',
                   updated_at=now()
             WHERE id IN (SELECT id FROM command_executions WHERE state='executing'
                           AND updated_at < now() - make_interval(mins => $1)
                           AND ($3::uuid IS NULL OR tenant_id = $3) LIMIT $2)
            """, EXECUTING_STALE_MINUTES, _BATCH, only_tenant))
        out["chat_turns_failed"] = _n(await conn.execute(
            """
            UPDATE ai_chat_messages SET status='failed', error_code='AI_RESPONSE_TIMEOUT', updated_at=now()
             WHERE id IN (SELECT id FROM ai_chat_messages WHERE status IN ('pending','streaming')
                           AND updated_at < now() - make_interval(mins => $1)
                           AND ($3::uuid IS NULL OR tenant_id = $3) LIMIT $2)
            """, CHAT_STUCK_MINUTES, _BATCH, only_tenant))
        out["reconstructions_timed_out"] = _n(await conn.execute(
            """
            UPDATE reconstruction_jobs
               SET status='failed', updated_at=now(), stage='failed', failure_category='stalled',
                   error=left('Timed out with no progress for ' || $3 || ' h — needs attention. '
                              || 'Your original photos and video are kept.', 500)
             WHERE id IN (SELECT id FROM reconstruction_jobs WHERE status='running'
                           AND updated_at < now() - make_interval(hours => $1)
                           AND ($4::uuid IS NULL OR tenant_id = $4) LIMIT $2)
            """, int(RECON_STALL_HOURS), _BATCH, str(int(RECON_STALL_HOURS)), only_tenant))
        out["call_intents_expired"] = _n(await conn.execute(
            """
            UPDATE agent_call_intents SET state='expired', updated_at=now()
             WHERE id IN (SELECT id FROM agent_call_intents WHERE state IN ('prepared','authorized')
                           AND expires_at < now() AND ($2::uuid IS NULL OR tenant_id = $2) LIMIT $1)
            """, _BATCH, only_tenant))
        out["purchases_intended_to_unknown"] = _n(await conn.execute(
            """
            UPDATE provider_purchases SET state='unknown', error='no outcome recorded (process stopped?)',
                   updated_at=now()
             WHERE id IN (SELECT id FROM provider_purchases WHERE state='intended'
                           AND created_at < now() - interval '15 minutes'
                           AND ($2::uuid IS NULL OR tenant_id = $2) LIMIT $1)
            """, _BATCH, only_tenant))
        stale_outbox = await conn.fetch(
            "SELECT id, tenant_id FROM email_outbox WHERE status='queued' "
            "AND updated_at < now() - interval '10 minutes' AND ($2::uuid IS NULL OR tenant_id = $2) "
            "ORDER BY updated_at LIMIT $1", _BATCH, only_tenant)
    if recovery_mode.is_recovery_mode():
        # Nothing may leave a restored copy; re-enqueueing would only
        # dead-letter again every hour.
        out["emails_reenqueued"] = "skipped: recovery mode"
    else:
        out["emails_reenqueued"] = await _reenqueue_outbox(stale_outbox)
    if recovery_mode.is_recovery_mode():
        out["provider_checks"] = "skipped: recovery mode"
        return out
    out["commands"] = await _reconcile_commands(only_tenant)
    out["purchases"] = await _reconcile_purchases(only_tenant)
    return out


def _n(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def _reenqueue_outbox(rows) -> int:
    import email_outbox
    from automation_jobs import enqueue_job

    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    n = 0
    for row in rows:
        ctx = TenantContext(agent_id="reconciliation", tenant_id=str(row["tenant_id"]), role=Role.PLATFORM_ADMIN)
        try:
            _, created = await enqueue_job(
                ctx, job_type=email_outbox.JOB_TYPE, payload={"outbox_id": str(row["id"])},
                idempotency_key=f"email-outbox:{row['id']}:{bucket}", created_by="reconciliation",
                queue_name="interactive", priority=30, max_attempts=6)
            n += int(bool(created))
        except Exception:  # noqa: BLE001 - next sweep tries again
            log.exception("re-enqueue of email %s failed", row["id"])
    return n


async def _reconcile_commands(only_tenant: Optional[str] = None) -> dict[str, int]:
    ctx = _platform()
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, command_type, provider, provider_reference, reconcile_attempts
              FROM command_executions
             WHERE state='reconciliation_required' AND reconcile_attempts < $1
               AND (reconcile_checked_at IS NULL OR reconcile_checked_at < now() - interval '10 minutes')
               AND ($3::uuid IS NULL OR tenant_id = $3)
             ORDER BY updated_at LIMIT $2
            """, RECONCILE_MAX_ATTEMPTS, _BATCH, only_tenant)
    counts = {"confirmed": 0, "not_sent": 0, "still_unknown": 0, "needs_human": 0}
    for row in rows:
        verdict = None
        if row["provider_reference"]:
            try:
                verdict = await asyncio.wait_for(_ask_provider(row), timeout=20)
            except Exception as exc:  # noqa: BLE001 - provider unreachable: try again later
                log.info("reconcile %s: provider check failed (%s)", row["id"], type(exc).__name__)
        attempts = int(row["reconcile_attempts"]) + 1
        async with tenant_tx(ctx) as conn:
            if verdict == "happened":
                await conn.execute(
                    "UPDATE command_executions SET state='succeeded', reconcile_attempts=$2, "
                    "reconcile_checked_at=now(), reconciliation_reason='confirmed at the provider', "
                    "updated_at=now() WHERE id=$1 AND state='reconciliation_required'", row["id"], attempts)
                counts["confirmed"] += 1
            elif verdict == "never":
                await conn.execute(
                    "UPDATE command_executions SET state='failed', reconcile_attempts=$2, "
                    "reconcile_checked_at=now(), last_error='the provider reports it was not sent', "
                    "updated_at=now() WHERE id=$1 AND state='reconciliation_required'", row["id"], attempts)
                counts["not_sent"] += 1
            else:
                reason = ("delivery unknown and no provider id was returned — check with the recipient"
                          if not row["provider_reference"] else "the provider has not confirmed it yet")
                if attempts >= RECONCILE_MAX_ATTEMPTS:
                    reason = "needs review: " + reason
                    counts["needs_human"] += 1
                else:
                    counts["still_unknown"] += 1
                await conn.execute(
                    "UPDATE command_executions SET reconcile_attempts=$2, reconcile_checked_at=now(), "
                    "reconciliation_reason=$3 WHERE id=$1", row["id"], attempts, reason)
    return counts


async def _ask_provider(row) -> Optional[str]:
    """'happened' | 'never' | None (unknown)."""
    provider = str(row["provider"] or "")
    ref = str(row["provider_reference"])
    tctx = TenantContext(agent_id="reconciliation", tenant_id=str(row["tenant_id"]), role=Role.PLATFORM_ADMIN)
    if provider.startswith("twilio"):
        from command_providers import _twilio_client
        from telephony_api import _twilio_credentials

        creds = await _twilio_credentials(tctx)

        def _fetch() -> str:
            client = _twilio_client(creds.get("account_sid", ""), creds.get("auth_token", ""),
                                    creds.get("api_key", ""), creds.get("api_secret", ""))
            if ref.startswith("CA"):
                return str(client.calls(ref).fetch().status)
            return str(client.messages(ref).fetch().status)

        status = (await asyncio.to_thread(_fetch)).lower()
        happened, never = ((_TWILIO_CALL_HAPPENED, _TWILIO_CALL_NEVER) if ref.startswith("CA")
                           else (_TWILIO_SMS_HAPPENED, _TWILIO_SMS_NEVER))
        return "happened" if status in happened else "never" if status in never else None
    if provider == "telnyx":
        from commands_api import _load_provider_credential
        from messaging_provider import TelnyxMessagingProvider

        raw = await _load_provider_credential(tctx, "telnyx")
        creds = json.loads(raw) if raw else None

        def _fetch() -> str:
            message = TelnyxMessagingProvider._client(creds).messages.retrieve(ref)
            to = (getattr(getattr(message, "data", None), "to", None) or [None])[0]
            return str(getattr(to, "status", "") or "")

        status = (await asyncio.to_thread(_fetch)).lower()
        return "happened" if status in _TELNYX_HAPPENED else "never" if status in _TELNYX_NEVER else None
    if provider in ("smtp", "google_calendar"):
        return None  # no provider-side lookup: a human checks
    return None


async def _reconcile_purchases(only_tenant: Optional[str] = None) -> dict[str, int]:
    """Unknown Twilio purchases: find a Neoh-named number bought around the
    attempt that no route uses; adopt it so the next connect reuses it. After
    an hour with nothing found, nothing was bought. Superseded numbers are
    released."""
    ctx = _platform()
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, agent_id, provider, state, provider_ref, detail, created_at "
            "FROM provider_purchases WHERE state IN ('unknown','confirmed') "
            "AND ($2::uuid IS NULL OR tenant_id = $2) ORDER BY created_at LIMIT $1", _BATCH, only_tenant)
        used = {r[0] for r in await conn.fetch(
            "SELECT inbound_forwarding_provider_sid FROM telephony_routes WHERE inbound_forwarding_provider_sid IS NOT NULL")}
    counts = {"adopted": 0, "nothing_bought": 0, "released": 0, "still_unknown": 0}
    for row in rows:
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else dict(row["detail"] or {})
        tctx = TenantContext(agent_id=str(row["agent_id"]), tenant_id=str(row["tenant_id"]), role=Role.PLATFORM_ADMIN)
        try:
            if row["state"] == "confirmed" and detail.get("superseded"):
                from voice_provider import get_voice_provider

                await asyncio.wait_for(get_voice_provider(row["provider"]).release_forwarding_number(
                    str(row["provider_ref"]), credentials=await _voice_credentials(tctx, row["provider"])), timeout=40)
                await _purchase_state(row["id"], "released")
                counts["released"] += 1
            elif row["state"] == "unknown" and row["provider"] == "twilio":
                found = await asyncio.wait_for(_find_unclaimed_twilio_number(tctx, row["created_at"], used), timeout=30)
                if found:
                    await _purchase_state(row["id"], "confirmed", ref=found[0], phone=found[1])
                    used.add(found[0])
                    counts["adopted"] += 1
                elif row["created_at"] < datetime.now(timezone.utc) - timedelta(hours=1):
                    await _purchase_state(row["id"], "failed", error="no number was bought")
                    counts["nothing_bought"] += 1
                else:
                    counts["still_unknown"] += 1
            elif row["state"] == "unknown":
                counts["still_unknown"] += 1  # Plivo: operator checks the account (runbook)
        except Exception as exc:  # noqa: BLE001 - provider down: next sweep
            log.info("purchase %s reconcile deferred (%s)", row["id"], type(exc).__name__)
            counts["still_unknown"] += 1
    return counts


async def _voice_credentials(tctx, provider: str) -> dict:
    try:
        if provider == "plivo":
            from telephony_api import _plivo_credentials

            return await _plivo_credentials(tctx)
        from telephony_api import _twilio_credentials

        return await _twilio_credentials(tctx)
    except Exception:  # noqa: BLE001
        return {}


async def _find_unclaimed_twilio_number(tctx, since: datetime, used: set) -> Optional[tuple[str, str]]:
    from command_providers import _twilio_client

    creds = await _voice_credentials(tctx, "twilio")

    def _list():
        client = _twilio_client(creds.get("account_sid", "") or os.getenv("TWILIO_ACCOUNT_SID", ""),
                                creds.get("auth_token", "") or os.getenv("TWILIO_AUTH_TOKEN", ""),
                                creds.get("api_key", ""), creds.get("api_secret", ""))
        out = []
        for number in client.incoming_phone_numbers.list(limit=200):
            name = str(number.friendly_name or "")
            created = number.date_created
            if name.startswith("neoh-forwarding-") and number.sid not in used and created and \
                    created >= since - timedelta(minutes=2):
                out.append((str(number.sid), str(number.phone_number)))
        return out

    candidates = await asyncio.to_thread(_list)
    return candidates[0] if len(candidates) == 1 else None


async def _purchase_state(purchase_id, state: str, *, ref: Optional[str] = None, phone: Optional[str] = None,
                          error: Optional[str] = None) -> None:
    async with tenant_tx(_platform()) as conn:
        await conn.execute(
            "UPDATE provider_purchases SET state=$2, provider_ref=COALESCE($3, provider_ref), "
            "detail = detail || $4::jsonb, error=$5, updated_at=now() WHERE id=$1",
            purchase_id, state, ref, json.dumps({"phone_number": phone} if phone else {}), error)
