"""Tenant-safe messaging (SMS/MMS) data layer — the Telnyx counterpart of
inbound_voice.py for Plivo voice.

Same invariants as the voice side:
  * A route's hosted-messaging/verification state can only ever be changed by
    a function here that just asked the provider directly — never by a
    client-supplied field.
  * The public business number is never duplicated. Every function below
    resolves it by joining telephony_routes on (tenant_id, agent_id) —
    losing that join means messaging has nothing to route on, by design.
  * Inbound webhook resolution never trusts a caller-supplied tenant/agent —
    only the destination number, matched against the DB.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from db.connection import tenant_tx
from tenancy import Role, TenantContext

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_MAX_VERIFICATION_ATTEMPTS = 5
_VERIFICATION_LOCKOUT_SECONDS = 900

_ROUTE_COLUMNS = (
    "id,tenant_id,agent_id,provider,provider_account_id,messaging_profile_id,"
    "eligibility_status,eligibility_checked_at,eligibility_detail,"
    "hosted_order_id,hosted_order_status,hosted_order_failure_reason,"
    "verification_method,loa_document_state,invoice_document_state,"
    "campaign_id,active,disconnected_at"
)


class MessagingDataError(RuntimeError):
    """Safe messaging-data failure; callers should not expose internal details."""


def _platform_context() -> TenantContext:
    return TenantContext(
        agent_id="telnyx-messaging-webhook",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )


def _tenant_context(tenant_id: str, agent_id: str) -> TenantContext:
    return TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.AGENT)


async def get_public_business_number(ctx: TenantContext) -> Optional[dict[str, Any]]:
    """The agent's voice-verified public number, or None.

    Messaging can only ever be configured for a number that has already
    passed voice's server-side Verified Caller ID flow — this is the join
    that enforces "reuse the number, never a second public identity" from
    the migration brief.
    """
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT voice_caller_id_e164,voice_caller_id_verified,provider AS voice_provider
              FROM telephony_routes
             WHERE tenant_id=$1::uuid AND agent_id=$2 AND active=true
             LIMIT 1
            """,
            ctx.tenant_id,
            ctx.agent_id,
        )
    if row is None or not row["voice_caller_id_verified"] or not row["voice_caller_id_e164"]:
        return None
    return dict(row)


