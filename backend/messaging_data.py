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
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import pan_redaction
import sealed_text
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


def account_fingerprint(credentials: Optional[Mapping[str, Any]]) -> Optional[str]:
    """An opaque id of the Telnyx account a credential acts as (25.1): the
    same key resolution the adapter uses (the credential, else the platform
    key), hashed — never the key or a prefix of it."""
    import hashlib

    key = str((credentials or {}).get("api_key") or os.getenv("TELNYX_API_KEY", "")).strip()
    return "tk:" + hashlib.sha256(key.encode()).hexdigest()[:16] if key else None


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

    attached = await _attach_hosted_order(ctx, purchase_id, order_ref, account_fingerprint(credentials))
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


async def _attach_hosted_order(ctx: TenantContext, purchase_id: str, order_ref: str,
                               account: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Put the order on the route and close its ledger row in one transaction.
    None if the route already carries an order (the ledger row is left
    `confirmed` for an operator rather than claimed by the wrong route)."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE messaging_routes
               SET hosted_order_id=$3, hosted_order_status='pending_verification',
                   verification_method='sms', hosted_order_failure_reason=NULL, updated_at=now(),
                   verification_attempt_count=0, verification_locked_until=NULL,
                   verification_attempted_at=NULL,
                   -- the account that holds this hosting (25.1 send gate)
                   provider_account_id=COALESCE($4, provider_account_id)
             WHERE tenant_id=$1::uuid AND agent_id=$2 AND hosted_order_id IS NULL
             RETURNING {_ROUTE_COLUMNS},created_at,updated_at
            """,
            ctx.tenant_id,
            ctx.agent_id,
            order_ref,
            account,
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


VERIFICATION_LOCKED = "Too many incorrect codes — try again in 15 minutes."
# Reserve one ownership-code attempt (25.7). A lock that has passed starts a
# new count; at the limit, or while locked, nothing is reserved (NULL).
# The count restarts when a lock has passed or the last attempt is older than
# the lockout window (attempts expire). The attempt that reaches the limit
# sets the lock in the same statement, so no later write is needed (a request
# that dies mid-flight cannot leave the route locked forever).
_RESERVE_VERIFICATION_ATTEMPT_SQL = """
    WITH fresh AS (
        SELECT id,
               (verification_locked_until IS NOT NULL AND verification_locked_until <= now())
            OR (verification_attempted_at IS NULL
                OR verification_attempted_at < now() - make_interval(secs => $4)) AS restart
          FROM messaging_routes
         WHERE tenant_id = $1::uuid AND agent_id = $2
           AND (verification_locked_until IS NULL OR verification_locked_until <= now())
           FOR UPDATE
    )
    UPDATE messaging_routes r
       SET verification_attempt_count = CASE WHEN f.restart THEN 1 ELSE r.verification_attempt_count + 1 END,
           verification_attempted_at = now(),
           verification_locked_until = CASE
               WHEN (CASE WHEN f.restart THEN 1 ELSE r.verification_attempt_count + 1 END) >= $3
               THEN now() + make_interval(secs => $4) ELSE NULL END,
           updated_at = now()
      FROM fresh f
     WHERE r.id = f.id AND (f.restart OR r.verification_attempt_count < $3)
    RETURNING r.verification_attempt_count"""


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

    # 25.7: one attempt reserved atomically BEFORE the provider is asked, so
    # concurrent submissions cannot exceed the limit; at the limit the route
    # is locked for _VERIFICATION_LOCKOUT_SECONDS. The code itself is never
    # stored, and a wrong code says nothing about how close it was.
    async with tenant_tx(ctx) as conn:
        attempt = await conn.fetchval(_RESERVE_VERIFICATION_ATTEMPT_SQL, ctx.tenant_id, ctx.agent_id,
                                      _MAX_VERIFICATION_ATTEMPTS, float(_VERIFICATION_LOCKOUT_SECONDS))
    if attempt is None:
        raise MessagingDataError(VERIFICATION_LOCKED)

    adapter = get_messaging_provider(str(route["provider"]))
    try:
        verified = await adapter.complete_ownership_verification(
            str(route["hosted_order_id"]), number, code, credentials=credentials
        )
    except Exception as exc:
        # The provider failed, not the code: the attempt is given back (and a
        # lock it set is lifted). Not for a timeout: the request may still
        # have reached the provider, so it counts.
        if not isinstance(exc, TimeoutError):
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    "UPDATE messaging_routes SET verification_attempt_count = GREATEST(verification_attempt_count - 1, 0), "
                    "verification_locked_until = CASE WHEN verification_attempt_count >= $3 THEN NULL "
                    "ELSE verification_locked_until END "
                    "WHERE tenant_id=$1::uuid AND agent_id=$2", ctx.tenant_id, ctx.agent_id, _MAX_VERIFICATION_ATTEMPTS)
        raise
    if not verified:
        if attempt >= _MAX_VERIFICATION_ATTEMPTS:
            raise MessagingDataError(VERIFICATION_LOCKED)
        raise MessagingDataError("That code did not verify — check it and try again")
    return await _set_route_columns(
        ctx,
        {
            "hosted_order_status": "processing",
            "hosted_order_failure_reason": None,
            "verification_attempt_count": 0,
            "verification_locked_until": None,
        },
    ) or route


async def refresh_hosted_order_status(ctx: TenantContext, *, credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Poll Telnyx for whether a submitted hosted order has finished
    processing. Never marks a route 'active' from anywhere but this."""
    from messaging_provider import get_messaging_provider

    route = await get_messaging_route(ctx)
    if route is None or not route.get("hosted_order_id"):
        raise MessagingDataError("No hosted-messaging order to check")
    account = account_fingerprint(credentials)
    if route.get("hosted_order_status") in ("active", "disconnected") and route.get("provider_account_id") == account:
        return route

    # Also the upgrade path for a route hosted before 25.1 recorded the
    # account: reading the order with this credential proves the account
    # holds it.
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
    if route.get("hosted_order_status") == "pending_verification" and mapped == "processing":
        # Before the owner has entered the ownership code the provider can only
        # answer pending (or something unknown): that is not "processing", and
        # moving there would hide the code form (25.6 review).
        mapped = "pending_verification"
    updates: dict[str, Any] = {"hosted_order_status": mapped, "hosted_order_checked_at": datetime.now(timezone.utc)}
    if account:
        updates["provider_account_id"] = account
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


# ── 10DLC registration: one brand and one campaign per brokerage ───────────
#
# Each registration costs money at the carrier, and no adapter can update a
# brand or campaign in place yet, so (L10-09): a submission when one already
# exists at the provider is refused; racing submissions — from one broker
# owner or several — reach the provider once (the intent is claimed under the
# brokerage's brand-row lock, through provider_purchases, 0139); an ambiguous
# outcome is `unknown` and blocks resubmission until checked; a definite
# refusal allows a retry; an accepted-but-unrecorded one is reused.

BRAND_ALREADY_REGISTERED = "A brand is already registered for this number; contact support to change it."
CAMPAIGN_ALREADY_REGISTERED = "A campaign is already registered for this number; contact support to change it."
_REGISTRATION_IN_FLIGHT = (
    "A {what} registration for your brokerage is already being submitted, or the last attempt is "
    "still being checked. Nothing new was submitted — try again in a few minutes."
)
_REGISTRATION_UNCONFIRMED = (
    "We could not confirm the {what} registration with the provider. Nothing more will be "
    "submitted for your brokerage until it has been checked."
)
_BRAND_FIELDS = (
    "display_name", "company_name", "ein", "entity_type", "vertical", "email", "phone",
    "street", "city", "state", "postal_code", "country", "website",
)
# The fields above, in that order, as $3..$15 (a literal statement: no SQL is
# assembled at run time; test_record_brand_intent_matches_the_field_list pins it).
_RECORD_BRAND_INTENT_SQL = """
    UPDATE tenant_messaging_brands
       SET provider=$2, status='not_started', failure_reason=NULL,
           display_name=$3, company_name=$4, ein=$5, entity_type=$6, vertical=$7,
           email=$8, phone=$9, street=$10, city=$11, state=$12, postal_code=$13,
           country=$14, website=$15
     WHERE tenant_id=$1::uuid"""
_BRAND_STATUSES = frozenset({"pending", "unverified", "verified", "failed"})
_CAMPAIGN_STATUSES = frozenset({"pending", "approved", "rejected", "failed"})


class RegistrationConflict(MessagingDataError):
    """A brand/campaign already exists at the provider, or a submission is unresolved."""


class RegistrationUnconfirmed(MessagingDataError):
    """The provider may have registered it; nothing more is submitted until checked."""


def _registration_status(raw: Any, allowed: frozenset[str]) -> str:
    """The provider's status if it is one this schema knows, else 'pending' —
    never a guess upward (an unrecognised value is not 'verified')."""
    value = str(raw or "").strip().lower()
    return value if value in allowed else "pending"


def _json_detail(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, str):
        return json.loads(raw or "{}")
    return dict(raw or {})


async def _claim_registration(
    conn: Any, ctx: TenantContext, provider: str, kind: str, what: str, detail: Mapping[str, Any]
) -> tuple[str, Optional[str], dict[str, Any]]:
    """(purchase_id, reusable_ref, detail). The caller holds the brand row's
    lock, which serialises claims across every agent of the brokerage; the
    open rows are read brokerage-wide for that reason (the ledger's unique
    in-flight index is per agent)."""
    import json

    import asyncpg

    open_rows = await conn.fetch(
        "SELECT id, state, provider_ref, detail FROM provider_purchases WHERE tenant_id=$1::uuid "
        "AND provider=$2 AND kind=$3 AND state IN ('intended','unknown','confirmed') ORDER BY created_at DESC",
        ctx.tenant_id,
        provider,
        kind,
    )
    if any(row["state"] != "confirmed" or not row["provider_ref"] for row in open_rows):
        raise RegistrationConflict(_REGISTRATION_IN_FLIGHT.format(what=what))
    if open_rows:
        row = open_rows[0]
        return str(row["id"]), str(row["provider_ref"]), _json_detail(row["detail"])
    try:
        purchase_id = await conn.fetchval(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, detail) "
            "VALUES ($1::uuid,$2,$3,$4,$5::jsonb) RETURNING id",
            ctx.tenant_id,
            ctx.agent_id,
            provider,
            kind,
            json.dumps(dict(detail)),
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise RegistrationConflict(_REGISTRATION_IN_FLIGHT.format(what=what)) from exc
    return str(purchase_id), None, dict(detail)


async def _ask_registration(
    ctx: TenantContext, purchase_id: str, call: Any, what: str, *, unconfirmed: Optional[str] = None
) -> tuple[str, Any]:
    """Ask the provider once and record the outcome on the ledger row. A
    definite refusal is re-raised as is (the row is `failed`, so a retry may
    follow); anything ambiguous leaves it `unknown` and raises
    RegistrationUnconfirmed."""
    import provider_purchases
    import recovery_mode
    from command_providers import ProviderConfigurationError, ProviderRejectedError
    from messaging_provider import MessagingProviderError

    try:
        result = await call()
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
        raise RegistrationUnconfirmed(unconfirmed or _REGISTRATION_UNCONFIRMED.format(what=what)) from exc
    await provider_purchases.confirmed_ref(ctx, purchase_id, str(result.reference), {"status": result.status})
    return str(result.reference), result.status


async def register_messaging_brand(
    ctx: TenantContext, fields: Mapping[str, Any], *, provider: str, credentials: Mapping[str, Any]
) -> dict[str, Any]:
    """Register the brokerage's 10DLC brand at most once (see the section note)."""
    import provider_purchases
    from messaging_provider import get_messaging_provider

    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "INSERT INTO tenant_messaging_brands (tenant_id, provider) VALUES ($1::uuid,$2) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            ctx.tenant_id,
            provider,
        )
        brand = await conn.fetchrow(
            "SELECT id, provider_brand_id FROM tenant_messaging_brands WHERE tenant_id=$1::uuid FOR UPDATE",
            ctx.tenant_id,
        )
        if brand["provider_brand_id"]:
            raise RegistrationConflict(BRAND_ALREADY_REGISTERED)
        purchase_id, reuse_ref, detail = await _claim_registration(
            conn, ctx, provider, provider_purchases.BRAND_10DLC, "brand", {}
        )
        if reuse_ref is None:
            # What is about to be sent is recorded before it is sent; a reused
            # registration keeps the fields its own attempt recorded.
            await conn.execute(
                _RECORD_BRAND_INTENT_SQL,
                ctx.tenant_id,
                provider,
                *(fields.get(name) for name in _BRAND_FIELDS),
            )

    if reuse_ref is None:
        adapter = get_messaging_provider(provider)
        try:
            ref, status = await _ask_registration(
                ctx, purchase_id, lambda: adapter.create_brand(dict(fields), credentials=credentials), "brand"
            )
        except RegistrationUnconfirmed:
            raise
        except Exception as exc:
            async with tenant_tx(ctx) as conn:
                await conn.execute(
                    "UPDATE tenant_messaging_brands SET status='failed', failure_reason=$2 "
                    "WHERE tenant_id=$1::uuid AND provider_brand_id IS NULL",
                    ctx.tenant_id,
                    str(exc)[:500],
                )
            raise
    else:
        ref, status = reuse_ref, detail.get("status")

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE tenant_messaging_brands
               SET provider_brand_id=$2, status=$3, failure_reason=NULL, submitted_at=now(),
                   provider_account_id=$4
             WHERE tenant_id=$1::uuid AND provider_brand_id IS NULL
             RETURNING id, status
            """,
            ctx.tenant_id,
            ref,
            _registration_status(status, _BRAND_STATUSES),
            account_fingerprint(credentials),
        )
        if row is None:
            # Left `confirmed` for an operator rather than attached to the wrong brand.
            logger.warning("10DLC brand %s not attached: the brokerage already has a brand", purchase_id)
            raise RegistrationConflict(BRAND_ALREADY_REGISTERED)
        await conn.execute(
            "UPDATE provider_purchases SET state='attached', provider_ref=$2, updated_at=now() WHERE id=$1::uuid",
            purchase_id,
            ref,
        )
    return {"brand_id": str(row["id"]), "provider_brand_id": ref, "status": row["status"]}


async def register_messaging_campaign(
    ctx: TenantContext,
    *,
    description: str,
    usecase: str,
    extra: Mapping[str, Any],
    provider: str,
    credentials: Mapping[str, Any],
) -> dict[str, Any]:
    """Submit the brokerage's 10DLC campaign at most once (see the section note)."""
    import provider_purchases
    from messaging_provider import get_messaging_provider

    brand_sql = (
        "SELECT id, provider_brand_id FROM tenant_messaging_brands WHERE tenant_id=$1::uuid FOR UPDATE"
    )
    existing_sql = (
        "SELECT 1 FROM tenant_messaging_campaigns WHERE tenant_id=$1::uuid "
        "AND provider_campaign_id IS NOT NULL LIMIT 1"
    )
    async with tenant_tx(ctx) as conn:
        brand = await conn.fetchrow(brand_sql, ctx.tenant_id)
        if brand is None or not brand["provider_brand_id"]:
            raise RegistrationConflict("Register a Brand before submitting a Campaign.")
        if await conn.fetchval(existing_sql, ctx.tenant_id):
            raise RegistrationConflict(CAMPAIGN_ALREADY_REGISTERED)
        purchase_id, reuse_ref, detail = await _claim_registration(
            conn, ctx, provider, provider_purchases.CAMPAIGN_10DLC, "campaign",
            {"usecase": usecase, "description": description},
        )

    if reuse_ref is None:
        adapter = get_messaging_provider(provider)
        ref, status = await _ask_registration(
            ctx,
            purchase_id,
            lambda: adapter.submit_campaign(
                brand_id=str(brand["provider_brand_id"]),
                description=description,
                usecase=usecase,
                credentials=credentials,
                **dict(extra),
            ),
            "campaign",
        )
    else:
        ref, status = reuse_ref, detail.get("status")

    async with tenant_tx(ctx) as conn:
        brand = await conn.fetchrow(brand_sql, ctx.tenant_id)
        if await conn.fetchval(existing_sql, ctx.tenant_id):
            logger.warning("10DLC campaign %s not attached: the brokerage already has a campaign", purchase_id)
            raise RegistrationConflict(CAMPAIGN_ALREADY_REGISTERED)
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_messaging_campaigns
                (tenant_id,brand_id,provider,provider_campaign_id,status,usecase,description,submitted_at,
                 provider_account_id)
            VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,now(),$8)
            RETURNING id,status
            """,
            ctx.tenant_id,
            brand["id"],
            provider,
            ref,
            _registration_status(status, _CAMPAIGN_STATUSES),
            detail.get("usecase", usecase),
            detail.get("description", description),
            account_fingerprint(credentials),
        )
        await conn.execute(
            "UPDATE provider_purchases SET state='attached', provider_ref=$2, updated_at=now() WHERE id=$1::uuid",
            purchase_id,
            ref,
        )
    return {"campaign_id": str(row["id"]), "provider_campaign_id": ref, "status": row["status"]}


# ── Send readiness (external audit 25.1) ───────────────────────────────────
# A business text may reach the carrier only when Neoh itself can show the
# sender is registered for it: an active route on the provider that sends it,
# live hosting, the owner's voice-verified number, the brokerage's approved
# 10DLC campaign (same tenant, same provider, brand not failed) on that route,
# and a committed (attached) assignment of THAT exact number to THAT campaign.
# Hosting alone was enough before; the carrier then rejects or filters it.
# Toll-free numbers are a separate registration (toll-free verification) that
# this path does not carry, so they are refused here rather than sent as 10DLC.
TOLL_FREE_PREFIXES = ("+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888")

SEND_BLOCKERS = {
    "not_connected": "connect text messages for your business number",
    "disconnected": "text messages were disconnected for this number",
    "wrong_provider": "this number is set up with a different text-message provider",
    "wrong_account": ("this number's text hosting belongs to a different provider account than the one "
                      "Neoh would send with — refresh the hosting status with your current credential"),
    "hosting_not_active": "finish hosting your business number for text messages",
    "no_verified_number": "verify your business number for calls first",
    "toll_free_not_supported": "toll-free numbers need toll-free verification, which is not available here yet",
    "no_campaign": "register your texting brand and campaign (10DLC)",
    "no_campaign_assigned": "assign your business number to your approved texting campaign (10DLC)",
    "campaign_not_approved": "your texting campaign (10DLC) is not approved yet",
    "registration_unverified": ("your texting campaign's approval has not been confirmed with the provider "
                                "in the last 14 days — check your texting registration"),
    "brand_not_registered": "your texting brand (10DLC) is not registered or was rejected",
    "number_not_assigned": ("your business number is not confirmed on your approved texting campaign "
                            "(the provider has not finished assigning it)"),
}

_SEND_READINESS_SQL = """
    SELECT r.agent_id, r.active, r.provider, r.provider_account_id, r.hosted_order_status, r.campaign_id,
           t.voice_caller_id_e164 AS sender,
           COALESCE(t.voice_caller_id_verified, false) AS sender_verified,
           c.status AS campaign_status, c.provider AS campaign_provider,
           COALESCE(c.status_ok_at > now() - make_interval(days => 14), false) AS campaign_fresh,
           c.provider_campaign_id, b.status AS brand_status, b.provider AS brand_provider,
           EXISTS (
               SELECT 1 FROM provider_purchases p
                WHERE p.tenant_id = r.tenant_id AND p.provider = r.provider
                  AND p.kind = '10dlc_number_assignment' AND p.state = 'attached'
                  -- the provider completed it (assignment is asynchronous)
                  AND p.detail->>'assignment_status' = 'ASSIGNED'
                  AND p.detail->>'campaign_id' = c.provider_campaign_id
                  AND p.detail->>'phone_number' = t.voice_caller_id_e164
           ) AS sender_assigned
      FROM messaging_routes r
      LEFT JOIN telephony_routes t
        ON t.tenant_id = r.tenant_id AND t.agent_id = r.agent_id AND t.active
      LEFT JOIN tenant_messaging_campaigns c
        ON c.tenant_id = r.tenant_id AND c.id = r.campaign_id
      LEFT JOIN tenant_messaging_brands b
        ON b.tenant_id = c.tenant_id AND b.id = c.brand_id
     WHERE r.tenant_id = $1::uuid AND ($2::text IS NULL OR r.agent_id = $2)
