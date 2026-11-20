"""Authenticated messaging (SMS/MMS) configuration and signed Telnyx webhooks.

Mirrors telephony_api.py's shape for the messaging rail. Endpoints live under
/api/messaging so provider terminology never has to leak into product-facing
paths (matches /api/telephony's pattern for voice).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from commands_api import _load_provider_credential
from messaging_data import (
    MessagingDataError,
    RegistrationConflict,
    RegistrationUnconfirmed,
    assign_campaign_number as assign_messaging_campaign_number,
    check_eligibility,
    complete_messaging_verification,
    connect_messaging,
    disconnect_messaging,
    get_messaging_route,
    get_public_business_number,
    open_registration_submissions,
    record_hosted_document,
    refresh_registration_status,
    refresh_hosted_order_status,
    register_messaging_brand,
    register_messaging_campaign,
)
from memory_core_errors import memory_core_offline
from tenancy import Role, TenantContext, require_context, require_role
from billing import require_active_subscription

logger = logging.getLogger("oracle.messaging")


def _texting_provider() -> str:
    """The configured provider, or a fixed 503. MessagingProviderError's text
    names the server's environment variable and its value; the broker owner
    reading the answer can set neither, so it goes to the log only."""
    from messaging_provider import MessagingProviderError, messaging_provider_name

    try:
        return messaging_provider_name()
    except MessagingProviderError as exc:
        logger.error("Messaging provider misconfigured: %s", exc)
        raise HTTPException(status_code=503, detail="Texting is not configured on this server.") from exc


def _provider_outcomes() -> tuple[type[BaseException], ...]:
    """What _ask_registration re-raises as a definite outcome. Anything else it
    turns into RegistrationUnconfirmed, so what is left under a registration
    route after these is the database or sealing layer."""
    import recovery_mode
    from command_providers import ProviderConfigurationError, ProviderRejectedError
    from messaging_provider import MessagingProviderError

    return (ProviderRejectedError, ProviderConfigurationError, MessagingProviderError,
            NotImplementedError, recovery_mode.RecoveryModeBlocked)


def _provider_outcome(exc: BaseException) -> HTTPException:
    """Only a real rejection reaches the owner in the provider's words (the
    adapter writes "Telnyx rejected the brand registration: ..."), for the
    owner fixing the registration. The other outcomes never reached the
    provider: a missing server key (TELNYX_API_KEY), recovery mode, a provider
    with no 10DLC support. They answer a fixed sentence; the text is logged."""
    import recovery_mode
    from command_providers import ProviderRejectedError

    if isinstance(exc, ProviderRejectedError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.error("Texting registration not sent to the provider: %s: %s", type(exc).__name__, exc)
    if isinstance(exc, recovery_mode.RecoveryModeBlocked):
        return HTTPException(status_code=503, detail="Texting is paused while Neoh is in recovery mode.")
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=501,
                             detail="This server's texting provider does not support 10DLC registration.")
    return HTTPException(status_code=503, detail="Texting is not configured on this server.")

router = APIRouter(prefix="/api/messaging", tags=["Messaging"])

_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024  # Telnyx's own LOA/invoice upload cap


class OtpComplete(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1, max_length=12)


class BrandRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str
    company_name: str
    email: str
    entity_type: str
    vertical: str
    country: str = "US"
    ein: Optional[str] = None
    phone: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    website: Optional[str] = None

    @field_validator("entity_type")
    @classmethod
    def validate_entity_type(cls, value: str) -> str:
        allowed = {"PRIVATE_PROFIT", "PUBLIC_PROFIT", "NON_PROFIT", "GOVERNMENT", "SOLE_PROPRIETOR"}
        if value.upper() not in allowed:
            raise ValueError(f"entity_type must be one of {sorted(allowed)}")
        return value.upper()


class CampaignRegister(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(min_length=10, max_length=4_000)
    usecase: str = "MIXED"
    sample1: Optional[str] = None
    optin_message: Optional[str] = None
    optout_message: Optional[str] = None
    help_message: Optional[str] = None
    message_flow: Optional[str] = None


async def _telnyx_credentials(ctx: TenantContext) -> dict[str, str]:
    credentials: dict[str, Any] = {}
    raw = await _load_provider_credential(ctx, "telnyx")
    if raw:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Stored Telnyx credential is invalid.") from exc
        if not isinstance(decoded, dict):
            raise HTTPException(status_code=503, detail="Stored Telnyx credential is invalid.")
        credentials.update(decoded)
    for key, env_name in (("api_key", "TELNYX_API_KEY"), ("public_key", "TELNYX_PUBLIC_KEY")):
        credentials.setdefault(key, os.getenv(env_name, ""))
    return {key: str(value or "").strip() for key, value in credentials.items()}


def _route_json(route: dict[str, Any], business_number: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        **route,
        "id": str(route["id"]),
        "tenant_id": str(route["tenant_id"]),
        "campaign_id": str(route["campaign_id"]) if route.get("campaign_id") else None,
        "public_business_number_e164": (
            str(business_number["voice_caller_id_e164"]) if business_number else None
        ),
    }


@router.get("/business-number")
async def get_business_messaging_status(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    route = await get_messaging_route(ctx)
    business_number = await get_public_business_number(ctx)
    if route is None:
        return {
            "provider": None,
            "public_business_number_e164": (
                str(business_number["voice_caller_id_e164"]) if business_number else None
            ),
            "eligibility_status": "unknown",
            "hosted_order_status": "not_started",
            "ready": False,
        }
    return await _with_readiness(ctx, _route_json(route, business_number))


async def _with_readiness(ctx: TenantContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Ready means carrier-ready (25.1): hosting AND the approved campaign AND
    the committed assignment of this number on the account Neoh sends with —
    not hosting alone. Adds ready, send_blockers and their wording."""
    from messaging_data import SEND_BLOCKERS, account_fingerprint, messaging_send_readiness
    from messaging_provider import MessagingProviderError, messaging_provider_name

    try:
        provider = messaging_provider_name()
    except MessagingProviderError:
        blockers = ["wrong_provider"]
    else:
        try:
            account = account_fingerprint(await _telnyx_credentials(ctx))
        except HTTPException:
            account = "unusable-credential"   # never matches: not ready, says why
        blockers = (await messaging_send_readiness(ctx, provider=provider, account=account))["blockers"]
    payload["ready"] = not blockers
    payload["send_blockers"] = blockers
    payload["send_blocker_messages"] = [SEND_BLOCKERS.get(b, b) for b in blockers]
    return payload