async def get_messaging_route(ctx: TenantContext) -> Optional[dict[str, Any]]:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_ROUTE_COLUMNS},created_at,updated_at
              FROM messaging_routes
             WHERE tenant_id=$1::uuid AND agent_id=$2
             LIMIT 1
            """,
            ctx.tenant_id,
            ctx.agent_id,
        )
    return dict(row) if row is not None else None


async def ensure_messaging_route(ctx: TenantContext, *, provider: str, provider_account_id: str) -> dict[str, Any]:
    """Idempotent: create the route row on first use, otherwise return it
    unchanged. Never overwrites an existing row's provider — switching
    messaging providers is a disconnect+reconnect, not an implicit update."""
    existing = await get_messaging_route(ctx)
    if existing is not None:
        return existing
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO messaging_routes (tenant_id,agent_id,provider,provider_account_id)
                 VALUES ($1::uuid,$2,$3,$4)
            ON CONFLICT (tenant_id,agent_id) DO UPDATE SET provider_account_id=EXCLUDED.provider_account_id
            RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            provider,
            provider_account_id,
        )
    return dict(row)


async def _set_route_columns(ctx: TenantContext, columns: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if not columns:
        return await get_messaging_route(ctx)
    set_clause = ",".join(f"{name}=${i + 3}" for i, name in enumerate(columns))
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE messaging_routes
               SET {set_clause}, updated_at=now()
             WHERE tenant_id=$1::uuid AND agent_id=$2
             RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            *columns.values(),
        )
    return dict(row) if row is not None else None


async def check_eligibility(ctx: TenantContext, *, credentials: Mapping[str, Any]) -> dict[str, Any]:
    from messaging_provider import ELIGIBILITY_NEEDS_REVIEW, get_messaging_provider

    business_number = await get_public_business_number(ctx)
    if business_number is None:
        raise MessagingDataError(
            "Connect and verify your business number for calls before setting up text messages"
        )
    number = str(business_number["voice_caller_id_e164"])
    provider_name = str((await get_messaging_route(ctx) or {}).get("provider") or _default_provider())
    route = await ensure_messaging_route(
        ctx, provider=provider_name, provider_account_id=str(business_number.get("voice_provider") or "")
    )

    adapter = get_messaging_provider(route["provider"])
    try:
        results = await adapter.check_number_eligibility([number], credentials=credentials)
    except Exception as exc:
        return await _set_route_columns(
            ctx,
            {
                "eligibility_status": ELIGIBILITY_NEEDS_REVIEW,
                "eligibility_checked_at": datetime.now(timezone.utc),
                "eligibility_detail": str(exc)[:500],
            },
        ) or route
    result = next((r for r in results if r.phone_number == number), None)
    status = result.status if result else ELIGIBILITY_NEEDS_REVIEW
    detail = result.detail if result else "provider returned no result for this number"
    return await _set_route_columns(
        ctx,
        {
            "eligibility_status": status,
            "eligibility_checked_at": datetime.now(timezone.utc),
            "eligibility_detail": detail[:500],
        },
    ) or route


def _default_provider() -> str:
    from messaging_provider import messaging_provider_name

    return messaging_provider_name()


async def connect_messaging(
    ctx: TenantContext,
    *,
    provider_account_id: str,
    credentials: Mapping[str, Any],
) -> dict[str, Any]:
    """Submit the hosted-messaging order and start OTP ownership verification.

    Idempotent: reuses an existing hosted_order_id rather than submitting a
    second order for the same route.
    """
    from messaging_provider import ELIGIBILITY_ELIGIBLE, get_messaging_provider

    business_number = await get_public_business_number(ctx)
    if business_number is None:
        raise MessagingDataError("Connect and verify your business number for calls first")
    number = str(business_number["voice_caller_id_e164"])

    existing = await get_messaging_route(ctx)
    if existing is None or existing.get("eligibility_status") != ELIGIBILITY_ELIGIBLE:
        raise MessagingDataError("Check number eligibility before connecting text messages")
    if existing.get("hosted_order_id"):
        return existing

    provider_name = str(existing.get("provider") or _default_provider())
    adapter = get_messaging_provider(provider_name)

    profile_id = existing.get("messaging_profile_id")
    order = await adapter.begin_hosted_messaging(
        number, messaging_profile_id=profile_id, credentials=credentials
    )
    updates: dict[str, Any] = {
        "hosted_order_id": order.reference,
        "hosted_order_status": "pending_verification",
        "verification_method": "sms",
    }
    try:
        await adapter.start_ownership_verification(
            order.reference, number, method="sms", credentials=credentials
        )
    except Exception as exc:
        updates["hosted_order_status"] = "manual_action_required"
        updates["hosted_order_failure_reason"] = str(exc)[:500]
    return await _set_route_columns(ctx, updates) or existing


async def complete_messaging_verification(
    ctx: TenantContext, code: str, *, credentials: Mapping[str, Any]
) -> dict[str, Any]:
    from messaging_provider import get_messaging_provider

    route = await get_messaging_route(ctx)
    if route is None or not route.get("hosted_order_id"):
        raise MessagingDataError("No pending text-message connection to verify")
    if route.get("hosted_order_status") == "active":
        return route

    business_number = await get_public_business_number(ctx)
    if business_number is None:
        raise MessagingDataError("Business number is no longer connected")
    number = str(business_number["voice_caller_id_e164"])

    adapter = get_messaging_provider(str(route["provider"]))
    verified = await adapter.complete_ownership_verification(
        str(route["hosted_order_id"]), number, code, credentials=credentials
    )
    if not verified:
        raise MessagingDataError("That code did not verify — check it and try again")
    return await _set_route_columns(
        ctx,
        {
            "hosted_order_status": "processing",
            "hosted_order_failure_reason": None,
        },
    ) or route


async def refresh_hosted_order_status(ctx: TenantContext, *, credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Poll Telnyx for whether a submitted hosted order has finished
    processing. Never marks a route 'active' from anywhere but this."""
    from messaging_provider import get_messaging_provider

    route = await get_messaging_route(ctx)
    if route is None or not route.get("hosted_order_id"):
        raise MessagingDataError("No hosted-messaging order to check")
    if route.get("hosted_order_status") in ("active", "disconnected"):
        return route

    adapter = get_messaging_provider(str(route["provider"]))
    status = await adapter.get_hosted_order_status(str(route["hosted_order_id"]), credentials=credentials)
    provider_status = str(status.get("status") or "")
    mapped = {
        "successful": "active",
        "loa_file_successful": "active",
        "pending": "processing",
        "provisioning": "processing",
        "incomplete_documentation": "pending_documents",
        "loa_file_invalid": "pending_documents",
        "incorrect_billing_information": "manual_action_required",
        "carrier_rejected": "failed",
        "compliance_review_failed": "failed",
        "ineligible_carrier": "failed",
        "failed": "failed",
        "deleted": "disconnected",
    }.get(provider_status, "processing")
    updates: dict[str, Any] = {"hosted_order_status": mapped}
    if mapped == "pending_documents":
        updates["loa_document_state"] = "required"
        updates["invoice_document_state"] = "required"
    if mapped == "failed":
        updates["hosted_order_failure_reason"] = provider_status
    return await _set_route_columns(ctx, updates) or route