"""


def send_blockers(row: Optional[Mapping[str, Any]], provider: str,
                  account: Optional[str] = None) -> list[str]:
    """Why this route may not send business texts yet ([] = it may). Pure.
    `account`: the fingerprint of the credential the send would use; the
    route's hosting must belong to that account. Without one (a caller that
    holds no credential), the route must at least carry a recorded account:
    a route hosted before the account was recorded is not assumed ready."""
    if row is None:
        return ["not_connected"]
    blockers: list[str] = []
    if not row.get("active"):
        blockers.append("disconnected")
    if row.get("provider") != provider:
        blockers.append("wrong_provider")
    if row.get("hosted_order_status") != "active":
        blockers.append("hosting_not_active")
    elif (row.get("provider_account_id") != account if account is not None
          else not str(row.get("provider_account_id") or "").startswith("tk:")):
        blockers.append("wrong_account")
    sender = str(row.get("sender") or "")
    if not sender or not row.get("sender_verified"):
        blockers.append("no_verified_number")
    elif sender.startswith(TOLL_FREE_PREFIXES):
        blockers.append("toll_free_not_supported")
    if not row.get("campaign_id"):
        blockers.append("no_campaign_assigned")
    else:
        if row.get("campaign_status") != "approved":
            blockers.append("campaign_not_approved")
        elif not row.get("campaign_fresh", True):
            blockers.append("registration_unverified")
        if row.get("campaign_provider") != row.get("provider") and "wrong_provider" not in blockers:
            blockers.append("wrong_provider")
        if row.get("brand_status") in (None, "failed", "not_started") or (
                row.get("brand_provider") not in (None, row.get("provider"))):
            blockers.append("brand_not_registered")
        if not row.get("sender_assigned"):
            blockers.append("number_not_assigned")
    return blockers


async def send_readiness_rows(conn: Any, tenant_id: str, agent_id: Optional[str] = None) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(_SEND_READINESS_SQL, tenant_id, agent_id)]


async def messaging_send_readiness(ctx: TenantContext, *, provider: str,
                                   account: Optional[str] = None) -> dict[str, Any]:
    """{"ready", "blockers", "sender"} for this agent's business texts."""
    async with tenant_tx(ctx) as conn:
        rows = await send_readiness_rows(conn, ctx.tenant_id, ctx.agent_id)
        tenant_campaign = await conn.fetchval(
            "SELECT status FROM tenant_messaging_campaigns WHERE tenant_id=$1::uuid "
            "ORDER BY created_at DESC LIMIT 1", ctx.tenant_id)
    ready = [r for r in rows if not send_blockers(r, provider, account)]
    row = ready[0] if ready else (rows[0] if rows else None)
    blockers = send_blockers(row, provider, account)
    if "no_campaign_assigned" in blockers and tenant_campaign != "approved":
        # Nothing to assign yet: say what is actually missing.
        blockers[blockers.index("no_campaign_assigned")] = (
            "campaign_not_approved" if tenant_campaign else "no_campaign")
    return {"ready": not blockers, "blockers": blockers,
            "sender": str(row["sender"]) if row and not blockers else None}


