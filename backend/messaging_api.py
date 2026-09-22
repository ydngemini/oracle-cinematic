"""Authenticated messaging (SMS/MMS) configuration and signed Telnyx webhooks.

Mirrors telephony_api.py's shape for the messaging rail. Endpoints live under
/api/messaging so provider terminology never has to leak into product-facing
paths (matches /api/telephony's pattern for voice).
"""

from __future__ import annotations

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
    check_eligibility,
    complete_messaging_verification,
    connect_messaging,
    disconnect_messaging,
    get_messaging_route,
    get_public_business_number,
    record_hosted_document,
    refresh_hosted_order_status,
)
from tenancy import Role, TenantContext, require_context, require_role

logger = logging.getLogger("oracle.messaging")

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
    payload = _route_json(route, business_number)
    payload["ready"] = route.get("hosted_order_status") == "active"
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


@router.put("/business-number")
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
    return _route_json(route, await get_public_business_number(ctx))


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
            "SELECT id,status,provider_brand_id,failure_reason FROM tenant_messaging_brands WHERE tenant_id=$1::uuid",
            ctx.tenant_id,
        )
        campaign = None
        if brand is not None:
            campaign = await conn.fetchrow(
                """
                SELECT id,status,provider_campaign_id,failure_reason
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
    }


@router.put("/business/registration/brand")
async def register_brand(
    body: BrandRegister,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """10DLC Brand registration — one per tenant/brokerage. Neoh is the ISV;
    each brokerage gets its own end-user Brand, never a shared generic one."""
    require_role(ctx, Role.BROKER_OWNER)
    from db.connection import tenant_tx
    from messaging_provider import get_messaging_provider, messaging_provider_name

    credentials = await _telnyx_credentials(ctx)
    adapter = get_messaging_provider(messaging_provider_name())
    fields = body.model_dump(exclude_none=True)
    try:
        result = await adapter.create_brand(fields, credentials=credentials)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telnyx rejected the brand registration: {exc}") from exc

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_messaging_brands (
                tenant_id,provider,provider_brand_id,status,display_name,company_name,
                ein,entity_type,vertical,email,phone,street,city,state,postal_code,
                country,website,submitted_at
            ) VALUES (
                $1::uuid,'telnyx',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,now()
            )
            ON CONFLICT (tenant_id) DO UPDATE SET
                provider_brand_id=EXCLUDED.provider_brand_id,
                status=EXCLUDED.status,
                display_name=EXCLUDED.display_name,
                company_name=EXCLUDED.company_name,
                ein=EXCLUDED.ein,
                entity_type=EXCLUDED.entity_type,
                vertical=EXCLUDED.vertical,
                email=EXCLUDED.email,
                phone=EXCLUDED.phone,
                street=EXCLUDED.street,
                city=EXCLUDED.city,
                state=EXCLUDED.state,
                postal_code=EXCLUDED.postal_code,
                country=EXCLUDED.country,
                website=EXCLUDED.website,
                submitted_at=now()
            RETURNING id,status
            """,
            ctx.tenant_id,
            result.reference,
            result.status,
            body.display_name,
            body.company_name,
            body.ein,
            body.entity_type,
            body.vertical,
            body.email,
            body.phone,
            body.street,
            body.city,
            body.state,
            body.postal_code,
            body.country,
            body.website,
        )
    return {"brand_id": str(row["id"]), "provider_brand_id": result.reference, "status": row["status"]}


