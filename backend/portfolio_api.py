"""Brokerage portfolio, party, milestone, and deadline operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


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
        elif key in {"metadata", "result", "payload"}:
            result[key] = _json(value)
    return result


class PartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    party_role: str = Field(pattern=r"^(seller|buyer|assignor|assignee|agent|broker|attorney|title|lender|joint_venture)$")
    display_name: str = Field(min_length=1, max_length=240)
    client_id: Optional[UUID] = None
    verified_at: Optional[datetime] = None


class MilestoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    milestone_type: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    due_at: Optional[datetime] = None
    assigned_to: Optional[str] = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MilestoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: str = Field(pattern=r"^(pending|at_risk|complete|waived|cancelled)$")
    due_at: Optional[datetime] = None
    assigned_to: Optional[str] = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("")
async def portfolio_dashboard(ctx: TenantContext = Depends(require_context)):
    """One consistent snapshot for the portfolio dashboard."""
    async with tenant_tx(ctx) as conn:
        active_contracts = await conn.fetch(
            """
            SELECT l.id, l.parcel_id, COALESCE(l.payload->>'address','') AS address,
                   l.dossier_status,
                   l.contract_execution_date, l.contract_expires_at,
                   GREATEST(0, CEIL(EXTRACT(EPOCH FROM
                       (l.contract_expires_at-now()))/86400))::int AS days_remaining,
                   c.id AS seller_client_id, c.full_name AS seller_name
            FROM leads l
            LEFT JOIN clients c ON c.id=l.seller_client_id
            WHERE l.dossier_status IN ('under_contract','marketing','assigned')
            ORDER BY l.contract_expires_at NULLS LAST, l.updated_at DESC
            LIMIT 100
            """
        )
        communication = await conn.fetchrow(
            """
            WITH outbound AS (
                SELECT count(*)::int AS messages,
                       count(DISTINCT COALESCE(thread_id::text,client_id::text,lead_id::text))::int AS threads
                FROM interaction_logs
                WHERE created_at >= now()-interval '30 days' AND direction='outbound'
            ), inbound AS (
                SELECT count(*)::int AS messages,
                       count(DISTINCT COALESCE(thread_id::text,client_id::text,lead_id::text))::int AS threads
                FROM interaction_logs
                WHERE created_at >= now()-interval '30 days' AND direction='inbound'
            )
            SELECT outbound.messages AS outbound_messages,
                   outbound.threads AS outbound_threads,
                   inbound.messages AS inbound_messages,
                   inbound.threads AS responded_threads,
                   CASE WHEN outbound.threads=0 THEN NULL
                        ELSE round(inbound.threads::numeric/outbound.threads,4) END AS response_rate
            FROM outbound, inbound
            """
        )
        ghosting = await conn.fetch(
            """
            SELECT c.id, c.full_name, c.client_type, c.stage, c.last_contacted_at,
                   EXTRACT(EPOCH FROM (now()-c.last_contacted_at))/3600 AS hours_silent
            FROM clients c
            WHERE c.archived_at IS NULL
              AND c.stage IN ('lead','active','nurture','under_contract')
              AND c.last_contacted_at < now()-interval '72 hours'
              AND NOT EXISTS (
                  SELECT 1 FROM interaction_logs i
                  WHERE i.client_id=c.id AND i.direction='inbound'
                    AND i.created_at > c.last_contacted_at
              )
            ORDER BY c.last_contacted_at ASC
            LIMIT 100
            """
        )
        milestones = await conn.fetch(
            """
            SELECT m.*, t.state_code, t.status AS transaction_status
            FROM transaction_milestones m
            JOIN transactions t ON t.id=m.transaction_id
            WHERE m.status IN ('pending','at_risk')
            ORDER BY m.due_at NULLS LAST, m.created_at
            LIMIT 150
            """
        )
        title_risks = await conn.fetch(
            """
            SELECT property_key, finding_type, match_status, chain_gap,
                   amount, recorded_at, review_status, created_at
            FROM title_findings
            WHERE review_status='required'
            ORDER BY chain_gap DESC, created_at DESC
            LIMIT 100
            """
        )
        zoning_opportunities = await conn.fetch(
            """
            SELECT id, property_key, zoning_district, max_far,
                   remaining_buildable_sqft, permitted_uses, review_status, created_at
            FROM zoning_analyses
            WHERE remaining_buildable_sqft > 0
            ORDER BY remaining_buildable_sqft DESC
            LIMIT 100
            """
        )
        intelligence_alerts = await conn.fetch(
            """
            SELECT id, property_key, analysis_type, confidence, model_version,
                   result, professional_review_status, created_at
            FROM intelligence_scores
            WHERE created_at >= now()-interval '30 days'
              AND (professional_review_status='required' OR confidence < 0.6)
            ORDER BY created_at DESC
            LIMIT 100
            """
        )

    comms = _row(communication) if communication else {}
    rate = comms.get("response_rate")
    if rate is not None:
        rate = float(rate)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "active_contracts": len(active_contracts),
            "response_rate_30d": rate,
            "ghosting_72h": len(ghosting),
            "deadlines_at_risk": sum(
                1 for item in milestones if item["status"] == "at_risk"
            ),
            "unreviewed_title_risks": len(title_risks),
            "zoning_opportunities": len(zoning_opportunities),
        },
        "communication": comms,
        "active_contracts": [_row(row) for row in active_contracts],
        "ghosting": [_row(row) for row in ghosting],
        "milestones": [_row(row) for row in milestones],
        "title_risks": [_row(row) for row in title_risks],
        "zoning_opportunities": [_row(row) for row in zoning_opportunities],
        "intelligence_alerts": [_row(row) for row in intelligence_alerts],
    }


@router.get("/transactions/{transaction_id}")
async def transaction_detail(
    transaction_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        transaction = await conn.fetchrow(
            "SELECT * FROM transactions WHERE id=$1", transaction_id
        )
        if not transaction:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        parties = await conn.fetch(
            "SELECT * FROM transaction_parties WHERE transaction_id=$1 ORDER BY created_at",
            transaction_id,
        )
        milestones = await conn.fetch(
            """
            SELECT * FROM transaction_milestones
            WHERE transaction_id=$1 ORDER BY due_at NULLS LAST, created_at
            """,
            transaction_id,
        )
    return {
        "transaction": _row(transaction),
        "parties": [_row(row) for row in parties],
        "milestones": [_row(row) for row in milestones],
    }


@router.post("/transactions/{transaction_id}/parties", status_code=status.HTTP_201_CREATED)
async def add_party(
    transaction_id: UUID,
    body: PartyCreate,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        exists = await conn.fetchval("SELECT 1 FROM transactions WHERE id=$1", transaction_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        row = await conn.fetchrow(
            """
            INSERT INTO transaction_parties (
                tenant_id, transaction_id, party_role, client_id,
                display_name, verified_at
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6) RETURNING *
            """,
            ctx.tenant_id,
            transaction_id,
            body.party_role,
            body.client_id,
            body.display_name,
            body.verified_at,
        )
    return _row(row)


@router.post("/transactions/{transaction_id}/milestones", status_code=status.HTTP_201_CREATED)
async def add_milestone(
    transaction_id: UUID,
    body: MilestoneCreate,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        exists = await conn.fetchval("SELECT 1 FROM transactions WHERE id=$1", transaction_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        row = await conn.fetchrow(
            """
            INSERT INTO transaction_milestones (
                tenant_id, transaction_id, milestone_type, title,
                due_at, assigned_to, metadata
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (transaction_id,milestone_type) DO UPDATE
               SET title=EXCLUDED.title, due_at=EXCLUDED.due_at,
                   assigned_to=EXCLUDED.assigned_to, metadata=EXCLUDED.metadata,
                   updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            transaction_id,
            body.milestone_type,
            body.title,
            body.due_at,
            body.assigned_to,
            json.dumps(body.metadata),
        )
    return _row(row)


@router.patch("/milestones/{milestone_id}")
async def update_milestone(
    milestone_id: UUID,
    body: MilestoneUpdate,
    ctx: TenantContext = Depends(require_context),
):
    completed_at = datetime.now(timezone.utc) if body.status == "complete" else None
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE transaction_milestones
               SET status=$2, due_at=$3, assigned_to=$4, metadata=$5::jsonb,
                   completed_at=$6, updated_at=now()
             WHERE id=$1
            RETURNING *
            """,
            milestone_id,
            body.status,
            body.due_at,
            body.assigned_to,
            json.dumps(body.metadata),
            completed_at,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    return _row(row)