def describe_blockers(blockers: Sequence[str]) -> str:
    return "; ".join(SEND_BLOCKERS.get(b, b) for b in blockers)


# ── Registration status sync (external audit 25.2) ─────────────────────────
# The brand and campaign rows used to keep the status their submission
# answered (pending) forever: nothing asked the provider again. The provider's
# own values are mapped here, value by value; an unrecognised value changes
# nothing (it is recorded as a check error), and nothing is marked approved
# because a submission was accepted — only MNO_PROVISIONED is "approved".
_TELNYX_CAMPAIGN_STATUS = {
    "MNO_PROVISIONED": "approved",
    "TCR_PENDING": "pending", "TCR_ACCEPTED": "pending", "TELNYX_ACCEPTED": "pending",
    "MNO_PENDING": "pending", "MNO_ACCEPTED": "pending",
    "TCR_FAILED": "rejected", "TELNYX_FAILED": "rejected", "MNO_REJECTED": "rejected",
    "MNO_PROVISIONING_FAILED": "failed",
    "TCR_SUSPENDED": "suspended", "TCR_EXPIRED": "expired",
}
_TELNYX_BRAND_REGISTRATION = {"REGISTRATION_PENDING": "pending", "REGISTRATION_FAILED": "failed"}
_TELNYX_BRAND_IDENTITY = {"VERIFIED": "verified", "VETTED_VERIFIED": "verified",
                          "SELF_DECLARED": "verified", "UNVERIFIED": "unverified"}