async def record_hosted_document(
    ctx: TenantContext,
    *,
    doc_type: str,
    storage_key: str,
    sha256: str,
    content_type: str,
    uploaded_by: str,
) -> dict[str, Any]:
    if doc_type not in ("loa", "invoice"):
        raise MessagingDataError("doc_type must be loa or invoice")
    route = await get_messaging_route(ctx)
    if route is None:
        raise MessagingDataError("No messaging route to attach a document to")
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            INSERT INTO messaging_hosted_documents
                (tenant_id,messaging_route_id,doc_type,storage_key,sha256,content_type,uploaded_by)
            VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7)
            """,
            ctx.tenant_id,
            route["id"],
            doc_type,
            storage_key,
            sha256,
            content_type,
            uploaded_by,
        )
    state_field = f"{doc_type}_document_state"
    return await _set_route_columns(ctx, {state_field: "uploaded"}) or route


async def disconnect_messaging(ctx: TenantContext, *, credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Disable outbound/inbound routing without destroying history — the
    message log and CRM activity rows are untouched."""
    from messaging_provider import get_messaging_provider

    route = await get_messaging_route(ctx)
    if route is None:
        raise MessagingDataError("No text-message connection to disconnect")
    business_number = await get_public_business_number(ctx)
    if business_number is not None and route.get("hosted_order_id"):
        adapter = get_messaging_provider(str(route["provider"]))
        try:
            await adapter.disconnect_hosted_number(
                str(route["hosted_order_id"]),
                str(business_number["voice_caller_id_e164"]),
                credentials=credentials,
            )
        except Exception:
            pass  # best-effort — Neoh-side disconnect still proceeds
    return await _set_route_columns(
        ctx,
        {
            "active": False,
            "hosted_order_status": "disconnected",
            "disconnected_at": datetime.now(timezone.utc),
        },
    ) or route


# ── Inbound routing: never trust a caller-supplied tenant/agent ────────────


async def resolve_messaging_route_by_number(to_e164: str) -> Optional[dict[str, Any]]:
    """The only trusted way to route an inbound SMS/MMS: match the dialed
    public business number against telephony_routes, then require an active
    messaging_routes row for that same tenant+agent. A number with a voice
    route but no completed messaging connection resolves to None — the
    webhook fails closed rather than guessing."""
    try:
        to_normalized = str(to_e164 or "").strip()
    except (TypeError, ValueError):
        return None
    if not _E164_RE.fullmatch(to_normalized):
        return None
    async with tenant_tx(_platform_context()) as conn:
        row = await conn.fetchrow(
            """
            SELECT m.id AS messaging_route_id, m.tenant_id, m.agent_id, m.provider,
                   m.hosted_order_status, t.voice_caller_id_e164
              FROM telephony_routes t
              JOIN messaging_routes m ON m.tenant_id=t.tenant_id AND m.agent_id=t.agent_id
             WHERE t.voice_caller_id_e164=$1
               AND t.voice_caller_id_verified=true
               AND t.active=true
               AND m.active=true
               AND m.hosted_order_status='active'
             LIMIT 1
            """,
            to_normalized,
        )
    return dict(row) if row is not None else None


# ── Message log + CRM timeline ──────────────────────────────────────────────


async def _match_contact(conn: Any, *, tenant_id: str, lookup_digest: str) -> tuple[Optional[str], Optional[str]]:
    row = await conn.fetchrow(
        """
        SELECT ac.id AS contact_id,
               COALESCE(ac.legacy_client_id, matched_client.id) AS client_id
          FROM agent_contacts ac
          LEFT JOIN LATERAL (
              SELECT c.id
                FROM clients c
               WHERE c.tenant_id=ac.tenant_id
                 AND c.contact_id=ac.id
                 AND c.archived_at IS NULL
               ORDER BY c.created_at ASC
               LIMIT 1
          ) matched_client ON true
         WHERE ac.tenant_id=$1::uuid
           AND ac.phone_lookup_hash=$2
           AND ac.deleted_at IS NULL
         ORDER BY ac.created_at ASC
         LIMIT 1
        """,
        tenant_id,
        lookup_digest,
    )
    if row is None:
        return None, None
    return str(row["contact_id"]), (str(row["client_id"]) if row["client_id"] else None)