@router.post("/business-number/check-eligibility")
async def check_business_number_eligibility(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    credentials = await _telnyx_credentials(ctx)
    try:
        route = await check_eligibility(ctx, credentials=credentials)
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _route_json(route, await get_public_business_number(ctx))


@router.put("/business-number", dependencies=[Depends(require_active_subscription)])
async def put_business_number_messaging(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Connect text messages for the already-eligible, already-voice-verified
    business number. Submits the real hosted-messaging order and starts SMS
    ownership verification — never marks anything READY itself."""
    require_role(ctx, Role.BROKER_OWNER)
    credentials = await _telnyx_credentials(ctx)
    account_id = credentials.get("api_key", "")[:12]  # opaque account fingerprint only
    if not credentials.get("api_key"):
        raise HTTPException(
            status_code=503,
            detail="Telnyx is not configured for this tenant yet — connect a Telnyx provider credential first.",
        )
    try:
        route = await connect_messaging(ctx, provider_account_id=account_id, credentials=credentials)
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _route_json(route, await get_public_business_number(ctx))


@router.post("/business-number/verify/complete")
async def verify_business_number_messaging(
    body: OtpComplete,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    credentials = await _telnyx_credentials(ctx)
    try:
        route = await complete_messaging_verification(ctx, body.code, credentials=credentials)
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _route_json(route, await get_public_business_number(ctx))


@router.post("/business-number/refresh")
async def refresh_business_number_messaging(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Poll Telnyx for whether a submitted hosted order has finished carrier
    processing. Safe to call repeatedly — this is asynchronous provider work
    that can take real time, never instant."""
    require_role(ctx, Role.BROKER_OWNER)
    credentials = await _telnyx_credentials(ctx)
    try:
        route = await refresh_hosted_order_status(ctx, credentials=credentials)
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _with_readiness(ctx, _route_json(route, await get_public_business_number(ctx)))


@router.post("/business-number/documents/{doc_type}")
async def upload_hosted_document(
    doc_type: str,
    request: Request,
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Upload the LOA / current-provider invoice Telnyx requires for some
    hosted-messaging orders. Stored in Neoh's own protected object storage
    first (never a public bucket, never a permanent public URL), then
    forwarded to Telnyx's own document-upload API for this order."""
    require_role(ctx, Role.BROKER_OWNER)
    if doc_type not in ("loa", "invoice"):
        raise HTTPException(status_code=404, detail="Unknown document type.")
    route = await get_messaging_route(ctx)
    if route is None or not route.get("hosted_order_id"):
        raise HTTPException(status_code=409, detail="Connect a business number before uploading documents.")
    if (file.content_type or "") != "application/pdf":
        raise HTTPException(status_code=422, detail="Documents must be a PDF.")
    data = await file.read(_MAX_DOCUMENT_BYTES + 1)
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds the 5 MB limit.")
    if not data:
        raise HTTPException(status_code=422, detail="Uploaded document is empty.")
    if not data.startswith(b"%PDF-"):
        # The declared type is whatever the browser (or a script) said; the
        # bytes are forwarded to Telnyx as a carrier document (review UPL-3).
        raise HTTPException(status_code=422, detail="Documents must be a PDF.")

    import asyncio

    import object_storage

    sha256 = hashlib.sha256(data).hexdigest()
    storage_key = f"messaging-hosted-documents/{ctx.tenant_id}/{route['id']}/{doc_type}-{sha256[:16]}.pdf"
    await asyncio.to_thread(object_storage.put_bytes, storage_key, data, "application/pdf")

    credentials = await _telnyx_credentials(ctx)
    from messaging_provider import get_messaging_provider

    adapter = get_messaging_provider(str(route["provider"]))
    try:
        await adapter.upload_hosted_documents(
            str(route["hosted_order_id"]),
            loa_bytes=data if doc_type == "loa" else None,
            invoice_bytes=data if doc_type == "invoice" else None,
            credentials=credentials,
        )
    except Exception as exc:
        logger.exception("Telnyx document upload failed: tenant=%s doc_type=%s", ctx.tenant_id, doc_type)
        raise HTTPException(status_code=502, detail=f"Telnyx rejected the document: {exc}") from exc

    updated = await record_hosted_document(
        ctx,
        doc_type=doc_type,
        storage_key=storage_key,
        sha256=sha256,
        content_type="application/pdf",
        uploaded_by=ctx.agent_id,
    )
    return _route_json(updated, await get_public_business_number(ctx))


@router.delete("/business-number")
async def delete_business_number_messaging(
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    credentials = await _telnyx_credentials(ctx)
    try:
        route = await disconnect_messaging(ctx, credentials=credentials)
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _route_json(route, await get_public_business_number(ctx))


# ── 10DLC — tenant-wide Brand + Campaign ────────────────────────────────────


@router.get("/business/registration")
async def get_business_registration(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        brand = await conn.fetchrow(
            "SELECT id,status,provider_brand_id,failure_reason,provider_status,status_checked_at,"
            "status_check_error FROM tenant_messaging_brands WHERE tenant_id=$1::uuid",
            ctx.tenant_id,
        )
        campaign = None
        if brand is not None:
            campaign = await conn.fetchrow(
                """
                SELECT id,status,provider_campaign_id,failure_reason,provider_status,
                       status_checked_at,status_check_error,approved_at
                  FROM tenant_messaging_campaigns
                 WHERE tenant_id=$1::uuid AND brand_id=$2::uuid
                 ORDER BY created_at DESC LIMIT 1
                """,
                ctx.tenant_id,
                brand["id"],
            )
    return {
        "brand": {**dict(brand), "id": str(brand["id"])} if brand else None,
        "campaign": {**dict(campaign), "id": str(campaign["id"])} if campaign else None,
        # A submission the provider has not (or not provably) answered yet.
        "open_submissions": await open_registration_submissions(ctx),
    }


@router.put("/business/registration/brand", dependencies=[Depends(require_active_subscription)])
async def register_brand(
    body: BrandRegister,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """10DLC Brand registration — one per tenant/brokerage. Neoh is the ISV;
    each brokerage gets its own end-user Brand, never a shared generic one.

    Registered at most once at the provider (L10-09): a PUT when a brand
    exists, or while a submission is unresolved, is a 409 and reaches no
    provider — no adapter can update a brand in place yet."""
    require_role(ctx, Role.BROKER_OWNER)
    provider = _texting_provider()

    credentials = await _telnyx_credentials(ctx)
    try:
        return await register_messaging_brand(
            ctx, body.model_dump(exclude_none=True), provider=provider, credentials=credentials
        )
    except RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistrationUnconfirmed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _provider_outcomes() as exc:
        raise _provider_outcome(exc) from exc
    except RuntimeError as exc:  # DatabaseUnavailable, SealError, CryptoError under tenant_tx
        raise memory_core_offline(exc, "brand registration not recorded") from exc


@router.put("/business/registration/campaign", dependencies=[Depends(require_active_subscription)])
async def register_campaign(
    body: CampaignRegister,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """10DLC Campaign — one per brokerage at the provider (L10-09), same
    guards as register_brand."""
    require_role(ctx, Role.BROKER_OWNER)
    provider = _texting_provider()

    credentials = await _telnyx_credentials(ctx)
    try:
        return await register_messaging_campaign(
            ctx,
            description=body.description,
            usecase=body.usecase,
            extra=body.model_dump(exclude={"description", "usecase"}, exclude_none=True),
            provider=provider,
            credentials=credentials,
        )
    except RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistrationUnconfirmed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _provider_outcomes() as exc:
        raise _provider_outcome(exc) from exc
    except RuntimeError as exc:  # DatabaseUnavailable, SealError, CryptoError under tenant_tx
        raise memory_core_offline(exc, "campaign submission not recorded") from exc


@router.post("/business/registration/refresh")
async def refresh_registration(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    """Read the brokerage's 10DLC brand and campaign status from the provider
    now (25.2). A broker owner only; reads, never submits. Two owners racing
    are safe: the newer provider answer wins."""
    require_role(ctx, Role.BROKER_OWNER)
    provider = _texting_provider()
    credentials = await _telnyx_credentials(ctx)
    # Throttled: repeated clicks within 30 s answer from what was just read.
    result = await refresh_registration_status(ctx, provider=provider, credentials=credentials,
                                               min_interval_s=30)
    return {**result, "messaging": await _with_readiness(ctx, {})}


@router.post("/business/registration/campaign/assign-number", dependencies=[Depends(require_active_subscription)])
async def assign_campaign_number(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    """Assign the owner's verified business number to the approved campaign —
    at most once at the provider per number + campaign (L10-10): a repeat is
    answered from the ledger, a racing or unresolved one is a 409, an
    ambiguous provider answer is a 502 that waits for an operator."""
    require_role(ctx, Role.BROKER_OWNER)
    provider = _texting_provider()

    business_number = await get_public_business_number(ctx)
    if business_number is None:
        raise HTTPException(status_code=409, detail="Connect and verify your business number first.")
    credentials = await _telnyx_credentials(ctx)
    try:
        return await assign_messaging_campaign_number(
            ctx,
            phone_number=str(business_number["voice_caller_id_e164"]),
            provider=provider,
            credentials=credentials,
        )
    except RegistrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RegistrationUnconfirmed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MessagingDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _provider_outcomes() as exc:
        raise _provider_outcome(exc) from exc
    except RuntimeError as exc:  # DatabaseUnavailable, SealError, CryptoError under tenant_tx
        raise memory_core_offline(exc, "number assignment not recorded") from exc


# ── Telnyx webhooks (signed, tenant-safe, idempotent) ───────────────────────



async def _record_orphan_opt_out(normalized, verified=None) -> None:
    from messaging_data import resolve_opt_out_owners_by_number
    from outreach_compliance import ConsentLedger
    from tenancy import Role as _Role
    from tenancy import TenantContext as _TenantContext

    owners = await resolve_opt_out_owners_by_number(normalized.to_e164)
    if not owners:
        logger.warning("STOP to a number no tenant has held; nothing to suppress")
        return
    if verified is not None:
        # Brokerages this signing key may record an opt-out for (25.4): its
        # own, or any holder when the platform key signed it.
        allowed = [o for o in owners if verified.may_suppress_for(o["tenant_id"])]
        if not allowed:
            logger.warning("ORACLE_ALERT telnyx_stop_unattributed a STOP signed by %s matched no brokerage it "
                           "speaks for; not recorded", verified.label)
            return
        owners = allowed
    try:
        for owner in owners:
            await ConsentLedger.suppress(
                _TenantContext(agent_id=str(owner["agent_id"]), tenant_id=str(owner["tenant_id"]), role=_Role.AGENT),
                contact=normalized.from_e164,
                channel="*",
                reason="stop_keyword",
                source_text=normalized.text[:200],
            )
    except Exception as exc:
        logger.exception("Failed to record SMS opt-out on an inactive route; asking Telnyx to retry")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Opt-out not recorded yet; retry.",
        ) from exc

@router.post("/webhooks/telnyx", include_in_schema=False)
async def telnyx_webhook(request: Request) -> Response:
    """Single inbound endpoint for both message events and hosted-order
    status events — Telnyx delivers all messaging webhooks to one
    configured URL, distinguished by `data.event_type`."""
    from messaging_provider import TelnyxMessagingProvider
    from messaging_data import (
        record_delivery_update,
        record_inbound_message,
        resolve_messaging_route_by_number,
    )
    from outreach_compliance import is_stop_keyword

    import telnyx_signing

    raw_body = (await request.body()).decode("utf-8")
    adapter = TelnyxMessagingProvider()
    # 25.4: verified against every authorized signing key (platform, its
    # rotation predecessor, each brokerage's own), and what verified it
    # decides which brokerages the event may act for — never the destination.
    try:
        event, verified = await telnyx_signing.verify(raw_body, dict(request.headers))
    except telnyx_signing.NoSigningKeys as exc:
        raise HTTPException(status_code=503, detail="Telnyx webhook validation is not configured.") from exc
    except Exception as exc:
        logger.warning("Rejected invalid Telnyx webhook signature: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Telnyx signature.") from exc

    def _refuse(tenant_id: Any) -> Response:
        if verified.refusal_is_temporary(tenant_id):
            # Its credential could not be read: not a verdict — Telnyx retries.
            logger.warning("Telnyx webhook for brokerage %s deferred: its credential is unreadable", tenant_id)
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "60"})
        logger.warning("Telnyx webhook signed by %s is not authorized for brokerage %s; not applied",
                       verified.label, tenant_id)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    event_type = str(getattr(getattr(event, "data", None), "event_type", "") or "")

    if event_type == "message.received":
        normalized = adapter.normalize_inbound_message(event)
        if normalized is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        route = await resolve_messaging_route_by_number(normalized.to_e164)
        opted_out = is_stop_keyword(normalized.text)
        if route is not None and not verified.may_act_for(route["tenant_id"]):
            # Crossed destination or a reassigned number: the event comes from
            # an account that does not serve this number's brokerage. Nothing
            # is logged for it — but a STOP is never dropped: it is recorded
            # for the number's holders this key may speak for.
            if opted_out:
                logger.warning("ORACLE_ALERT telnyx_stop_on_refused_route signed by %s for brokerage %s; "
                               "recorded as an opt-out only", verified.label, route["tenant_id"])
                await _record_orphan_opt_out(normalized, verified)
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            return _refuse(route["tenant_id"])
        if route is None:
            if opted_out:
                # No active route (disconnected, suspended, offboarded) — the
                # message is not logged, but the opt-out must still be kept.
                await _record_orphan_opt_out(normalized, verified)
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            logger.warning("Inbound Telnyx message did not match an active route")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await record_inbound_message(
            tenant_id=str(route["tenant_id"]),
            agent_id=str(route["agent_id"]),
            provider="telnyx",
            provider_message_id=normalized.provider_message_id,
            from_e164=normalized.from_e164,
            to_e164=normalized.to_e164,
            text=normalized.text,
            media=normalized.media,
            opted_out=opted_out,
        )
        if opted_out:
            from outreach_compliance import ConsentLedger
            from tenancy import Role as _Role
            from tenancy import TenantContext as _TenantContext

            platform_ctx = _TenantContext(
                agent_id=str(route["agent_id"]),
                tenant_id=str(route["tenant_id"]),
                role=_Role.AGENT,
            )
            try:
                await ConsentLedger.suppress(
                    platform_ctx,
                    contact=normalized.from_e164,
                    channel="*",
                    reason="stop_keyword",
                    source_text=normalized.text[:200],
                )
            except Exception as exc:
                # A STOP that is not recorded is a consent violation waiting to
                # happen: nothing else reads sms_messages.opted_out. Answer 503
                # so Telnyx redelivers; the message insert above is idempotent
                # on the provider id and this suppression runs again (HOOK-4).
                logger.exception("Failed to record SMS opt-out suppression; asking Telnyx to retry")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Opt-out not recorded yet; retry.",
                ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if event_type in ("message.sent", "message.finalized"):
        delivery = adapter.normalize_delivery_event(event)
        if delivery is not None:
            from messaging_data import tenant_for_provider_message

            owner = await tenant_for_provider_message(delivery.provider, delivery.provider_message_id)
            if owner is None:
                # The receipt beat our own write of the message row: retried,
                # so the brokerage check runs once the row exists (never an
                # unchecked update of a row written in between).
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "30"})
            if not verified.may_act_for(owner):
                return _refuse(owner)
            known = await record_delivery_update(
                provider=delivery.provider,
                provider_message_id=delivery.provider_message_id,
                status=delivery.status,
                error_reason=delivery.error_reason,
                event_id=delivery.event_id,
                occurred_at=delivery.occurred_at,
            )
            if not known:
                # The receipt beat our own write of the message row; ask
                # Telnyx to redeliver instead of dropping it (the row would
                # otherwise sit at "queued" forever).
                return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers={"Retry-After": "30"})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if event_type in ("10dlc.brand.update", "10dlc.campaign.update", "10dlc.phone_number.update"):
        # The event is only a nudge (25.2): its status is not trusted. The
        # brokerage's registration is re-read from the provider, in the
        # background so the acknowledgement is not held up; the periodic sync
        # is the backstop if this process stops first.
        payload = getattr(getattr(event, "data", None), "payload", None) or {}
        get = payload.get if isinstance(payload, dict) else (lambda k: getattr(payload, k, None))
        provider_id = str(get("campaignId") or get("campaign_id") or get("brandId") or get("brand_id")
                          or get("phoneNumber") or get("phone_number") or "")
        if provider_id:
            task = asyncio.ensure_future(_refresh_for_provider_id(provider_id, verified))
            _REFRESH_TASKS.add(task)                   # kept until done (no GC mid-flight)
            task.add_done_callback(_REFRESH_TASKS.discard)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Hosted-order / number-order status events and anything else this
    # endpoint does not specifically act on: acknowledge without acting, so
    # Telnyx does not retry indefinitely on an event Neoh does not need.
    logger.info("Telnyx webhook event acknowledged: type=%s", event_type or "unknown")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_REFRESH_TASKS: set = set()


async def _refresh_for_provider_id(provider_id: str, verified: Any = None) -> None:
    """Re-read one brokerage's 10DLC status after a provider event (25.2)."""
    from messaging_data import tenant_for_registration
    from messaging_provider import messaging_provider_name

    try:
        tenant_id = await tenant_for_registration(provider_id)
        if not tenant_id:
            logger.info("10DLC event for an unknown registration %s ignored", provider_id[:64])
            return
        if verified is not None and not verified.may_act_for(tenant_id):
            logger.warning("10DLC event signed by %s not authorized for brokerage %s; ignored",
                           verified.label, tenant_id)
            return
        # A burst of events for one brokerage reads the provider once a minute.
        await sync_registration_for_tenant(tenant_id, provider=messaging_provider_name(), min_interval_s=60)
    except Exception:  # noqa: BLE001 — the periodic sync retries
        logger.warning("10DLC status refresh after an event failed", exc_info=True)


async def sync_registration_for_tenant(tenant_id: str, *, provider: str,
                                       min_interval_s: float = 0) -> dict[str, Any]:
    ctx = TenantContext(agent_id="10dlc-status-sync", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    try:
        credentials = await _telnyx_credentials(ctx)
    except HTTPException as exc:
        raise RuntimeError(f"Telnyx credential unusable: {exc.detail}") from exc
    return await refresh_registration_status(ctx, provider=provider, credentials=credentials,
                                             min_interval_s=min_interval_s)


async def sync_hosted_orders(*, limit: int = 200) -> dict[str, Any]:
    """25.6: read every non-final hosted order from the provider (the webhook
    acknowledges order events without acting; nothing else advanced them but
    a broker's refresh). Each route under its own agent and credential; one
    failure is recorded and the sweep goes on."""
    from messaging_data import hosted_orders_to_sync, mark_hosted_order_checked, refresh_hosted_order_status

    synced = failed = 0
    for tenant_id, agent_id in await hosted_orders_to_sync(limit=limit):
        ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
        try:
            await refresh_hosted_order_status(ctx, credentials=await _telnyx_credentials(ctx))
            synced += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.warning("hosted-order status sync failed for tenant %s", tenant_id, exc_info=True)
            # stamped anyway: a route that keeps failing must not sort first
            # forever and starve the others
            await mark_hosted_order_checked(ctx)
    logger.info("ORACLE_METRIC messaging_hosted_order_sync synced=%d failed=%d", synced, failed)
    return {"synced": synced, "failed": failed}


async def sync_open_registrations(*, limit: int = 200) -> dict[str, Any]:
    """Periodic 10DLC status sync (25.2): every brokerage whose registration
    can still change, least recently read first. One brokerage's failure is
    recorded and the sweep goes on."""
    from messaging_data import registrations_to_sync
    from messaging_provider import MessagingProviderError, messaging_provider_name

    try:
        provider = messaging_provider_name()
    except MessagingProviderError:
        logger.warning("10DLC status sync skipped: ORACLE_MESSAGING_PROVIDER is not a supported provider")
        return {"synced": 0, "failed": 0, "skipped": "provider"}
    synced = failed = 0
    for tenant_id in await registrations_to_sync(limit=limit):
        try:
            await sync_registration_for_tenant(tenant_id, provider=provider)
            synced += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.warning("10DLC status sync failed for tenant %s", tenant_id, exc_info=True)
    logger.info("ORACLE_METRIC messaging_registration_sync synced=%d failed=%d", synced, failed)
    return {"synced": synced, "failed": failed}