_PROBLEM_CAMPAIGN_STATUSES = ("rejected", "failed", "suspended", "expired")


def map_campaign_status(provider_status: str) -> Optional[str]:
    return _TELNYX_CAMPAIGN_STATUS.get(str(provider_status or "").strip().upper())


def map_brand_status(registration: str, identity: str) -> Optional[str]:
    reg = str(registration or "").strip().upper()
    if reg in _TELNYX_BRAND_REGISTRATION:
        return _TELNYX_BRAND_REGISTRATION[reg]
    if reg == "OK":
        return _TELNYX_BRAND_IDENTITY.get(str(identity or "").strip().upper())
    return None


def _not_found(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError"


async def _read_status(read: Any) -> tuple[Optional[dict[str, Any]], Optional[str], bool]:
    """(answer, error, gone). Never raises: a failed read changes no status."""
    try:
        return await read(), None, False
    except Exception as exc:  # noqa: BLE001 — recorded on the row
        return None, f"{type(exc).__name__}: {exc}"[:500], _not_found(exc)


_APPLY_BRAND_SQL = """
    UPDATE tenant_messaging_brands
       SET status = COALESCE($3, status),
           provider_status = COALESCE($4, provider_status),
           failure_reason = CASE WHEN $3 IS NULL THEN failure_reason
                                 WHEN $3 = 'failed' THEN $5 ELSE NULL END,
           approved_at = CASE WHEN $3 = 'verified' THEN COALESCE(approved_at, now()) ELSE approved_at END,
           status_check_error = $6,
           status_checked_at = $7,
           status_ok_at = COALESCE($8, status_ok_at),
           provider_account_id = COALESCE(provider_account_id, $9)
     WHERE id = $1::uuid AND tenant_id = $2::uuid
       AND (status_checked_at IS NULL OR status_checked_at <= $7)
    RETURNING status"""

_APPLY_CAMPAIGN_SQL = """
    UPDATE tenant_messaging_campaigns
       SET status = COALESCE($3, status),
           provider_status = COALESCE($4, provider_status),
           failure_reason = CASE WHEN $3 IS NULL THEN failure_reason
                                 WHEN $3 IN ('rejected','failed','suspended','expired') THEN $5
                                 ELSE NULL END,
           approved_at = CASE WHEN $3 = 'approved' THEN COALESCE(approved_at, now()) ELSE approved_at END,
           status_check_error = $6,
           status_checked_at = $7,
           status_ok_at = COALESCE($8, status_ok_at),
           provider_account_id = COALESCE(provider_account_id, $9)
     WHERE id = $1::uuid AND tenant_id = $2::uuid
       AND (status_checked_at IS NULL OR status_checked_at <= $7)
    RETURNING status"""

# Telnyx number-assignment states. Only ASSIGNED carries traffic (the send
# gate requires it). A failed assignment ended; PENDING_UNASSIGNMENT is kept
# attached (recorded, not sendable): if the unassignment then fails the
# number is still on the campaign, and a released row would make the next
# assignment a duplicate the provider refuses.
_ASSIGNMENT_ENDED = frozenset({"FAILED_ASSIGNMENT"})
_ASSIGNMENT_STATES = frozenset({"ASSIGNED", "PENDING_ASSIGNMENT", "FAILED_ASSIGNMENT",
                                "PENDING_UNASSIGNMENT", "FAILED_UNASSIGNMENT"})


async def _db_now(ctx: TenantContext) -> Any:
    # The database's clock, not this process's: web and worker processes
    # racing must order their reads on one clock.
    async with tenant_tx(ctx) as conn:
        return await conn.fetchval("SELECT clock_timestamp()")


async def refresh_registration_status(
    ctx: TenantContext, *, provider: str, credentials: Mapping[str, Any], min_interval_s: float = 0
) -> dict[str, Any]:
    """Read the brokerage's 10DLC brand, campaign and committed number
    assignments from the provider and apply what they say (25.2).

    Safe to race: each write carries the database time its read was sent and
    an older read never overwrites a newer one; no lock is held across a
    provider call. A successful read proves the credential's account holds
    the registration (recorded on first proof). A "not found" is evidence the
    registration is gone only from the account recorded as holding it — from
    any other account it changes nothing (a switched or fallback credential
    must not expire a brokerage's campaign). `min_interval_s`: skip the
    provider entirely when everything was read that recently (throttle).
    """
    from messaging_provider import get_messaging_provider

    account = account_fingerprint(credentials)
    async with tenant_tx(ctx) as conn:
        route_account = await conn.fetchval(
            "SELECT provider_account_id FROM messaging_routes WHERE tenant_id=$1::uuid "
            "AND provider_account_id LIKE 'tk:%' ORDER BY updated_at DESC LIMIT 1", ctx.tenant_id)
        brand = await conn.fetchrow(
            "SELECT id, provider, provider_brand_id, status, provider_account_id, status_checked_at "
            "FROM tenant_messaging_brands WHERE tenant_id=$1::uuid", ctx.tenant_id)
        campaigns = await conn.fetch(
            "SELECT id, provider, provider_campaign_id, status, provider_account_id, status_checked_at "
            "FROM tenant_messaging_campaigns "
            "WHERE tenant_id=$1::uuid AND provider_campaign_id IS NOT NULL ORDER BY created_at", ctx.tenant_id)
        recent = await conn.fetchval(
            "SELECT bool_and(status_checked_at > now() - make_interval(secs => $2)) FROM ("
            " SELECT status_checked_at FROM tenant_messaging_campaigns WHERE tenant_id=$1::uuid"
            "   AND provider_campaign_id IS NOT NULL"
            " UNION ALL SELECT status_checked_at FROM tenant_messaging_brands WHERE tenant_id=$1::uuid"
            "   AND provider_brand_id IS NOT NULL) r", ctx.tenant_id, float(min_interval_s))
    out: dict[str, Any] = {"brand": None, "campaigns": [], "assignments": [], "throttled": False}
    adapter = get_messaging_provider(provider)
    if min_interval_s and recent:
        # Brand and campaign were just read; committed assignments are still
        # re-read (a phone-number event must not wait for the next sweep).
        out["throttled"] = True
        out["assignments"] = await _refresh_assignments(ctx, adapter, provider, credentials,
                                                        account is not None and route_account == account)
        return out

    def holder_is(recorded: Optional[str]) -> bool:
        holder = recorded or route_account
        return account is not None and holder == account

    if brand is not None and brand["provider_brand_id"] and brand["provider"] == provider:
        sent_at = await _db_now(ctx)
        answer, error, gone = await _read_status(
            lambda: adapter.get_brand_status(str(brand["provider_brand_id"]), credentials=credentials))
        new = raw = reason = ok_at = proven = None
        if answer is not None:
            raw = f"{answer.get('status') or ''}/{answer.get('identity_status') or ''}"
            new = map_brand_status(answer.get("status"), answer.get("identity_status"))
            reason = str(answer.get("failure_reason") or "")[:500] or None
            proven = account
            if new is None:
                error = f"unrecognised brand status {raw!r}; status left unchanged"
            else:
                ok_at = sent_at
        elif gone and not holder_is(brand["provider_account_id"]):
            error = "not found with this credential's provider account (not the one holding it); left unchanged"
        async with tenant_tx(ctx) as conn:
            applied = await conn.fetchval(_APPLY_BRAND_SQL, brand["id"], ctx.tenant_id, new, raw,
                                          reason, error, sent_at, ok_at, proven)
        out["brand"] = {"brand_id": str(brand["id"]), "status": applied or brand["status"],
                        "previous": brand["status"], "error": error}

    for campaign in campaigns:
        if campaign["provider"] != provider:
            continue
        sent_at = await _db_now(ctx)
        answer, error, gone = await _read_status(
            lambda c=campaign: adapter.get_campaign_status(str(c["provider_campaign_id"]), credentials=credentials))
        new = raw = reason = ok_at = proven = None
        if answer is not None:
            raw = str(answer.get("status") or "")
            new = map_campaign_status(raw)
            reason = str(answer.get("failure_reason") or "")[:500] or None
            proven = account
            if new == "approved" and answer.get("t_mobile_suspended"):
                new, raw, reason = "suspended", f"{raw}/T_MOBILE_SUSPENDED", reason or "suspended by T-Mobile"
            if new is None:
                error = f"unrecognised campaign status {raw!r}; status left unchanged"
            else:
                ok_at = sent_at
        elif gone and holder_is(campaign["provider_account_id"]):
            # The account that holds it no longer knows it: it cannot carry
            # traffic. Recorded as expired, never left 'approved'.
            new, raw, reason = "expired", "NOT_FOUND", "the campaign is no longer found at the provider"
            ok_at, error = sent_at, None
        elif gone:
            error = "not found with this credential's provider account (not the one holding it); left unchanged"
        async with tenant_tx(ctx) as conn:
            applied = await conn.fetchval(_APPLY_CAMPAIGN_SQL, campaign["id"], ctx.tenant_id, new, raw,
                                          reason, error, sent_at, ok_at, proven)
        if new and applied and applied != campaign["status"]:
            logger.info("10DLC campaign %s: %s -> %s (%s)", campaign["id"], campaign["status"], applied, raw)
        out["campaigns"].append({"campaign_id": str(campaign["id"]), "status": applied or campaign["status"],
                                 "previous": campaign["status"], "error": error})

    out["assignments"] = await _refresh_assignments(ctx, adapter, provider, credentials, holder_is(None))
    # A registration the provider confirmed but that never reached its row is
    # for an operator (L10-09), never attached by a refresh; the health
    # component and operator diagnostics show it.
    out["unattached_submissions"] = len(await open_registration_submissions(ctx))
    return out


async def _refresh_assignments(ctx: TenantContext, adapter: Any, provider: str,
                               credentials: Mapping[str, Any], account_verified: bool) -> list[dict[str, Any]]:
    """Re-read committed (attached) number assignments. ASSIGNED is recorded
    (the send gate requires it); a number on another campaign, without one,
    or whose assignment failed or is being removed is released. A "not
    found" releases only when read through the account holding the hosting."""
    import json

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT id, detail->>'campaign_id' AS campaign_ref, detail->>'phone_number' AS phone "
            "FROM provider_purchases WHERE tenant_id=$1::uuid AND provider=$2 "
            "AND kind='10dlc_number_assignment' AND state='attached'", ctx.tenant_id, provider)
    results = []
    for row in rows:
        answer, error, gone = await _read_status(
            lambda r=row: adapter.get_number_campaign(str(r["phone"]), credentials=credentials))
        status = cid = None
        release = None
        if answer is not None:
            cid = (answer or {}).get("campaign_id")
            status = str((answer or {}).get("assignment_status") or "").strip().upper()
            if not cid or cid != row["campaign_ref"]:
                release = f"now assigned to {cid}" if cid else "no longer assigned at the provider"
            elif status in _ASSIGNMENT_ENDED:
                release = f"assignment {status}"
            elif status not in _ASSIGNMENT_STATES:
                error = f"unrecognised assignment status {status!r}; left unchanged"
        elif gone and account_verified:
            release = "no longer assigned at the provider"
        elif gone:
            error = "not found with this credential's provider account (not the one holding it); left unchanged"
        note = {"assignment_checked_at": datetime.now(timezone.utc).isoformat(),
                "assignment_check_error": error}
        if status in _ASSIGNMENT_STATES:
            note["assignment_status"] = status
        async with tenant_tx(ctx) as conn:
            if release:
                await conn.execute(
                    "UPDATE provider_purchases SET state='released', updated_at=now(), "
                    "detail = detail || $2::jsonb WHERE id=$1::uuid AND state='attached'",
                    row["id"], json.dumps({**note, "released_reason": release}))
                logger.warning("10DLC assignment %s released: %s", row["id"], release)
            else:
                await conn.execute(
                    "UPDATE provider_purchases SET updated_at=now(), detail = detail || $2::jsonb "
                    "WHERE id=$1::uuid AND state='attached'", row["id"], json.dumps(note))
        results.append({"assignment_id": str(row["id"]), "released": bool(release),
                        "assignment_status": status, "error": error})
    return results


