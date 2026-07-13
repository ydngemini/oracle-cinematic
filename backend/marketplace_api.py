"""Contract-linked internal marketplace and objective buyer matching."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from approval_service import create_approval, decide_approval
from db.connection import tenant_tx
from marketplace_engine import rank_buyer_request
from platform_policy import ActionRisk, Feature, enforce_public_property_data, require_feature
from tenancy import Role, TenantContext, require_context

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"truthful_summary", "criteria", "criteria_trace", "explicit_preferences"}:
            result[key] = _json(value)
        elif isinstance(value, list):
            result[key] = list(value)
    return result


class PublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: str = Field(default="platform", pattern=r"^(tenant|platform)$")
    asking_price: Optional[float] = Field(default=None, ge=0, le=1_000_000_000)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=8, max_length=500)


class BuyerProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: UUID
    states: list[str] = Field(default_factory=list, max_length=51)
    counties: list[str] = Field(default_factory=list, max_length=200)
    property_types: list[str] = Field(default_factory=list, max_length=50)
    min_price: Optional[float] = Field(default=None, ge=0)
    max_price: Optional[float] = Field(default=None, ge=0)
    min_beds: Optional[int] = Field(default=None, ge=0, le=100)
    min_sqft: Optional[int] = Field(default=None, ge=0)
    max_rehab: Optional[float] = Field(default=None, ge=0)
    strategies: list[str] = Field(default_factory=list, max_length=50)
    verification_status: str = Field(
        default="unverified",
        pattern=r"^(unverified|identity_verified|funds_verified)$",
    )
    acquisition_history_verified: bool = False
    explicit_preferences: dict[str, Any] = Field(default_factory=dict)


class BuyerRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    buyer_profile_id: UUID
    request_name: str = Field(min_length=1, max_length=160)
    criteria: dict[str, Any]
    expires_at: Optional[datetime] = None


class BiddingMessageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=4_000)
    channel: str = Field(default="email", pattern=r"^(email|sms)$")


@router.get("")
async def browse_marketplace(
    state_code: Optional[str] = Query(default=None, min_length=2, max_length=2),
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    async with tenant_tx(ctx) as conn:
        if state_code:
            rows = await conn.fetch(
                """
                SELECT p.*
                FROM marketplace_publications p
                WHERE p.state IN ('published','under_offer')
                  AND upper(p.truthful_summary->>'state')=$1
                ORDER BY p.published_at DESC LIMIT $2
                """,
                state_code.upper(),
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT p.*
                FROM marketplace_publications p
                WHERE p.state IN ('published','under_offer')
                ORDER BY p.published_at DESC LIMIT $1
                """,
                limit,
            )
    return {"publications": [_row(row) for row in rows]}