async def record_inbound_message(
    *,
    tenant_id: str,
    agent_id: str,
    provider: str,
    provider_message_id: str,
    from_e164: str,
    to_e164: str,
    text: str,
    media: Sequence[Mapping[str, str]],
    opted_out: bool,
) -> str:
    """Idempotent on (provider, provider_message_id) — a retried webhook
    converges on the same row rather than duplicating the message or the
    CRM activity (brief section 27)."""
    from contact_truth import lookup_hash, normalize_phone

    ctx = _tenant_context(tenant_id, agent_id)
    normalized_from = normalize_phone(from_e164) or from_e164
    lookup_digest = lookup_hash(tenant_id, "phone", normalized_from)
    async with tenant_tx(ctx) as conn:
        contact_id, client_id = await _match_contact(conn, tenant_id=tenant_id, lookup_digest=lookup_digest)
        row = await conn.fetchrow(
            """
            INSERT INTO sms_messages (
                tenant_id,agent_id,contact_id,client_id,direction,provider,
                provider_message_id,provider_status,status,from_e164,to_e164,
                body,media,opt_out_event
            ) VALUES (
                $1::uuid,$2,$3::uuid,$4::uuid,'inbound',$5,$6,'received','delivered',
                $7,$8,$9,$10::jsonb,$11
            )
            ON CONFLICT (provider,provider_message_id) DO NOTHING
            RETURNING id,activity_id
            """,
            tenant_id,
            agent_id,
            contact_id,
            client_id,
            provider,
            provider_message_id,
            normalized_from,
            to_e164,
            text[:4_000],
            _json_media(media),
            opted_out,
        )
        if row is None:
            existing = await conn.fetchrow(
                "SELECT id,activity_id FROM sms_messages WHERE provider=$1 AND provider_message_id=$2",
                provider,
                provider_message_id,
            )
            return str(existing["id"]) if existing else ""

        message_id = str(row["id"])
        if client_id and row["activity_id"] is None:
            summary = "Opted out via text message" if opted_out else "Text message received"
            activity = await conn.fetchrow(
                """
                INSERT INTO client_activities (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'message',$3,
                             jsonb_build_object('direction','inbound','sms_message_id',$4::text),
                             'sms-inbound')
                     RETURNING id
                """,
                tenant_id,
                client_id,
                summary,
                message_id,
            )
            await conn.execute(
                "UPDATE sms_messages SET activity_id=$2::uuid WHERE id=$1::uuid",
                message_id,
                activity["id"],
            )
        return message_id


def _json_media(media: Sequence[Mapping[str, str]]) -> str:
    import json

    return json.dumps(list(media)[:10], separators=(",", ":"))


async def record_outbound_message(
    *,
    tenant_id: str,
    agent_id: str,
    contact_id: Optional[str],
    client_id: Optional[str],
    provider: str,
    provider_message_id: str,
    from_e164: str,
    to_e164: str,
    body: str,
) -> None:
    ctx = _tenant_context(tenant_id, agent_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sms_messages (
                tenant_id,agent_id,contact_id,client_id,direction,provider,
                provider_message_id,status,from_e164,to_e164,body
            ) VALUES ($1::uuid,$2,$3::uuid,$4::uuid,'outbound',$5,$6,'queued',$7,$8,$9)
            ON CONFLICT (provider,provider_message_id) DO NOTHING
            RETURNING id
            """,
            tenant_id,
            agent_id,
            contact_id,
            client_id,
            provider,
            provider_message_id,
            from_e164,
            to_e164,
            body[:4_000],
        )
        if row is not None and client_id:
            activity = await conn.fetchrow(
                """
                INSERT INTO client_activities (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'message','Text message sent',
                             jsonb_build_object('direction','outbound','sms_message_id',$3::text),$4)
                     RETURNING id
                """,
                tenant_id,
                client_id,
                str(row["id"]),
                agent_id,
            )
            await conn.execute(
                "UPDATE sms_messages SET activity_id=$2::uuid WHERE id=$1::uuid",
                row["id"],
                activity["id"],
            )


async def record_delivery_update(*, provider: str, provider_message_id: str, status: str, error_reason: Optional[str]) -> None:
    """Idempotent status update — applies to whichever row already exists;
    a duplicate/out-of-order webhook just re-applies the same values."""
    async with tenant_tx(_platform_context()) as conn:
        await conn.execute(
            """
            UPDATE sms_messages
               SET status=$3,
                   provider_status=$3,
                   error_reason=$4,
                   delivered_at=CASE WHEN $3='delivered' THEN COALESCE(delivered_at,now()) ELSE delivered_at END,
                   failed_at=CASE WHEN $3 IN ('failed','undelivered') THEN COALESCE(failed_at,now()) ELSE failed_at END
             WHERE provider=$1 AND provider_message_id=$2
            """,
            provider,
            provider_message_id,
            status,
            error_reason,
        )