async def registrations_to_sync(*, limit: int = 200) -> list[str]:
    """Brokerages with a 10DLC registration whose status can still change,
    least recently read first: pending; approved or suspended (may be
    revoked or restored); expired as not-found (may have been a transient
    account problem); rejected/failed at most daily (appeals); a brand still
    pending/unverified; a committed assignment not yet ASSIGNED."""
    async with tenant_tx(_platform_context()) as conn:
        rows = await conn.fetch(
            """
            SELECT tenant_id::text AS tenant_id, min(COALESCE(checked, 'epoch')) AS oldest
              FROM (SELECT tenant_id, status_checked_at AS checked FROM tenant_messaging_campaigns
                     WHERE provider_campaign_id IS NOT NULL
                       AND (status IN ('pending','approved','suspended')
                            OR (status = 'expired' AND provider_status = 'NOT_FOUND')
                            OR (status IN ('rejected','failed')
                                AND COALESCE(status_checked_at, 'epoch') < now() - interval '1 day'))
                    UNION ALL
                    SELECT tenant_id, status_checked_at FROM tenant_messaging_brands
                     WHERE provider_brand_id IS NOT NULL AND status IN ('pending','unverified')
                    UNION ALL
                    SELECT tenant_id, NULL FROM provider_purchases
                     WHERE kind = '10dlc_number_assignment' AND state = 'attached'
                       AND detail->>'assignment_status' IS DISTINCT FROM 'ASSIGNED') r
             GROUP BY tenant_id ORDER BY 2 LIMIT $1
            """, max(1, min(1000, int(limit))))
    return [r["tenant_id"] for r in rows]


async def tenant_for_registration(provider_id: str) -> Optional[str]:
    """The brokerage a provider brand/campaign id, or an assigned number,
    belongs to (webhook routing)."""
    async with tenant_tx(_platform_context()) as conn:
        return await conn.fetchval(
            "SELECT tenant_id::text FROM tenant_messaging_campaigns WHERE provider_campaign_id=$1 "
            "UNION ALL SELECT tenant_id::text FROM tenant_messaging_brands WHERE provider_brand_id=$1 "
            "UNION ALL SELECT tenant_id::text FROM provider_purchases WHERE kind='10dlc_number_assignment' "
            "AND state='attached' AND detail->>'phone_number'=$1 LIMIT 1",
            provider_id)


NO_APPROVED_CAMPAIGN = "No approved Campaign to assign this number to."
_ASSIGNMENT_IN_FLIGHT = (
    "This number is already being assigned to your campaign. Nothing new was submitted — "
    "try again in a few minutes."
)
_ASSIGNMENT_UNCONFIRMED = (
    "We could not confirm that this number was assigned to your campaign. Nothing more will be "
    "submitted for it until an operator has checked it with the provider."
)
_CAMPAIGN_FOR_ASSIGNMENT_SQL = """
    SELECT c.id, c.provider_campaign_id
      FROM tenant_messaging_campaigns c
      JOIN tenant_messaging_brands b ON b.id = c.brand_id
     WHERE c.tenant_id = $1::uuid AND c.status = 'approved'
     ORDER BY c.created_at DESC
     LIMIT 1
       FOR UPDATE OF c"""
_ASSIGNMENT_ROWS_SQL = """
    SELECT id, state, provider_ref
      FROM provider_purchases
     WHERE tenant_id = $1::uuid AND provider = $2 AND kind = $3
       AND detail->>'campaign_id' = $4 AND detail->>'phone_number' = $5
       AND state IN ('intended','unknown','confirmed','attached')
     ORDER BY created_at DESC"""


