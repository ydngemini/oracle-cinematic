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

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from db.connection import tenant_tx
from tenancy import Role, TenantContext, brokerage_scope

logger = logging.getLogger("oracle.messaging_data")

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
    # Matching a phone number to a client is a brokerage function: the message
    # or call belongs on the client's record whoever's line it came in on (0131).
    return brokerage_scope(TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.AGENT))


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
    unchanged. Never overwrites an existing row's provider or provider
    account — switching messaging providers is a disconnect+reconnect, not an
    implicit update (audit A07, L10-06).

    Race-safe: racing first uses all insert, the first commit wins, and every
    caller gets that one row back. The losers' INSERT waits on the winner's
    unique key, does nothing, and the statement after it (READ COMMITTED: a
    fresh snapshot) reads the committed row."""
    existing = await get_messaging_route(ctx)
    if existing is not None:
        return _kept(existing, provider, provider_account_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO messaging_routes (tenant_id,agent_id,provider,provider_account_id)
                 VALUES ($1::uuid,$2,$3,$4)
            ON CONFLICT (tenant_id,agent_id) DO NOTHING
            RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            provider,
            provider_account_id,
        )
        if row is not None:
            return dict(row)
        row = await conn.fetchrow(
            f"""
            SELECT {_ROUTE_COLUMNS},created_at,updated_at
              FROM messaging_routes
             WHERE tenant_id=$1::uuid AND agent_id=$2
            """,
            ctx.tenant_id,
            ctx.agent_id,
        )
    if row is None:
        # The conflicting row is not visible to this context: never guess.
        raise MessagingDataError("Text-message setup is unavailable for this account right now")
    return _kept(dict(row), provider, provider_account_id)


def _kept(route: dict[str, Any], provider: str, provider_account_id: str) -> dict[str, Any]:
    if (route.get("provider"), route.get("provider_account_id")) != (provider, provider_account_id):
        # Expected after a race or a provider change in config; never applied.
        logger.info("messaging route %s kept its provider/account (asked for another)", route.get("id"))
    return route


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

    At most once per route, however many requests race (audit A07, L10-06):
    the order goes through the provider side-effect ledger (provider_purchases,
    kind 'hosted_messaging_order', 0136). The intent is written before the
    provider is asked and only one may be in flight per route; the caller that
    attaches the order to the route is the only one that sends the
    verification text. A route that already has an order is returned as is.
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
    route, purchase_id, order_ref = await _claim_hosted_order(ctx, provider_name)
    if purchase_id is None:
        return route  # another request attached an order meanwhile
    adapter = get_messaging_provider(provider_name)
    if order_ref is None:
        order_ref = await _submit_hosted_order(
            ctx, adapter, purchase_id, number,
            messaging_profile_id=route.get("messaging_profile_id"), credentials=credentials,
        )

    attached = await _attach_hosted_order(ctx, purchase_id, order_ref)
    if attached is None:
        return await get_messaging_route(ctx) or route
    try:
        await adapter.start_ownership_verification(
            order_ref, number, method="sms", credentials=credentials
        )
    except Exception as exc:
        return await _set_route_columns(
            ctx,
            {"hosted_order_status": "manual_action_required", "hosted_order_failure_reason": str(exc)[:500]},
        ) or attached
    return attached


async def _claim_hosted_order(
    ctx: TenantContext, provider: str
) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    """(route, purchase_id, reusable_order_ref) under the route's row lock.

    purchase_id None: the route already has an order. reusable_order_ref set:
    an earlier attempt's order was accepted but never reached the route (a
    crash in between), so it is attached instead of submitting another.
    Raises MessagingDataError while another submission is in flight or its
    outcome is unknown — the ledger's partial unique index decides that.
    """
    import asyncpg

    import provider_purchases

    try:
        async with tenant_tx(ctx) as conn:
            # The lock orders this claim after any attach already committing,
            # so a claim never starts beside an order that just landed.
            route = await conn.fetchrow(
                f"""
                SELECT {_ROUTE_COLUMNS},created_at,updated_at
                  FROM messaging_routes
                 WHERE tenant_id=$1::uuid AND agent_id=$2
                   FOR UPDATE
                """,
                ctx.tenant_id,
                ctx.agent_id,
            )
            if route is None:
                raise MessagingDataError("Check number eligibility before connecting text messages")
            if route["hosted_order_id"]:
                return dict(route), None, None
            reusable = await conn.fetchrow(
                "SELECT id, provider_ref FROM provider_purchases WHERE tenant_id=$1::uuid "
                "AND lower(agent_id)=lower($2) AND provider=$3 AND kind=$4 AND state='confirmed' "
                "ORDER BY created_at DESC LIMIT 1",
                ctx.tenant_id,
                ctx.agent_id,
                provider,
                provider_purchases.HOSTED_MESSAGING_ORDER,
            )
            if reusable is not None and reusable["provider_ref"]:
                return dict(route), str(reusable["id"]), str(reusable["provider_ref"])
            purchase_id = await conn.fetchval(
                "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind) "
                "VALUES ($1::uuid,$2,$3,$4) RETURNING id",
                ctx.tenant_id,
                ctx.agent_id,
                provider,
                provider_purchases.HOSTED_MESSAGING_ORDER,
            )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise MessagingDataError(
            "Text messages for this number are already being connected, or the last attempt is "
            "still being checked. Nothing new was submitted — try again in a few minutes."
        ) from exc
    return dict(route), str(purchase_id), None


async def _submit_hosted_order(
    ctx: TenantContext,
    adapter: Any,
    purchase_id: str,
    number: str,
    *,
    messaging_profile_id: Optional[str],
    credentials: Mapping[str, Any],
) -> str:
    """Ask the provider once and record the outcome on the ledger row.

    A definite refusal frees the route for a retry. Anything ambiguous (a
    timeout, a lost or malformed answer) leaves the row `unknown`, which
    blocks another submission until an operator checks the provider."""
    import provider_purchases
    import recovery_mode
    from command_providers import ProviderConfigurationError, ProviderRejectedError
    from messaging_provider import MessagingProviderError

    try:
        order = await adapter.begin_hosted_messaging(
            number, messaging_profile_id=messaging_profile_id, credentials=credentials
        )
    except (
        ProviderConfigurationError,
        ProviderRejectedError,
        MessagingProviderError,
        NotImplementedError,
        recovery_mode.RecoveryModeBlocked,
    ) as exc:
        await provider_purchases.failed(ctx, purchase_id, f"{type(exc).__name__}: {exc}")
        raise
    except BaseException as exc:
        await provider_purchases.unknown(ctx, purchase_id, f"{type(exc).__name__}: {exc}"[:500])
        if not isinstance(exc, Exception):
            raise
        raise MessagingDataError(
            "We could not confirm the text-message order with the provider. Nothing more will be "
            "submitted for this number until it has been checked."
        ) from exc
    await provider_purchases.confirmed(ctx, purchase_id, order.reference, number)
    return str(order.reference)


async def _attach_hosted_order(ctx: TenantContext, purchase_id: str, order_ref: str) -> Optional[dict[str, Any]]:
    """Put the order on the route and close its ledger row in one transaction.
    None if the route already carries an order (the ledger row is left
    `confirmed` for an operator rather than claimed by the wrong route)."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE messaging_routes
               SET hosted_order_id=$3, hosted_order_status='pending_verification',
                   verification_method='sms', hosted_order_failure_reason=NULL, updated_at=now()
             WHERE tenant_id=$1::uuid AND agent_id=$2 AND hosted_order_id IS NULL
             RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            order_ref,
        )
        if row is None:
            logger.warning("hosted order %s not attached: route already has an order", purchase_id)
            return None
        await conn.execute(
            "UPDATE provider_purchases SET state='attached', provider_ref=$2, updated_at=now() WHERE id=$1::uuid",
            purchase_id,
            order_ref,
        )
    return dict(row)


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
    provider_status = "not_hosted"
    if business_number is not None and route.get("hosted_order_id"):
        adapter = get_messaging_provider(str(route["provider"]))
        try:
            provider_status = await adapter.disconnect_hosted_number(
                str(route["hosted_order_id"]),
                str(business_number["voice_caller_id_e164"]),
                credentials=credentials,
            ) or "released"
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            # Neoh stops routing either way, but the route must not claim the
            # provider let go of the number when it did not.
            logger.warning("hosted number release failed: %s", type(exc).__name__)
            provider_status = "release_failed"
    updated = await _set_route_columns(
        ctx,
        {
            "active": False,
            "hosted_order_status": "disconnected" if provider_status != "release_failed" else "manual_action_required",
            "disconnected_at": datetime.now(timezone.utc),
        },
    ) or route
    return {**updated, "provider_release": provider_status}


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