@router.post("/publications/from-contract/{contract_id}", status_code=status.HTTP_201_CREATED)
async def create_publication_from_contract(
    contract_id: UUID,
    body: PublicationCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    async with tenant_tx(ctx) as conn:
        contract = await conn.fetchrow(
            """
            SELECT * FROM contract_documents
            WHERE id=$1 AND status='signed'
              AND document_type IN ('assignment','seller_purchase')
            FOR SHARE
            """,
            contract_id,
        )
        if not contract:
            raise HTTPException(
                status_code=409,
                detail="A signed assignment or seller contract is required.",
            )
        if not contract["lead_id"]:
            raise HTTPException(status_code=409, detail="Contract is not linked to a property lead.")
        lead = await conn.fetchrow("SELECT * FROM leads WHERE id=$1", contract["lead_id"])
        if not lead:
            raise HTTPException(status_code=404, detail="Contract property was not found.")
        underwriting = _json(lead["underwriting"]) or {}
        summary = {
            "address": lead["address"],
            "state": lead["state"],
            "parcel_id": lead["parcel_id"],
            "beds": lead["beds"],
            "baths": float(lead["baths"]) if lead["baths"] is not None else None,
            "sqft": lead["sqft"],
            "arv": underwriting.get("arv") or underwriting.get("arv_estimate"),
            "rehab": underwriting.get("rehab") or underwriting.get("rehab_estimate"),
            "source": "signed_contract_and_property_dossier",
            "contract_document_id": str(contract_id),
        }
        enforce_public_property_data(summary)
        asking_price = body.asking_price
        if asking_price is None:
            asking_price = lead["asking_price"] or underwriting.get("disposition_price")
        row = await conn.fetchrow(
            """
            INSERT INTO marketplace_publications (
                tenant_id, lead_id, transaction_id, contract_document_id,
                state, visibility, truthful_summary, asking_price, created_by
            ) VALUES ($1::uuid,$2,$3,$4,'draft',$5,$6::jsonb,$7,$8)
            ON CONFLICT (tenant_id,lead_id) DO UPDATE
               SET contract_document_id=EXCLUDED.contract_document_id,
                   truthful_summary=EXCLUDED.truthful_summary,
                   asking_price=EXCLUDED.asking_price,
                   visibility=EXCLUDED.visibility,
                   updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            contract["lead_id"],
            contract["transaction_id"],
            contract_id,
            body.visibility,
            json.dumps(summary),
            asking_price,
            ctx.agent_id,
        )
    publication = _row(row)
    approval_payload = {
        "publication_id": publication["id"],
        "visibility": publication["visibility"],
        "asking_price": float(publication["asking_price"]) if publication["asking_price"] is not None else None,
        "truthful_summary": publication["truthful_summary"],
    }
    approval = await create_approval(
        ctx,
        action_type="marketplace:publish",
        risk=ActionRisk.FINANCIAL,
        target_type="marketplace_publication",
        target_id=publication["id"],
        draft_payload=approval_payload,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_publications
               SET approval_id=$2::uuid WHERE id=$1::uuid
            RETURNING *
            """,
            publication["id"],
            str(approval["id"]),
        )
    return {"publication": _row(row), "approval": approval}


@router.post("/publications/{publication_id}/publish")
async def publish_publication(
    publication_id: UUID,
    body: Decision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    async with tenant_tx(ctx) as conn:
        publication = await conn.fetchrow(
            "SELECT * FROM marketplace_publications WHERE id=$1 FOR UPDATE", publication_id
        )
        if not publication:
            raise HTTPException(status_code=404, detail="Publication not found.")
        if publication["state"] != "draft":
            raise HTTPException(status_code=409, detail=f"Publication is {publication['state']}.")
    approval = await decide_approval(
        ctx,
        str(publication["approval_id"]),
        decision="approved",
        reason=body.reason,
    )
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE marketplace_publications
               SET state='published', published_at=now(), approved_by=$2, updated_at=now()
             WHERE id=$1 RETURNING *
            """,
            publication_id,
            ctx.agent_id,
        )
    return {"publication": _row(row), "approval": approval}


@router.put("/buyers/profile")
async def upsert_buyer_profile(
    body: BuyerProfileUpsert,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    enforce_public_property_data(body.model_dump())
    states = sorted({state.upper() for state in body.states if len(state) == 2})
    async with tenant_tx(ctx) as conn:
        client = await conn.fetchrow("SELECT client_type FROM clients WHERE id=$1", body.client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Buyer client not found.")
        if client["client_type"] not in {"buyer", "both"}:
            raise HTTPException(status_code=409, detail="Client is not marked as a buyer.")
        row = await conn.fetchrow(
            """
            INSERT INTO buyer_profiles (
                tenant_id,client_id,states,counties,property_types,min_price,
                max_price,min_beds,min_sqft,max_rehab,strategies,
                verification_status,acquisition_history_verified,explicit_preferences
            ) VALUES ($1::uuid,$2,$3::char(2)[],$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
            ON CONFLICT (tenant_id,client_id) DO UPDATE SET
                states=EXCLUDED.states,counties=EXCLUDED.counties,
                property_types=EXCLUDED.property_types,min_price=EXCLUDED.min_price,
                max_price=EXCLUDED.max_price,min_beds=EXCLUDED.min_beds,
                min_sqft=EXCLUDED.min_sqft,max_rehab=EXCLUDED.max_rehab,
                strategies=EXCLUDED.strategies,
                verification_status=EXCLUDED.verification_status,
                acquisition_history_verified=EXCLUDED.acquisition_history_verified,
                explicit_preferences=EXCLUDED.explicit_preferences,updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            body.client_id,
            states,
            body.counties,
            body.property_types,
            body.min_price,
            body.max_price,
            body.min_beds,
            body.min_sqft,
            body.max_rehab,
            body.strategies,
            body.verification_status,
            body.acquisition_history_verified,
            json.dumps(body.explicit_preferences),
        )
    return _row(row)


@router.post("/buyers/requests", status_code=status.HTTP_201_CREATED)
async def create_buyer_request(
    body: BuyerRequestCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    enforce_public_property_data(body.criteria)
    async with tenant_tx(ctx) as conn:
        profile = await conn.fetchval("SELECT 1 FROM buyer_profiles WHERE id=$1", body.buyer_profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Buyer profile not found.")
        row = await conn.fetchrow(
            """
            INSERT INTO buyer_requests (
                tenant_id,buyer_profile_id,request_name,criteria,expires_at,created_by
            ) VALUES ($1::uuid,$2,$3,$4::jsonb,$5,$6) RETURNING *
            """,
            ctx.tenant_id,
            body.buyer_profile_id,
            body.request_name,
            json.dumps(body.criteria),
            body.expires_at,
            ctx.agent_id,
        )
    return _row(row)


@router.post("/publications/{publication_id}/match")
async def match_buyers(
    publication_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    async with tenant_tx(ctx) as conn:
        publication = await conn.fetchrow(
            """
            SELECT p.*
            FROM marketplace_publications p
            WHERE p.id=$1 AND p.state IN ('published','under_offer')
            """,
            publication_id,
        )
        if not publication:
            raise HTTPException(status_code=404, detail="Published property not found.")
        requests = await conn.fetch(
            """
            SELECT r.*, p.states,p.counties,p.property_types,p.min_price,p.max_price,
                   p.min_beds,p.min_sqft,p.max_rehab,p.strategies,
                   p.verification_status,p.acquisition_history_verified
            FROM buyer_requests r JOIN buyer_profiles p ON p.id=r.buyer_profile_id
            WHERE r.status='active' AND p.active=true
              AND (r.expires_at IS NULL OR r.expires_at>now())
            """
        )
        summary = _json(publication["truthful_summary"]) or {}
        facts = {
            **summary,
            "asking_price": publication["asking_price"],
            "rehab": summary.get("rehab"),
        }
        matches: list[dict[str, Any]] = []
        for request in requests:
            match = rank_buyer_request(facts, dict(request), _json(request["criteria"]) or {})
            row = await conn.fetchrow(
                """
                INSERT INTO marketplace_matches (
                    tenant_id,publication_id,buyer_request_id,match_score,
                    criteria_trace,acquisition_history_verified
                ) VALUES ($1::uuid,$2,$3,$4,$5::jsonb,$6)
                ON CONFLICT (publication_id,buyer_request_id) DO UPDATE SET
                    match_score=EXCLUDED.match_score,
                    criteria_trace=EXCLUDED.criteria_trace,
                    acquisition_history_verified=EXCLUDED.acquisition_history_verified,
                    updated_at=now()
                RETURNING *
                """,
                ctx.tenant_id,
                publication_id,
                request["id"],
                match["match_score"],
                json.dumps(match["criteria_trace"]),
                match["acquisition_history_verified"],
            )
            matches.append(_row(row))
    matches.sort(key=lambda item: float(item["match_score"]), reverse=True)
    return {"publication_id": str(publication_id), "matches": matches}


@router.post("/publications/{publication_id}/bidding-message")
async def draft_bidding_message(
    publication_id: UUID,
    body: BiddingMessageDraft,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.MARKETPLACE)
    enforce_public_property_data(body.model_dump())
    async with tenant_tx(ctx) as conn:
        publication = await conn.fetchrow(
            "SELECT id,state FROM marketplace_publications WHERE id=$1", publication_id
        )
        if not publication:
            raise HTTPException(status_code=404, detail="Publication not found.")
    platform_ctx = TenantContext(
        agent_id="marketplace-truth-check",
        tenant_id=os.getenv(
            "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(platform_ctx) as conn:
        verified_offers = await conn.fetchval(
            "SELECT count(*)::int FROM marketplace_matches WHERE publication_id=$1 AND state='offer'",
            publication_id,
        )
    claims_competition = bool(
        re.search(r"\b(multiple offers?|competing offers?|bidding war|other buyers?)\b", body.message, re.I)
    )
    if claims_competition and int(verified_offers or 0) < 2:
        raise HTTPException(
            status_code=422,
            detail="Competition claim is unsupported by at least two recorded offers.",
        )
    payload = {
        "publication_id": str(publication_id),
        "channel": body.channel,
        "message": body.message,
        "verified_offer_count": int(verified_offers or 0),
    }
    approval = await create_approval(
        ctx,
        action_type="marketplace:bidding_message",
        risk=ActionRisk.BIDDING_MESSAGE,
        target_type="marketplace_publication",
        target_id=str(publication_id),
        draft_payload=payload,
        expires_in_minutes=240,
    )
    return {"draft": payload, "approval": approval, "send_state": "not_sent"}


@router.post("/bidding-messages/{approval_id}/approve")
async def approve_bidding_message(
    approval_id: UUID,
    body: Decision,
    ctx: TenantContext = Depends(require_context),
):
    approval = await decide_approval(
        ctx, str(approval_id), decision="approved", reason=body.reason
    )
    return {
        "approval": approval,
        "approved_draft": approval["draft_payload"],
        "send_state": "approved_not_sent",
        "next_step": "Create an approval-bound EMAIL or SMS command.",
    }