async def assign_campaign_number(
    ctx: TenantContext, *, phone_number: str, provider: str, credentials: Mapping[str, Any]
) -> dict[str, Any]:
    """Assign the business number to the brokerage's approved 10DLC campaign
    at most once at the provider (L10-10; same ledger as the brand and
    campaign, L10-09).

    Under the campaign row's lock (which serialises every broker owner of the
    brokerage), for this number + campaign: an in-flight or `unknown`
    assignment is a 409 and reaches no provider — `unknown` (timeout, lost
    answer) waits for an operator, never a retry; an `attached` one is
    already done (no provider call); a `confirmed` one that never reached the
    route is reused. Otherwise the intent is recorded first, the provider is
    asked once outside the transaction, and the route and the ledger row are
    updated together. A definite refusal is `failed` and may be retried."""
    import json

    import asyncpg

    import provider_purchases
    from messaging_provider import get_messaging_provider

    async with tenant_tx(ctx) as conn:
        # The brokerage's own campaign, also for a platform admin acting for it
        # (the same tenant filter the neighbouring 10DLC queries use).
        campaign = await conn.fetchrow(_CAMPAIGN_FOR_ASSIGNMENT_SQL, ctx.tenant_id)
        if campaign is None or not campaign["provider_campaign_id"]:
            raise RegistrationConflict(NO_APPROVED_CAMPAIGN)
        provider_campaign = str(campaign["provider_campaign_id"])
        rows = await conn.fetch(
            _ASSIGNMENT_ROWS_SQL, ctx.tenant_id, provider, provider_purchases.NUMBER_ASSIGNMENT_10DLC,
            provider_campaign, phone_number,
        )
        if any(r["state"] == "unknown" for r in rows):
            raise RegistrationConflict(_ASSIGNMENT_UNCONFIRMED)
        if any(r["state"] == "intended" for r in rows):
            raise RegistrationConflict(_ASSIGNMENT_IN_FLIGHT)
        done = next((r for r in rows if r["state"] == "attached"), None)
        reuse = next((r for r in rows if r["state"] == "confirmed"), None)
        purchase_id: Optional[str] = str(reuse["id"]) if reuse else None
        if done is None and reuse is None:
            try:
                purchase_id = str(await conn.fetchval(
                    "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, detail) "
                    "VALUES ($1::uuid,$2,$3,$4,$5::jsonb) RETURNING id",
                    ctx.tenant_id,
                    ctx.agent_id,
                    provider,
                    provider_purchases.NUMBER_ASSIGNMENT_10DLC,
                    json.dumps({"campaign_id": provider_campaign, "phone_number": phone_number}),
                ))
            except asyncpg.exceptions.UniqueViolationError as exc:
                # This owner has another 10DLC assignment unresolved.
                raise RegistrationConflict(_ASSIGNMENT_IN_FLIGHT) from exc

    if done is None and reuse is None:
        adapter = get_messaging_provider(provider)
        await _ask_registration(
            ctx,
            purchase_id,
            lambda: adapter.assign_number_to_campaign(provider_campaign, phone_number, credentials=credentials),
            "number assignment",
            unconfirmed=_ASSIGNMENT_UNCONFIRMED,
        )

    route = await get_messaging_route(ctx)
    async with tenant_tx(ctx) as conn:
        if route is not None:
            await conn.execute(
                "UPDATE messaging_routes SET campaign_id=$2::uuid, updated_at=now() WHERE id=$1::uuid",
                route["id"],
                campaign["id"],
            )
        if done is None:
            await conn.execute(
                "UPDATE provider_purchases SET state='attached', updated_at=now() WHERE id=$1::uuid",
                purchase_id,
            )
    # The provider completes an assignment asynchronously; texts wait for
    # ASSIGNED (25.2). Read it once now, best effort; the sync reads it after.
    try:
        adapter = get_messaging_provider(provider)
        assignments = await _refresh_assignments(ctx, adapter, provider, credentials, False)
    except Exception:  # noqa: BLE001 — the assignment itself succeeded; the sync reads its status
        logger.warning("assignment status not read right after assigning", exc_info=True)
        assignments = []
    mine = purchase_id or (str(done["id"]) if done is not None else None)
    status = next((a["assignment_status"] for a in assignments if a["assignment_id"] == mine), None)
    return {"campaign_id": str(campaign["id"]), "assigned": True, "already_assigned": done is not None,
            "assignment_status": status}


async def open_registration_submissions(ctx: TenantContext) -> list[dict[str, Any]]:
    """10DLC submissions whose outcome is not on the brand/campaign yet."""
    import provider_purchases

    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT kind, state, updated_at FROM provider_purchases WHERE tenant_id=$1::uuid "
            "AND kind = ANY($2::text[]) AND state IN ('intended','unknown','confirmed') ORDER BY created_at",
            ctx.tenant_id,
            [provider_purchases.BRAND_10DLC, provider_purchases.CAMPAIGN_10DLC,
             provider_purchases.NUMBER_ASSIGNMENT_10DLC],
        )
    kinds = {provider_purchases.BRAND_10DLC: "brand", provider_purchases.CAMPAIGN_10DLC: "campaign",
             provider_purchases.NUMBER_ASSIGNMENT_10DLC: "number_assignment"}
    return [
        {"kind": kinds[r["kind"]],
         "state": "unconfirmed" if r["state"] == "unknown" else "in_progress",
         "since": r["updated_at"].isoformat()}
        for r in rows
    ]


# ── Inbound routing: never trust a caller-supplied tenant/agent ────────────


