"""Deliver email_outbox rows — the agent's own composer emails.

`POST /api/crm/clients/{id}/messages` (crm.py) wrote an outbound email into
email_outbox, logged it as contact, advanced last_contacted_at — and nothing
ever sent it (resilience audit 2026-10-02: rows sat in `queued` forever while
the client read as contacted). This is the sender.

States (email_outbox.status):
    queued   → waiting to be sent (or re-queued after a definite failure)
    sending  → claimed; the SMTP conversation is in progress, or it ended so
               that delivery is UNKNOWN (connection lost after DATA). Such a
               row is never resent automatically: error says so, and a human
               decides. Unknown is a valid state.
    sent     → the server accepted it (provider_message_id = Message-ID)
    failed   → refused, or not configured (error says which)
The claim is a single UPDATE … WHERE status='queued', so two workers can never
send the same row.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import recovery_mode
from db.connection import tenant_tx
from tenancy import Role, TenantContext

log = logging.getLogger("oracle.email_outbox")

JOB_TYPE = "email:outbox"
UNKNOWN_DELIVERY = "delivery_unknown"


async def enqueue_delivery(ctx: TenantContext, outbox_id: str) -> None:
    from automation_jobs import enqueue_job

    await enqueue_job(ctx, job_type=JOB_TYPE, payload={"outbox_id": outbox_id},
                      idempotency_key=f"email-outbox:{outbox_id}", created_by=ctx.agent_id,
                      queue_name="interactive", priority=20, max_attempts=6)


async def deliver(tenant_id: str, outbox_id: str) -> dict[str, Any]:
    from command_providers import (
        ProviderConfigurationError,
        ProviderRejectedError,
        ProviderRequestError,
    )
    from commands_api import _load_provider_credential, _resolve_email_provider, load_agent_identity

    worker_ctx = TenantContext(agent_id="email-outbox", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(worker_ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE email_outbox SET status='sending', updated_at=now()
             WHERE id=$1 AND tenant_id=$2 AND status='queued'
             RETURNING id, client_id, to_email, subject, body_text, created_by
            """, outbox_id, tenant_id)
    if row is None:
        return {"status": "not_queued"}  # already sent, cancelled, or claimed

    drafting = TenantContext(agent_id=str(row["created_by"] or "email-outbox"), tenant_id=tenant_id,
                             role=Role.PLATFORM_ADMIN)
    try:
        sender, credential_key = _resolve_email_provider()
        raw = await _load_provider_credential(drafting, credential_key, account_label=drafting.agent_id)
        identity = await load_agent_identity(drafting)
        credentials = json.loads(raw) if raw else None
        result = await sender(
            {"target": {"email": row["to_email"]}, "subject": row["subject"], "body": row["body_text"]},
            credentials=credentials,
            reply_to=str(identity.get("public_email") or "").strip() or None,
        )
    except recovery_mode.RecoveryModeBlocked as exc:
        # Refused before any network call: definitely not sent. Back in the
        # queue; the worker dead-letters the job (no retry storm), and the
        # sweep re-enqueues it once recovery mode is lifted.
        await _finish(tenant_id, outbox_id, "queued", error=f"held: {str(exc)[:200]}")
        raise
    # ProviderRejectedError subclasses ProviderRequestError: test it first.
    except ProviderRejectedError as exc:
        # Nothing was sent. A connection refusal / outage is worth retrying;
        # the job's backoff decides. Put it back in the queue and raise.
        await _finish(tenant_id, outbox_id, "queued", error=str(exc)[:500])
        raise
    except ProviderRequestError as exc:
        # Possibly delivered. Leave it in 'sending' with the reason; never resend.
        await _finish(tenant_id, outbox_id, "sending", error=f"{UNKNOWN_DELIVERY}: {str(exc)[:300]}")
        return {"status": UNKNOWN_DELIVERY}
    except (ProviderConfigurationError, ValueError, TypeError) as exc:
        await _finish(tenant_id, outbox_id, "failed", error=f"not configured: {str(exc)[:400]}")
        return {"status": "failed"}
    except Exception as exc:  # noqa: BLE001 - unknown failure after the claim
        await _finish(tenant_id, outbox_id, "sending", error=f"{UNKNOWN_DELIVERY}: {type(exc).__name__}")
        return {"status": UNKNOWN_DELIVERY}

    async with tenant_tx(worker_ctx) as conn:
        await conn.execute(
            "UPDATE email_outbox SET status='sent', sent_at=now(), provider_message_id=$3, error=NULL, "
            "updated_at=now() WHERE id=$1 AND tenant_id=$2", outbox_id, tenant_id, result.reference)
        # Contact happened now, not when it was typed.
        await conn.execute("UPDATE clients SET last_contacted_at=now() WHERE id=$1 AND tenant_id=$2",
                           row["client_id"], tenant_id)
    return {"status": "sent"}


async def _finish(tenant_id: str, outbox_id: str, status: str, *, error: str) -> None:
    ctx = TenantContext(agent_id="email-outbox", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        await conn.execute("UPDATE email_outbox SET status=$3, error=$4, updated_at=now() "
                           "WHERE id=$1 AND tenant_id=$2", outbox_id, tenant_id, status, error)


async def _job(payload: dict, reporter) -> dict:
    # Job handlers receive the payload; the job row (and tenant) is reporter.job.
    return await deliver(str(reporter.job["tenant_id"]), str(payload["outbox_id"]))


def register() -> None:
    from automation_jobs import register_handler

    register_handler(JOB_TYPE, _job)