@router.put("/business/registration/campaign")
async def register_campaign(
    body: CampaignRegister,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    from db.connection import tenant_tx
    from messaging_provider import get_messaging_provider, messaging_provider_name

    async with tenant_tx(ctx) as conn:
        brand = await conn.fetchrow(
            "SELECT id,provider_brand_id,status FROM tenant_messaging_brands WHERE tenant_id=$1::uuid",
            ctx.tenant_id,
        )
    if brand is None or not brand["provider_brand_id"]:
        raise HTTPException(status_code=409, detail="Register a Brand before submitting a Campaign.")

    credentials = await _telnyx_credentials(ctx)
    adapter = get_messaging_provider(messaging_provider_name())
    extra = body.model_dump(exclude={"description", "usecase"}, exclude_none=True)
    try:
        result = await adapter.submit_campaign(
            brand_id=str(brand["provider_brand_id"]),
            description=body.description,
            usecase=body.usecase,
            credentials=credentials,
            **extra,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telnyx rejected the campaign submission: {exc}") from exc

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_messaging_campaigns
                (tenant_id,brand_id,provider,provider_campaign_id,status,usecase,description,submitted_at)
            VALUES ($1::uuid,$2::uuid,'telnyx',$3,$4,$5,$6,now())
            RETURNING id,status
            """,
            ctx.tenant_id,
            brand["id"],
            result.reference,
            result.status,
            body.usecase,
            body.description,
        )
    return {"campaign_id": str(row["id"]), "provider_campaign_id": result.reference, "status": row["status"]}


@router.post("/business/registration/campaign/assign-number")
async def assign_campaign_number(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    from db.connection import tenant_tx
    from messaging_provider import get_messaging_provider, messaging_provider_name

    business_number = await get_public_business_number(ctx)
    if business_number is None:
        raise HTTPException(status_code=409, detail="Connect and verify your business number first.")
    async with tenant_tx(ctx) as conn:
        campaign = await conn.fetchrow(
            """
            SELECT c.id,c.provider_campaign_id
              FROM tenant_messaging_campaigns c
              JOIN tenant_messaging_brands b ON b.id=c.brand_id
             WHERE c.tenant_id=$1::uuid AND c.status='approved'
             ORDER BY c.created_at DESC LIMIT 1
            """,
            ctx.tenant_id,
        )
    if campaign is None or not campaign["provider_campaign_id"]:
        raise HTTPException(status_code=409, detail="No approved Campaign to assign this number to.")

    credentials = await _telnyx_credentials(ctx)
    adapter = get_messaging_provider(messaging_provider_name())
    try:
        await adapter.assign_number_to_campaign(
            str(campaign["provider_campaign_id"]),
            str(business_number["voice_caller_id_e164"]),
            credentials=credentials,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telnyx rejected the number assignment: {exc}") from exc

    route = await get_messaging_route(ctx)
    if route is not None:
        from db.connection import tenant_tx as _tx

        async with _tx(ctx) as conn:
            await conn.execute(
                "UPDATE messaging_routes SET campaign_id=$2::uuid, updated_at=now() WHERE tenant_id=$1::uuid AND id=$3::uuid",
                ctx.tenant_id,
                campaign["id"],
                route["id"],
            )
    return {"campaign_id": str(campaign["id"]), "assigned": True}


# ── Telnyx webhooks (signed, tenant-safe, idempotent) ───────────────────────


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

    raw_body = (await request.body()).decode("utf-8")
    public_key = os.getenv("TELNYX_PUBLIC_KEY", "").strip()
    if not public_key:
        raise HTTPException(status_code=503, detail="Telnyx webhook validation is not configured.")

    adapter = TelnyxMessagingProvider()
    try:
        event = adapter.validate_webhook(
            raw_body, dict(request.headers), credentials={"public_key": public_key}
        )
    except Exception as exc:
        logger.warning("Rejected invalid Telnyx webhook signature: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Telnyx signature.") from exc

    event_type = str(getattr(getattr(event, "data", None), "event_type", "") or "")

    if event_type == "message.received":
        normalized = adapter.normalize_inbound_message(event)
        if normalized is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        route = await resolve_messaging_route_by_number(normalized.to_e164)
        if route is None:
            logger.warning("Inbound Telnyx message did not match an active route")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        opted_out = is_stop_keyword(normalized.text)
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
            except Exception:
                logger.exception("Failed to record SMS opt-out suppression")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if event_type in ("message.sent", "message.finalized"):
        delivery = adapter.normalize_delivery_event(event)
        if delivery is not None:
            await record_delivery_update(
                provider=delivery.provider,
                provider_message_id=delivery.provider_message_id,
                status=delivery.status,
                error_reason=delivery.error_reason,
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Hosted-order / number-order status events and anything else this
    # endpoint does not specifically act on: acknowledge without acting, so
    # Telnyx does not retry indefinitely on an event Neoh does not need.
    logger.info("Telnyx webhook event acknowledged: type=%s", event_type or "unknown")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