async def resolve_messaging_route_by_number(to_e164: str) -> Optional[dict[str, Any]]:
    """The only trusted way to route an inbound SMS/MMS: match the dialed
    public business number against telephony_routes, then require an active
    messaging_routes row for that same tenant+agent. A number with a voice
    route but no completed messaging connection resolves to None — the
    webhook fails closed rather than guessing. So does a number whose agent
    is suspended or gone (D-2026-11-16-01, audit 22.5); a STOP to it is still
    recorded through resolve_opt_out_owners_by_number."""
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
               AND EXISTS (SELECT 1 FROM users u
                            WHERE u.tenant_id = t.tenant_id
                              AND lower(u.agent_id) = lower(t.agent_id)
                              AND u.is_active)
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
    # A client who texts a card number does not put it in our database (PCI
    # DSS SAQ A); the body keeps "[card number removed]" in its place.
    text = pan_redaction.scrub(text, "sms_inbound", tenant_id=tenant_id)
    async with tenant_tx(ctx) as conn:
        contact_id, client_id = await _match_contact(conn, tenant_id=tenant_id, lookup_digest=lookup_digest)
        row = await conn.fetchrow(
            """
            INSERT INTO sms_messages (
                tenant_id,agent_id,contact_id,client_id,direction,provider,
                provider_message_id,provider_status,status,from_e164,to_e164,
                body,body_ciphertext,media,opt_out_event
            ) VALUES (
                $1::uuid,$2,$3::uuid,$4::uuid,'inbound',$5,$6,'received','delivered',
                $7,$8,NULL,pgp_sym_encrypt(NULLIF($9::text,''),$12::text,'s2k-mode=1'),$10::jsonb,$11
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
            sealed_text.key_for(tenant_id),
        )
        if row is None:
            existing = await conn.fetchrow(
                "SELECT id,activity_id FROM sms_messages WHERE provider=$1 AND provider_message_id=$2",
                provider,
                provider_message_id,
            )
            return str(existing["id"]) if existing else ""

        message_id = str(row["id"])
        replied = not opted_out and bool(client_id or contact_id)
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
    if replied:
        # The lead answered (0158, L04-03): stamp their newest first response.
        # After the commit, because the reply's evidence references this
        # message; in the platform context, because the number's agent need
        # not be the one who may edit the lead's record. An opt-out is not a
        # reply. Never fails the webhook.
        import outreach_lifecycle

        try:
            async with tenant_tx(_platform_context()) as conn:
                await outreach_lifecycle.record_reply(
                    conn, tenant_id=tenant_id, sms_message_id=message_id,
                    client_id=client_id, contact_id=contact_id,
                    received_at=datetime.now(timezone.utc),
                )
        except Exception:  # noqa: BLE001
            logger.warning("reply bookkeeping failed for message %s", message_id, exc_info=True)
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
) -> bool:
    """Log an outbound text; True if this call inserted the row (False: it
    already existed — the insert is idempotent on the provider message id)."""
    ctx = _tenant_context(tenant_id, agent_id)
    # The approved draft was already cleaned (stage_command); this guards the
    # log row against any other caller.
    body = pan_redaction.scrub(body, "sms_outbound", tenant_id=tenant_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO sms_messages (
                tenant_id,agent_id,contact_id,client_id,direction,provider,
                provider_message_id,status,from_e164,to_e164,body,body_ciphertext
            ) VALUES ($1::uuid,$2,$3::uuid,$4::uuid,'outbound',$5,$6,'queued',$7,$8,
                      NULL,pgp_sym_encrypt(NULLIF($9::text,''),$10::text,'s2k-mode=1'))
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
            sealed_text.key_for(tenant_id),
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
    return row is not None




REPAIR_RECHECK_HOURS = 24


async def repair_missing_outbound_rows(only_tenant: Optional[str] = None, *, limit: int = 100) -> dict[str, int]:
    """Rebuild the log row of a text Telnyx accepted but we never recorded
    (external audit 25.5). Never re-sends anything.

    The worker sends first and then writes the sms_messages row; a failure of
    that write was only logged, and the command still succeeded with the
    provider's message id. Without the row the text has no delivery status, no
    timeline entry and no first-response delivery, and the receipt webhook
    answers 503 for a row that would never appear.

    Rebuilt from the command itself (approved draft, target, the staging
    agent). The sender is the number the send actually used, from the job's
    provider result; failing that, the agent's line. record_outbound_message
    is idempotent on (provider, provider_message_id), so a concurrent sweep or
    the worker's late write cannot double it.

    Never rebuilt: a text to someone erased by a deletion request (their
    tombstone matches the number, or the client/contact it named is gone) —
    the erasure deleted that row on purpose. Rows that cannot be rebuilt are
    marked (reconcile_checked_at) and looked at again only after
    REPAIR_RECHECK_HOURS, so they cannot crowd out newer ones.
    """
    from privacy_lifecycle import contact_hmac

    async with tenant_tx(_platform_context()) as conn:
        rows = await conn.fetch(
            """
            SELECT ce.id, ce.tenant_id, ce.created_by, ce.target, ce.draft, ce.provider_reference,
                   COALESCE(
                     NULLIF(j.result->'detail'->>'from', ''),
                     (SELECT r.voice_caller_id_e164 FROM telephony_routes r
                       WHERE r.tenant_id = ce.tenant_id AND r.agent_id = ce.created_by
                         AND r.voice_caller_id_e164 IS NOT NULL
                       ORDER BY r.active DESC, r.updated_at DESC NULLS LAST LIMIT 1)) AS sender,
                   (NULLIF(ce.target->>'client_id', '') IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM clients c WHERE c.tenant_id = ce.tenant_id
                          AND c.id::text = lower(ce.target->>'client_id')))
                   OR (NULLIF(ce.target->>'contact_id', '') IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM agent_contacts a WHERE a.tenant_id = ce.tenant_id
                          AND a.id::text = lower(ce.target->>'contact_id'))) AS record_gone
              FROM command_executions ce
              LEFT JOIN automation_jobs j ON j.id = ce.job_id AND j.tenant_id = ce.tenant_id
             WHERE ce.command_type = 'SMS' AND ce.state = 'succeeded' AND ce.provider = 'telnyx'
               AND ce.provider_reference IS NOT NULL
               -- the SEND time: updated_at moves with every write (set_updated_at
               -- trigger), including this repair's own set-aside mark
               AND COALESCE(ce.provider_submitted_at, ce.created_at) < now() - interval '2 minutes'
               AND COALESCE(ce.provider_submitted_at, ce.created_at) > now() - interval '30 days'
               AND (ce.reconcile_checked_at IS NULL
                    OR ce.reconcile_checked_at < now() - make_interval(hours => $3))
               AND ($1::uuid IS NULL OR ce.tenant_id = $1)
               AND NOT EXISTS (SELECT 1 FROM sms_messages m
                                WHERE m.provider = 'telnyx' AND m.provider_message_id = ce.provider_reference)
             ORDER BY ce.reconcile_checked_at NULLS FIRST, COALESCE(ce.provider_submitted_at, ce.created_at)
             LIMIT $2
            """, only_tenant, limit, REPAIR_RECHECK_HOURS)
    counts = {"repaired": 0, "erased": 0, "no_sender": 0, "failed": 0}

    async def set_aside(command_id) -> None:
        async with tenant_tx(_platform_context()) as conn:
            await conn.execute("UPDATE command_executions SET reconcile_checked_at=now() WHERE id=$1",
                               command_id)

    for row in rows:
        try:
            target = _json_detail(row["target"])
            draft = _json_detail(row["draft"])
            content = draft.get("content") if isinstance(draft.get("content"), dict) else draft
            phone = str(target.get("phone") or "")
            tenant_id = str(row["tenant_id"])
            erased = bool(row["record_gone"])
            if phone and not erased:
                async with tenant_tx(_platform_context()) as conn:
                    erased = bool(await conn.fetchval(
                        "SELECT 1 FROM suppression_tombstones WHERE tenant_id=$1::uuid AND contact_hmac=$2 "
                        "AND reason='erased_by_request'", tenant_id, contact_hmac(tenant_id, phone)))
            if erased:
                counts["erased"] += 1
                await set_aside(row["id"])
                continue
            if not row["sender"] or not phone:
                counts["no_sender"] += 1
                logger.warning("sent text %s has no log row and no known sender; needs a person",
                               row["provider_reference"])
                await set_aside(row["id"])
                continue
            inserted = await record_outbound_message(
                tenant_id=tenant_id,
                agent_id=str(row["created_by"]),
                contact_id=str(target["contact_id"]) if target.get("contact_id") else None,
                client_id=str(target["client_id"]) if target.get("client_id") else None,
                provider="telnyx",
                provider_message_id=str(row["provider_reference"]),
                from_e164=str(row["sender"]),
                to_e164=phone,
                body=str(content.get("body") or ""),
            )
            counts["repaired"] += int(inserted)
        except Exception:  # noqa: BLE001 - one row must not stop the sweep
            counts["failed"] += 1
            logger.exception("log row for sent text %s not repaired", row["provider_reference"])
            try:
                await set_aside(row["id"])
            except Exception:  # noqa: BLE001
                pass
    return counts


async def tenant_for_provider_message(provider: str, provider_message_id: str) -> Optional[str]:
    """The brokerage an outbound message belongs to (webhook authorization, 25.4)."""
    async with tenant_tx(_platform_context()) as conn:
        return await conn.fetchval(
            "SELECT tenant_id::text FROM sms_messages WHERE provider=$1 AND provider_message_id=$2 LIMIT 1",
            provider, provider_message_id)


_FINAL_OUTCOME = {"delivered": "delivered", "failed": "failed", "undelivered": "failed"}


def _contradiction(stored: Optional[str], received: str) -> Optional[str]:
    """The stored final status when `received` is a final status of the other
    outcome (delivered vs failed/undelivered); otherwise None."""
    before, after = _FINAL_OUTCOME.get(str(stored or "")), _FINAL_OUTCOME.get(str(received or ""))
    return str(stored) if before and after and before != after else None


async def mark_hosted_order_checked(ctx: TenantContext) -> None:
    try:
        async with tenant_tx(ctx) as conn:
            await conn.execute("UPDATE messaging_routes SET hosted_order_checked_at=now() "
                               "WHERE tenant_id=$1::uuid AND agent_id=$2", ctx.tenant_id, ctx.agent_id)
    except Exception:  # noqa: BLE001
        logger.warning("hosted-order check time not recorded", exc_info=True)


async def hosted_orders_to_sync(*, limit: int = 200) -> list[tuple[str, str]]:
    """(tenant_id, agent_id) of active routes whose hosted order is with the
    provider and not final, least recently read first (25.6). An order still
    waiting for the owner's ownership code is not swept: nothing advances it
    but the owner."""
    async with tenant_tx(_platform_context()) as conn:
        rows = await conn.fetch(
            "SELECT tenant_id::text AS tenant_id, agent_id FROM messaging_routes "
            "WHERE active AND hosted_order_id IS NOT NULL AND hosted_order_status IN "
            "('processing','pending_documents','manual_action_required') "
            "ORDER BY hosted_order_checked_at NULLS FIRST LIMIT $1", max(1, min(1000, int(limit))))
    return [(r["tenant_id"], r["agent_id"]) for r in rows]


class ReceiptNotFound(MessagingDataError):
    pass


async def reconcile_delivery_receipt(ctx: TenantContext, *, receipt_id: int, final_status: str) -> dict[str, Any]:
    """A person settles contradicting delivery receipts (25.8) after checking
    the provider's record: the open contradictions of that message are marked
    reconciled and the message takes `final_status` ('delivered' or 'failed').

    What it cannot do, it reports instead of claiming: a first-response row
    already 'delivered' (or 'failed') does not move backwards, and a
    lead_engaged charge metered on the first delivered receipt stays — the
    result says so (billing_followup) for the usage-resolution flow."""
    import outreach_lifecycle

    if final_status not in ("delivered", "failed"):
        raise MessagingDataError("final_status must be 'delivered' or 'failed'")
    async with tenant_tx(ctx) as conn:
        receipt = await conn.fetchrow(
            "SELECT tenant_id, provider, provider_message_id FROM sms_delivery_receipts "
            "WHERE id=$1 AND contradicts IS NOT NULL AND reconciled_at IS NULL FOR UPDATE", int(receipt_id))
        if receipt is None:
            raise ReceiptNotFound("No open contradiction with that id")
        tenant, provider, message = receipt["tenant_id"], receipt["provider"], receipt["provider_message_id"]
        stored = await conn.fetchval(
            "SELECT status FROM sms_messages WHERE tenant_id=$1 AND provider=$2 AND provider_message_id=$3 "
            "FOR UPDATE", tenant, provider, message)
        closed = await conn.execute(
            "UPDATE sms_delivery_receipts SET reconciled_at=now() WHERE tenant_id=$1 AND provider=$2 "
            "AND provider_message_id=$3 AND contradicts IS NOT NULL AND reconciled_at IS NULL AND id <= $4",
            tenant, provider, message, int(receipt_id))
        changed = _FINAL_OUTCOME.get(str(stored or "")) != final_status
        if changed:
            await conn.execute(
                "UPDATE sms_messages SET status=$4, provider_status=$4, "
                "delivered_at=CASE WHEN $4='delivered' THEN COALESCE(delivered_at, now()) ELSE NULL END, "
                "failed_at=CASE WHEN $4='failed' THEN COALESCE(failed_at, now()) ELSE NULL END "
                "WHERE tenant_id=$1 AND provider=$2 AND provider_message_id=$3", tenant, provider, message, final_status)
            advance = (outreach_lifecycle.record_sms_delivered if final_status == "delivered"
                       else outreach_lifecycle.record_sms_failed)
            advanced = await advance(conn, provider=provider, provider_message_id=message)
        else:
            advanced = 0
        facts = await conn.fetchrow(
            """
            SELECT (SELECT r.disposition FROM command_executions c
                      JOIN lead_response_events r ON r.command_id = c.id AND r.tenant_id = c.tenant_id
                     WHERE c.tenant_id=$1 AND c.provider=$2 AND c.provider_reference=$3 LIMIT 1) AS first_response,
                   (SELECT count(*) FROM command_executions c
                      JOIN billing_usage_events u ON u.source_id = c.id AND u.tenant_id = c.tenant_id
                     WHERE c.tenant_id=$1 AND c.provider=$2 AND c.provider_reference=$3
                       AND u.metric='lead_engaged') AS metered
            """, tenant, provider, message)
    first_response = facts["first_response"] if facts else None
    metered = int(facts["metered"] or 0) if facts else 0
    followup = None
    if final_status == "failed" and metered:
        followup = "a lead_engaged charge was metered on the delivered receipt; resolve it in usage billing"
        logger.warning("ORACLE_ALERT sms_reconciled_failed_but_metered %s message %s", provider, message)
    return {"tenant_id": str(tenant), "provider_message_id": message, "final_status": final_status,
            "status_changed": changed, "first_response_advanced": int(advanced or 0),
            "first_response_disposition": first_response, "metered": metered, "billing_followup": followup,
            "receipts_reconciled": int(str(closed).split()[-1] or 0)}


async def open_delivery_contradictions(ctx: TenantContext, *, limit: int = 100) -> list[dict[str, Any]]:
    """Open contradicting receipts for the operator (25.8): which to reconcile."""
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, provider, provider_message_id, status, contradicts, occurred_at, received_at "
            "FROM sms_delivery_receipts WHERE contradicts IS NOT NULL AND reconciled_at IS NULL "
            "ORDER BY received_at LIMIT $1", max(1, min(500, int(limit))))
    return [{**dict(r), "tenant_id": str(r["tenant_id"])} for r in rows]


DELIVERY_RECEIPT_KEEP_DAYS = 90


async def prune_delivery_receipts() -> int:
    """Delete journaled receipts after DELIVERY_RECEIPT_KEEP_DAYS; a
    contradiction is kept until it is reconciled (25.8)."""
    async with tenant_tx(_platform_context()) as conn:
        result = await conn.execute(
            "DELETE FROM sms_delivery_receipts WHERE received_at < now() - make_interval(days => $1) "
            "AND (contradicts IS NULL OR reconciled_at IS NOT NULL)", DELIVERY_RECEIPT_KEEP_DAYS)
    return int(str(result).split()[-1] or 0)


async def record_delivery_update(*, provider: str, provider_message_id: str, status: str,
                                 error_reason: Optional[str], event_id: Optional[str] = None,
                                 occurred_at: Optional[str] = None) -> bool:
    """Monotonic status update. Returns False only when no such message row
    exists yet (the receipt beat our own insert) so the webhook can ask the
    provider to redeliver.

    Delivery receipts arrive late, twice and out of order. A late
    `message.sent` used to downgrade `delivered`; now status only moves
    forward (queued → sent → delivered/failed) and a terminal state is final.
    """
    async with tenant_tx(_platform_context()) as conn:
        # 25.8: every receipt is journaled. A final status contradicting the
        # final status already stored is marked and alerted, and changes
        # nothing (no status, no delivery/failure side effects): a person
        # reconciles it against the provider's record. Locked per message so
        # two receipts racing are judged one after the other.
        current = await conn.fetchrow(
            "SELECT tenant_id, status FROM sms_messages WHERE provider=$1 AND provider_message_id=$2 "
            "FOR UPDATE", provider, provider_message_id)
        if current is None:
            return False
        contradicts = _contradiction(current["status"], status)
        try:
            occurred = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00")) if occurred_at else None
        except ValueError:
            occurred = None
        receipt_id = await conn.fetchval(
            "INSERT INTO sms_delivery_receipts (tenant_id, provider, provider_message_id, status, error_reason, "
            "contradicts, provider_event_id, occurred_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT (provider, provider_event_id) WHERE provider_event_id IS NOT NULL DO NOTHING "
            "RETURNING id",
            current["tenant_id"], provider, provider_message_id[:128], str(status)[:40],
            (str(error_reason)[:500] if error_reason else None), contradicts,
            (str(event_id)[:128] if event_id else None), occurred)
        if contradicts:
            if receipt_id is not None:
                logger.warning("ORACLE_ALERT sms_delivery_contradiction receipt %s (%s message %s): stored %s, "
                               "receipt says %s; left for reconciliation",
                               receipt_id, provider, provider_message_id, contradicts, status)
            return True
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
        import outreach_lifecycle

        if outreach_lifecycle.SMS_STATUS_STAGE.get(status) is outreach_lifecycle.Stage.DELIVERED:
            # The carrier's receipt is a text's delivery (outreach_lifecycle):
            # the only point a speed-to-lead text meters lead_engaged. Run on
            # every 'delivered' receipt, including a duplicate, because it is
            # idempotent and a redelivery heals a first attempt that failed.
            # SAVEPOINTed inside; it cannot fail this status update.
            await outreach_lifecycle.record_sms_delivered(
                conn, provider=provider, provider_message_id=provider_message_id,
            )
        elif outreach_lifecycle.SMS_STATUS_STAGE.get(status) is outreach_lifecycle.Stage.FAILED:
            # A carrier failure: the first response reached the provider, not
            # the lead. Idempotent and SAVEPOINTed, like the delivered path.
            await outreach_lifecycle.record_sms_failed(
                conn, provider=provider, provider_message_id=provider_message_id,
            )
        if str(result).endswith(" 0"):
            exists = await conn.fetchval(
                "SELECT 1 FROM sms_messages WHERE provider=$1 AND provider_message_id=$2",
                provider, provider_message_id)
            return bool(exists)
        return True