async def resolve_opt_out_owners_by_number(to_e164: str) -> list[dict[str, Any]]:
    """Every tenant/agent that has held this business number, active or not.

    Used only for a STOP sent to a number with no active route: a disconnected,
    suspended or offboarded agent's number. Dropping that STOP (the old
    behaviour) meant the person stayed contactable the moment the number was
    reconnected or reassigned. An opt-out is recorded for each holder — over-
    suppressing is the safe direction. Erased tenants are excluded (nothing of
    theirs is contactable any more).
    """
    to_normalized = str(to_e164 or "").strip()
    if not _E164_RE.fullmatch(to_normalized):
        return []
    async with tenant_tx(_platform_context()) as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT t.tenant_id, t.agent_id
              FROM telephony_routes t
              JOIN tenants tn ON tn.id = t.tenant_id
             WHERE (t.voice_caller_id_e164 = $1 OR t.sms_sender_e164 = $1)
               AND tn.lifecycle_state <> 'erased'
            """,
            to_normalized,
        )
    return [dict(r) for r in rows]


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




async def record_delivery_update(*, provider: str, provider_message_id: str, status: str,
                                 error_reason: Optional[str]) -> bool:
    """Monotonic status update. Returns False only when no such message row
    exists yet (the receipt beat our own insert) so the webhook can ask the
    provider to redeliver.

    Delivery receipts arrive late, twice and out of order. A late
    `message.sent` used to downgrade `delivered`; now status only moves
    forward (queued → sent → delivered/failed) and a terminal state is final.
    """
    async with tenant_tx(_platform_context()) as conn:
        result = await conn.execute(
            """
            UPDATE sms_messages
               SET status=$3,
                   provider_status=$3,
                   error_reason=COALESCE($4, error_reason),
                   delivered_at=CASE WHEN $3='delivered' THEN COALESCE(delivered_at,now()) ELSE delivered_at END,
                   failed_at=CASE WHEN $3 IN ('failed','undelivered') THEN COALESCE(failed_at,now()) ELSE failed_at END
             WHERE provider=$1 AND provider_message_id=$2
               AND (CASE $3 WHEN 'queued' THEN 0 WHEN 'sent' THEN 1 ELSE 2 END)
                 > (CASE status WHEN 'queued' THEN 0 WHEN 'sent' THEN 1 ELSE 2 END)
            """,
            provider,
            provider_message_id,
            status,
            error_reason,
        )
        if str(result).endswith(" 0"):
            exists = await conn.fetchval(
                "SELECT 1 FROM sms_messages WHERE provider=$1 AND provider_message_id=$2",
                provider, provider_message_id)
            return bool(exists)
        return True
