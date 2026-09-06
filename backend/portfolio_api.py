"""Brokerage portfolio, party, milestone, and deadline operations."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

import outcome_memory
from billing_usage import record_usage

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
logger = logging.getLogger("oracle.portfolio")

PartyRole = Literal[
    "seller",
    "buyer",
    "assignor",
    "assignee",
    "agent",
    "broker",
    "attorney",
    "title",
    "lender",
    "joint_venture",
]
PropertySource = Literal["pipeline", "mls"]
FinancingType = Literal["cash", "conventional", "fha", "va", "usda", "other"]


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
        elif isinstance(value, date):
            result[key] = value.isoformat()
        elif key in {
            "metadata",
            "result",
            "payload",
            "source_provenance",
            "contingencies",
        }:
            result[key] = _json(value)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    parsed = _json(value)
    return parsed if isinstance(parsed, dict) else {}


def _version_conflict(resource: str, current_version: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "version_conflict",
            "resource": resource,
            "current_version": current_version,
        },
    )


def _terms_error(values: dict[str, Any]) -> Optional[str]:
    purchase_price = values.get("purchase_price")
    earnest_money = values.get("earnest_money")
    if (
        purchase_price is not None
        and earnest_money is not None
        and earnest_money > purchase_price
    ):
        return "earnest_money cannot exceed purchase_price"

    closing_deadline = values.get("closing_deadline")
    if closing_deadline is not None:
        for field in (
            "offer_deadline",
            "inspection_deadline",
            "financing_deadline",
        ):
            deadline = values.get(field)
            if deadline is not None and deadline > closing_deadline:
                return f"{field} cannot be after closing_deadline"
    return None


async def _property_anchor(
    conn: Any,
    ctx: TenantContext,
    property_source: PropertySource,
    property_id: UUID,
) -> dict[str, Any]:
    """Resolve only the property the caller explicitly selected.

    Pipeline lookup is both RLS-bound and explicitly tenant-filtered. Normalized
    MLS rows are shared reference data, so they have no tenant predicate. This
    function intentionally never consults showings or client history.
    """
    if property_source == "pipeline":
        row = await conn.fetchrow(
            """
            SELECT id, parcel_id, state AS state_code,
                   COALESCE(NULLIF(address,''),NULLIF(payload->>'address','')) AS address,
                   COALESCE(payload->>'city','') AS city,
                   COALESCE(payload->>'zip_code',payload->>'zip','') AS postal_code,
                   COALESCE(NULLIF(payload->>'property_type',''),'residential_1_4')
                       AS property_type,
                   updated_at
              FROM leads
             WHERE id=$1 AND tenant_id=$2::uuid
            """,
            property_id,
            ctx.tenant_id,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT id, mls_id, mls_number, state_code, address, city,
                   zip_code AS postal_code, property_type, last_updated AS updated_at
              FROM oracle_mls_listings
             WHERE id=$1 AND mls_id <> 'rentcast'
            """,
            property_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail="Property source not found.")

    anchor = dict(row)
    state_code = str(anchor.get("state_code") or "").strip().upper()
    address = str(anchor.get("address") or "").strip()
    if len(state_code) != 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Property source is missing a two-letter state code.",
        )
    if not address:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Property source is missing an address.",
        )

    provenance: dict[str, Any] = {
        "source": property_source,
        "source_id": str(property_id),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    source_updated_at = anchor.get("updated_at")
    if source_updated_at is not None:
        provenance["source_updated_at"] = (
            source_updated_at.isoformat()
            if isinstance(source_updated_at, (date, datetime))
            else str(source_updated_at)
        )
    if property_source == "pipeline":
        provenance["parcel_id"] = anchor.get("parcel_id")
    else:
        provenance["mls_id"] = anchor.get("mls_id")
        provenance["mls_number"] = anchor.get("mls_number")

    return {
        "state_code": state_code,
        "address": address,
        "city": str(anchor.get("city") or "").strip() or None,
        "postal_code": str(anchor.get("postal_code") or "").strip() or None,
        "property_type": str(anchor.get("property_type") or "residential_1_4"),
        "lead_id": property_id if property_source == "pipeline" else None,
        "mls_listing_id": property_id if property_source == "mls" else None,
        "source_provenance": provenance,
    }


class PartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    party_role: PartyRole
    display_name: str = Field(min_length=1, max_length=240)
    client_id: Optional[UUID] = None
    verified_at: Optional[datetime] = None


class TransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    property_source: PropertySource
    property_id: UUID
    client_id: Optional[UUID] = None
    party_role: Optional[PartyRole] = None
    purchase_price: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    earnest_money: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    financing_amount: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    offer_deadline: Optional[date] = None
    inspection_deadline: Optional[date] = None
    financing_deadline: Optional[date] = None
    closing_deadline: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_links_and_terms(self):
        if (self.client_id is None) != (self.party_role is None):
            raise ValueError("client_id and party_role must be provided together")
        error = _terms_error(self.model_dump())
        if error:
            raise ValueError(error)
        return self


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    purchase_price: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    earnest_money: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    financing_amount: Optional[Decimal] = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    offer_deadline: Optional[date] = None
    inspection_deadline: Optional[date] = None
    financing_deadline: Optional[date] = None
    closing_deadline: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def require_change(self):
        if not (self.model_fields_set - {"version"}):
            raise ValueError("at least one transaction field must be provided")
        return self


class TransactionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


#: Mirrors transactions_lost_reason_chk (0098). A closed list because the
#: evaluator has to COUNT loss causes; free text is kept alongside as the
#: agent's own words, the same (code, verbatim) pair agent_decisions uses.
LOST_REASON_CODES: tuple[str, ...] = (
    "price", "financing", "inspection", "competing_offer",
    "client_withdrew", "listing_expired", "other",
)

#: The states a deal can die from. A closed deal is not lost; a cancelled one
#: was never a deal. Kept as data so the rule is testable without a database.
LOSABLE_STATES: frozenset[str] = frozenset({"active", "under_contract"})


class TransactionLose(BaseModel):
    """A deal that died, with why.

    'cancelled' already existed and carries nothing — no timestamp, no reason,
    no value — so a lost deal was indistinguishable from an abandoned draft
    and Outcome Memory had no negative signal to learn from.
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    reason_code: Literal[
        "price", "financing", "inspection", "competing_offer",
        "client_withdrew", "listing_expired", "other",
    ]
    reason: Optional[str] = Field(default=None, max_length=500)


class OfferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    earnest_money: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=14, decimal_places=2
    )
    financing_type: FinancingType = "cash"
    proposed_closing_date: Optional[date] = None
    expires_at: Optional[datetime] = None
    contingencies: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_amounts(self):
        if self.earnest_money > self.amount:
            raise ValueError("earnest_money cannot exceed amount")
        return self


class OfferAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_version: int = Field(ge=1)
    offer_version: int = Field(ge=1)


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


async def _move_related_stages(
    conn: Any,
    transaction: Any,
    tenant_id: str,
    *,
    client_stage: Literal["under_contract", "closed"],
    lead_stage: Literal["under_contract", "closed"],
    listing_stage: Literal["pending", "sold"],
) -> None:
    """Move only tenant-owned records linked on the transaction itself."""
    values = dict(transaction)
    client_id = values.get("client_id")
    lead_id = values.get("lead_id")
    listing_id = values.get("listing_id")

    if client_id is not None:
        await conn.execute(
            """
            UPDATE clients SET stage=$1,updated_at=now()
             WHERE id=$2 AND tenant_id=$3::uuid
            """,
            client_stage,
            client_id,
            tenant_id,
        )
    if lead_id is not None:
        await conn.execute(
            """
            UPDATE leads SET dossier_status=$1,updated_at=now()
             WHERE id=$2 AND tenant_id=$3::uuid
            """,
            lead_stage,
            lead_id,
            tenant_id,
        )
    if listing_id is not None or lead_id is not None:
        await conn.execute(
            """
            UPDATE listings SET status=$1,updated_at=now()
             WHERE tenant_id=$2::uuid
               AND (($3::uuid IS NOT NULL AND id=$3::uuid)
                    OR ($4::uuid IS NOT NULL AND lead_id=$4::uuid))
               AND status IN ('draft','active','pending')
            """,
            listing_stage,
            tenant_id,
            listing_id,
            lead_id,
        )


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
            SELECT m.*, t.state_code, t.status AS transaction_status,
                   COALESCE((
                       SELECT array_agg(DISTINCT p.party_role ORDER BY p.party_role)
                       FROM transaction_parties p
                       WHERE p.transaction_id=m.transaction_id
                   ), ARRAY[]::text[]) AS party_roles
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


@router.get("/summary")
async def portfolio_summary(ctx: TenantContext = Depends(require_context)):
    """Dense Slot-2 summary derived only from the authenticated tenant's records."""
    try:
        async with tenant_tx(ctx) as conn:
            transaction_metrics = await conn.fetchrow(
                """
                SELECT
                    COALESCE(
                        sum(COALESCE(offer.amount,t.purchase_price,0))
                            FILTER (WHERE t.status IN ('active','under_contract')),
                        0
                    ) AS active_volume,
                    count(*) FILTER (
                        WHERE t.status='under_contract'
                    )::int AS transactions_under_contract
                  FROM transactions t
                  LEFT JOIN transaction_offers offer
                    ON offer.tenant_id=t.tenant_id
                   AND offer.id=t.accepted_offer_id
                 WHERE t.tenant_id=$1::uuid
                """,
                ctx.tenant_id,
            )
            communication = await conn.fetchrow(
                """
                WITH outbound AS (
                    SELECT count(DISTINCT COALESCE(
                               thread_id::text,client_id::text,lead_id::text
                           ))::int AS threads
                      FROM interaction_logs
                     WHERE tenant_id=$1::uuid
                       AND created_at >= now()-interval '30 days'
                       AND direction='outbound'
                ), inbound AS (
                    SELECT count(DISTINCT COALESCE(
                               thread_id::text,client_id::text,lead_id::text
                           ))::int AS threads
                      FROM interaction_logs
                     WHERE tenant_id=$1::uuid
                       AND created_at >= now()-interval '30 days'
                       AND direction='inbound'
                )
                SELECT CASE
                         WHEN outbound.threads=0 THEN 0
                         ELSE LEAST(
                             100,
                             round(
                                 inbound.threads::numeric
                                 / outbound.threads * 100,
                                 1
                             )
                         )
                       END AS response_rate
                  FROM outbound,inbound
                """,
                ctx.tenant_id,
            )
            ghosting = await conn.fetch(
                """
                SELECT c.id,c.full_name,c.client_type,c.stage,
                       c.last_contacted_at,
                       EXTRACT(
                           EPOCH FROM (now()-c.last_contacted_at)
                       )/3600 AS hours_silent
                  FROM clients c
                 WHERE c.tenant_id=$1::uuid
                   AND c.archived_at IS NULL
                   AND c.stage IN ('lead','active','nurture','under_contract')
                   AND c.last_contacted_at < now()-interval '72 hours'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM interaction_logs i
                        WHERE i.tenant_id=$1::uuid
                          AND i.client_id=c.id
                          AND i.direction='inbound'
                          AND i.created_at > c.last_contacted_at
                   )
                 ORDER BY c.last_contacted_at ASC
                 LIMIT 100
                """,
                ctx.tenant_id,
            )
            party_counts = await conn.fetch(
                """
                SELECT p.party_role,t.status,count(DISTINCT t.id)::int AS count
                  FROM transaction_parties p
                  JOIN transactions t
                    ON t.tenant_id=p.tenant_id
                   AND t.id=p.transaction_id
                 WHERE p.tenant_id=$1::uuid
                   AND p.party_role IN ('seller','buyer')
                 GROUP BY p.party_role,t.status
                """,
                ctx.tenant_id,
            )
            buyer_offer_pending = await conn.fetchval(
                """
                SELECT count(DISTINCT t.id)::int
                  FROM transactions t
                  JOIN transaction_parties p
                    ON p.tenant_id=t.tenant_id
                   AND p.transaction_id=t.id
                  JOIN transaction_offers offer
                    ON offer.tenant_id=t.tenant_id
                   AND offer.transaction_id=t.id
                 WHERE t.tenant_id=$1::uuid
                   AND p.party_role='buyer'
                   AND t.status='active'
                   AND offer.status='submitted'
                """,
                ctx.tenant_id,
            )
            title_risks = await conn.fetch(
                """
                SELECT property_key,finding_type,review_status
                  FROM title_findings
                 WHERE tenant_id=$1::uuid
                   AND review_status='required'
                 ORDER BY chain_gap DESC,created_at DESC
                 LIMIT 50
                """,
                ctx.tenant_id,
            )
            zoning_opportunities = await conn.fetch(
                """
                SELECT property_key,zoning_district,review_status
                  FROM zoning_analyses
                 WHERE tenant_id=$1::uuid
                   AND remaining_buildable_sqft > 0
                   AND review_status='required'
                 ORDER BY remaining_buildable_sqft DESC
                 LIMIT 50
                """,
                ctx.tenant_id,
            )
            distress_flags = await conn.fetch(
                """
                SELECT property_key,analysis_type,result,
                       professional_review_status
                  FROM intelligence_scores
                 WHERE tenant_id=$1::uuid
                   AND analysis_type IN ('distress','pre_distress')
                   AND professional_review_status='required'
                 ORDER BY observation_date DESC,created_at DESC
                 LIMIT 50
                """,
                ctx.tenant_id,
            )
            activity_rows = await conn.fetch(
                """
                SELECT event_id,category,action,target_id,metadata,created_at
                  FROM audit_ledger
                 WHERE tenant_id=$1::uuid
                 ORDER BY created_at DESC
                 LIMIT 20
                """,
                ctx.tenant_id,
            )
    except TimeoutError as exc:
        logger.warning(
            "Tenant-scoped portfolio summary timed out tenant=%s",
            ctx.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portfolio analytics are temporarily unavailable.",
        ) from exc

    counts = {
        ("seller", "active"): 0,
        ("seller", "under_contract"): 0,
        ("seller", "closed"): 0,
        ("buyer", "active"): 0,
        ("buyer", "under_contract"): 0,
        ("buyer", "closed"): 0,
    }
    for row in party_counts:
        key = (row["party_role"], row["status"])
        if key in counts:
            counts[key] = int(row["count"])

    response_percentage = float(
        communication["response_rate"] if communication else 0
    )
    title_flags = [
        {
            "type": "TITLE",
            "property_key": item["property_key"],
            "label": item["finding_type"],
            "review_status": item["review_status"],
        }
        for item in title_risks
    ]
    zoning_flags = [
        {
            "type": "ZONING",
            "property_key": item["property_key"],
            "label": item["zoning_district"],
            "review_status": item["review_status"],
        }
        for item in zoning_opportunities
    ]
    pre_distress_flags = [
        {
            "type": "PRE_DISTRESS",
            "property_key": row["property_key"],
            "label": (
                _mapping(row["result"]).get("summary")
                or _mapping(row["result"]).get("label")
                or "Pre-distress review required"
            ),
            "flags": _mapping(row["result"]),
            "review_status": row["professional_review_status"],
        }
        for row in distress_flags
    ]
    ghosting_clients = [
        {
            "client_id": item["id"],
            "name": item["full_name"],
            "last_contact_hours": int(float(item["hours_silent"] or 0)),
            "stage": str(item["stage"] or "").replace("_", " ").title(),
            "client_type": item["client_type"],
        }
        for item in ghosting
    ]
    active_contract_count = int(
        transaction_metrics["transactions_under_contract"] or 0
    )
    return {
        "tenant_id": ctx.tenant_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "active_contracts": active_contract_count,
            "total_volume": float(transaction_metrics["active_volume"] or 0),
            "response_rate_30d": response_percentage,
            "ghosting_alerts_count": len(ghosting_clients),
        },
        "ghosting_clients": ghosting_clients,
        "milestone_breakdown": {
            "sellers": {
                "prospecting": counts[("seller", "active")],
                "under_contract": counts[("seller", "under_contract")],
                "closed": counts[("seller", "closed")],
            },
            "buyers": {
                "matched": counts[("buyer", "active")],
                "offer_pending": int(buyer_offer_pending or 0),
                "under_contract": counts[("buyer", "under_contract")],
                "closed": counts[("buyer", "closed")],
            },
        },
        "intelligence_flags": title_flags + zoning_flags + pre_distress_flags,
        "activity_pulse": [_row(row) for row in activity_rows],
        "active_contract_records": [],
        "milestones": [],
    }


@router.get("/transactions")
async def list_transactions(
    transaction_status: Optional[str] = Query(
        default=None,
        alias="status",
        pattern=r"^(active|under_contract|closed|cancelled)$",
    ),
    property_source: Optional[str] = Query(
        default=None, pattern=r"^(pipeline|mls)$"
    ),
    client_id: Optional[UUID] = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: TenantContext = Depends(require_context),
):
    """List only the current tenant's transactions, including for admins."""
    async with tenant_tx(ctx) as conn:
        total = await conn.fetchval(
            """
            SELECT count(*)::int FROM transactions t
             WHERE t.tenant_id=$1::uuid
               AND ($2::text IS NULL OR t.status=$2)
               AND ($3::text IS NULL OR t.property_source=$3)
               AND ($4::uuid IS NULL OR t.client_id=$4::uuid)
            """,
            ctx.tenant_id,
            transaction_status,
            property_source,
            client_id,
        )
        rows = await conn.fetch(
            """
            SELECT t.*,c.full_name AS client_name,
                   ao.amount AS accepted_offer_amount
              FROM transactions t
              LEFT JOIN clients c
                ON c.tenant_id=t.tenant_id AND c.id=t.client_id
              LEFT JOIN transaction_offers ao
                ON ao.tenant_id=t.tenant_id AND ao.id=t.accepted_offer_id
             WHERE t.tenant_id=$1::uuid
               AND ($2::text IS NULL OR t.status=$2)
               AND ($3::text IS NULL OR t.property_source=$3)
               AND ($4::uuid IS NULL OR t.client_id=$4::uuid)
             ORDER BY t.updated_at DESC,t.id
             LIMIT $5 OFFSET $6
            """,
            ctx.tenant_id,
            transaction_status,
            property_source,
            client_id,
            limit,
            offset,
        )
    return {
        "transactions": [_row(row) for row in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.post("/transactions", status_code=status.HTTP_201_CREATED)
async def create_transaction(
    body: TransactionCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Create a deal from one explicitly selected pipeline or MLS property."""
    async with tenant_tx(ctx) as conn:
        anchor = await _property_anchor(
            conn, ctx, body.property_source, body.property_id
        )
        client = None
        if body.client_id is not None:
            client = await conn.fetchrow(
                """
                SELECT id,full_name FROM clients
                 WHERE id=$1 AND tenant_id=$2::uuid AND archived_at IS NULL
                """,
                body.client_id,
                ctx.tenant_id,
            )
            if not client:
                raise HTTPException(status_code=404, detail="Client not found.")

        transaction = await conn.fetchrow(
            """
            INSERT INTO transactions (
                tenant_id,state_code,property_type,lead_id,mls_listing_id,
                client_id,client_party_role,property_source,property_id,
                property_address,property_city,property_postal_code,
                source_provenance,purchase_price,earnest_money,financing_amount,
                offer_deadline,inspection_deadline,financing_deadline,
                closing_deadline,notes,created_by,updated_by
            ) VALUES (
                $1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,
                $14,$15,$16,$17,$18,$19,$20,$21,$22,$22
            )
            RETURNING *
            """,
            ctx.tenant_id,
            anchor["state_code"],
            anchor["property_type"],
            anchor["lead_id"],
            anchor["mls_listing_id"],
            body.client_id,
            body.party_role,
            body.property_source,
            body.property_id,
            anchor["address"],
            anchor["city"],
            anchor["postal_code"],
            json.dumps(anchor["source_provenance"]),
            body.purchase_price,
            body.earnest_money,
            body.financing_amount,
            body.offer_deadline,
            body.inspection_deadline,
            body.financing_deadline,
            body.closing_deadline,
            body.notes,
            ctx.agent_id,
        )

        party = None
        if client is not None:
            party = await conn.fetchrow(
                """
                INSERT INTO transaction_parties (
                    tenant_id,transaction_id,party_role,client_id,display_name
                ) VALUES ($1::uuid,$2,$3,$4,$5)
                RETURNING *
                """,
                ctx.tenant_id,
                transaction["id"],
                body.party_role,
                body.client_id,
                client["full_name"],
            )
    return {
        "transaction": _row(transaction),
        "party": _row(party) if party else None,
    }


@router.patch("/transactions/{transaction_id}")
async def patch_transaction(
    transaction_id: UUID,
    body: TransactionUpdate,
    ctx: TenantContext = Depends(require_context),
):
    updates = body.model_dump(exclude_unset=True)
    updates.pop("version", None)
    async with tenant_tx(ctx) as conn:
        current = await conn.fetchrow(
            """
            SELECT * FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not current:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if current["version"] != body.version:
            raise _version_conflict("transaction", current["version"])
        if current["status"] in {"closed", "cancelled"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "transaction_not_editable", "status": current["status"]},
            )

        merged = dict(current)
        merged.update(updates)
        error = _terms_error(merged)
        if error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
            )

        args: list[Any] = [transaction_id, ctx.tenant_id, body.version]
        assignments: list[str] = []
        for column in (
            "purchase_price",
            "earnest_money",
            "financing_amount",
            "offer_deadline",
            "inspection_deadline",
            "financing_deadline",
            "closing_deadline",
            "notes",
        ):
            if column in updates:
                args.append(updates[column])
                assignments.append(f"{column}=${len(args)}")
        args.append(ctx.agent_id)
        assignments.extend(
            [f"updated_by=${len(args)}", "version=version+1", "updated_at=now()"]
        )
        transaction = await conn.fetchrow(
            f"""
            UPDATE transactions SET {','.join(assignments)}
             WHERE id=$1 AND tenant_id=$2::uuid AND version=$3
            RETURNING *
            """,
            *args,
        )
        if not transaction:
            raise _version_conflict("transaction", current["version"])
    return {"transaction": _row(transaction)}


@router.post("/transactions/{transaction_id}/close")
async def close_transaction(
    transaction_id: UUID,
    body: TransactionClose,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        current = await conn.fetchrow(
            """
            SELECT * FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not current:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if current["version"] != body.version:
            raise _version_conflict("transaction", current["version"])
        if current["status"] != "under_contract":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "invalid_transaction_transition",
                    "from": current["status"],
                    "to": "closed",
                },
            )
        transaction = await conn.fetchrow(
            """
            UPDATE transactions
               SET status='closed',closed_at=now(),updated_by=$4,
                   version=version+1,updated_at=now()
             WHERE id=$1 AND tenant_id=$2::uuid AND version=$3
            RETURNING *
            """,
            transaction_id,
            ctx.tenant_id,
            body.version,
            ctx.agent_id,
        )
        if not transaction:
            raise _version_conflict("transaction", current["version"])
        await _move_related_stages(
            conn,
            transaction,
            ctx.tenant_id,
            client_stage="closed",
            lead_stage="closed",
            listing_stage="sold",
        )
        # The close is the one write that must commit. Both receipts below run
        # in their own SAVEPOINT and never raise, so a bookkeeping failure
        # cannot undo a closing. outcome_value is the raw purchase price —
        # expected_value applies the commission rate, not this table.
        await outcome_memory.record_outcome(
            ctx,
            outcome_kind="transaction_closed",
            subject_type="transaction",
            subject_id=str(transaction["id"]),
            client_id=str(transaction["client_id"]) if transaction["client_id"] else None,
            source_table="transactions",
            source_id=str(transaction["id"]),
            occurred_at=transaction["closed_at"],
            outcome_value=(
                float(transaction["purchase_price"])
                if transaction["purchase_price"] is not None else None
            ),
            conn=conn,
        )
        # Allowed by 0067's CHECK and never once emitted before this line.
        await record_usage(
            ctx,
            metric="transaction_closed",
            quantity=1,
            idempotency_key=f"transaction_closed:{transaction['id']}",
            occurred_at=transaction["closed_at"],
            conn=conn,
        )
    return {"transaction": _row(transaction)}


@router.post("/transactions/{transaction_id}/lose")
async def lose_transaction(
    transaction_id: UUID,
    body: TransactionLose,
    ctx: TenantContext = Depends(require_context),
):
    """A deal that died is a fact, not a cancelled row.

    Same optimistic-version discipline as close. Only a live deal can be lost
    — a closed one is not, and a cancelled one was never a deal.

    Deliberately does NOT cascade stages. _move_related_stages is typed for
    under_contract|closed only; deciding that a lost deal moves clients.stage
    to 'lost' and leads.dossier_status to 'dead' is a separate call about
    what those columns mean, and guessing it here would rewrite two other
    tables' semantics as a side effect of recording one fact.
    """
    async with tenant_tx(ctx) as conn:
        current = await conn.fetchrow(
            """
            SELECT * FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not current:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if current["version"] != body.version:
            raise _version_conflict("transaction", current["version"])
        if not lose_transition_allowed(current["status"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "invalid_transaction_transition",
                    "from": current["status"],
                    "to": "lost",
                },
            )
        transaction = await conn.fetchrow(
            """
            UPDATE transactions
               SET status='lost', lost_at=now(),
                   lost_reason_code=$5, lost_reason=$6,
                   updated_by=$4, version=version+1, updated_at=now()
             WHERE id=$1 AND tenant_id=$2::uuid AND version=$3
            RETURNING *
            """,
            transaction_id,
            ctx.tenant_id,
            body.version,
            ctx.agent_id,
            body.reason_code,
            body.reason,
        )
        if not transaction:
            raise _version_conflict("transaction", current["version"])
        await outcome_memory.record_outcome(
            ctx,
            outcome_kind="transaction_lost",
            subject_type="transaction",
            subject_id=str(transaction["id"]),
            client_id=str(transaction["client_id"]) if transaction["client_id"] else None,
            source_table="transactions",
            source_id=str(transaction["id"]),
            occurred_at=transaction["lost_at"],
            outcome_value=(
                float(transaction["purchase_price"])
                if transaction["purchase_price"] is not None else None
            ),
            detail={"reason_code": body.reason_code},
            conn=conn,
        )
    return {"transaction": _row(transaction)}


def lose_transition_allowed(current_status: Optional[str]) -> bool:
    """Whether a deal in this state can be recorded as lost."""
    return current_status in LOSABLE_STATES


@router.get("/transactions/{transaction_id}/offers")
async def list_offers(
    transaction_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        exists = await conn.fetchval(
            """
            SELECT 1 FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        rows = await conn.fetch(
            """
            SELECT * FROM transaction_offers
             WHERE transaction_id=$1 AND tenant_id=$2::uuid
             ORDER BY created_at DESC,id
            """,
            transaction_id,
            ctx.tenant_id,
        )
    return {"offers": [_row(row) for row in rows], "total": len(rows)}


@router.post(
    "/transactions/{transaction_id}/offers",
    status_code=status.HTTP_201_CREATED,
)
async def create_offer(
    transaction_id: UUID,
    body: OfferCreate,
    ctx: TenantContext = Depends(require_context),
):
    expires_at = body.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )

    async with tenant_tx(ctx) as conn:
        transaction = await conn.fetchrow(
            """
            SELECT id,status FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not transaction:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if transaction["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "transaction_not_open", "status": transaction["status"]},
            )
        offer = await conn.fetchrow(
            """
            INSERT INTO transaction_offers (
                tenant_id,transaction_id,status,amount,earnest_money,
                financing_type,proposed_closing_date,expires_at,
                contingencies,notes,created_by,updated_by,submitted_at
            ) VALUES (
                $1::uuid,$2,'submitted',$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$10,now()
            )
            RETURNING *
            """,
            ctx.tenant_id,
            transaction_id,
            body.amount,
            body.earnest_money,
            body.financing_type,
            body.proposed_closing_date,
            expires_at,
            json.dumps(body.contingencies),
            body.notes,
            ctx.agent_id,
        )
    return {"offer": _row(offer)}


@router.post("/transactions/{transaction_id}/offers/{offer_id}/accept")
async def accept_offer(
    transaction_id: UUID,
    offer_id: UUID,
    body: OfferAccept,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        current_transaction = await conn.fetchrow(
            """
            SELECT * FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not current_transaction:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if current_transaction["version"] != body.transaction_version:
            raise _version_conflict(
                "transaction", current_transaction["version"]
            )
        if current_transaction["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "invalid_transaction_transition",
                    "from": current_transaction["status"],
                    "to": "under_contract",
                },
            )

        current_offer = await conn.fetchrow(
            """
            SELECT * FROM transaction_offers
             WHERE id=$1 AND transaction_id=$2 AND tenant_id=$3::uuid
             FOR UPDATE
            """,
            offer_id,
            transaction_id,
            ctx.tenant_id,
        )
        if not current_offer:
            raise HTTPException(status_code=404, detail="Offer not found.")
        if current_offer["version"] != body.offer_version:
            raise _version_conflict("offer", current_offer["version"])
        if current_offer["status"] != "submitted":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "invalid_offer_transition",
                    "from": current_offer["status"],
                    "to": "accepted",
                },
            )
        expires_at = current_offer.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "offer_expired"},
                )

        await conn.execute(
            """
            UPDATE transaction_offers
               SET status='rejected',rejected_at=now(),updated_by=$3,
                   version=version+1,updated_at=now()
             WHERE transaction_id=$1 AND tenant_id=$2::uuid
               AND id<>$4 AND status='submitted'
            """,
            transaction_id,
            ctx.tenant_id,
            ctx.agent_id,
            offer_id,
        )
        offer = await conn.fetchrow(
            """
            UPDATE transaction_offers
               SET status='accepted',accepted_at=now(),updated_by=$5,
                   version=version+1,updated_at=now()
             WHERE id=$1 AND transaction_id=$2 AND tenant_id=$3::uuid
               AND version=$4
            RETURNING *
            """,
            offer_id,
            transaction_id,
            ctx.tenant_id,
            body.offer_version,
            ctx.agent_id,
        )
        if not offer:
            raise _version_conflict("offer", current_offer["version"])

        transaction = await conn.fetchrow(
            """
            UPDATE transactions
               SET status='under_contract',accepted_offer_id=$3,
                   purchase_price=$4,earnest_money=$5,
                   closing_deadline=COALESCE($6,closing_deadline),
                   updated_by=$7,version=version+1,updated_at=now()
             WHERE id=$1 AND tenant_id=$2::uuid AND version=$8
            RETURNING *
            """,
            transaction_id,
            ctx.tenant_id,
            offer_id,
            current_offer["amount"],
            current_offer["earnest_money"],
            current_offer.get("proposed_closing_date"),
            ctx.agent_id,
            body.transaction_version,
        )
        if not transaction:
            raise _version_conflict(
                "transaction", current_transaction["version"]
            )
        await _move_related_stages(
            conn,
            transaction,
            ctx.tenant_id,
            client_stage="under_contract",
            lead_stage="under_contract",
            listing_stage="pending",
        )
    return {"transaction": _row(transaction), "offer": _row(offer)}


@router.get("/transactions/{transaction_id}")
async def transaction_detail(
    transaction_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    async with tenant_tx(ctx) as conn:
        transaction = await conn.fetchrow(
            "SELECT * FROM transactions WHERE id=$1 AND tenant_id=$2::uuid",
            transaction_id,
            ctx.tenant_id,
        )
        if not transaction:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        parties = await conn.fetch(
            """
            SELECT * FROM transaction_parties
             WHERE transaction_id=$1 AND tenant_id=$2::uuid
             ORDER BY created_at
            """,
            transaction_id,
            ctx.tenant_id,
        )
        milestones = await conn.fetch(
            """
            SELECT * FROM transaction_milestones
             WHERE transaction_id=$1 AND tenant_id=$2::uuid
             ORDER BY due_at NULLS LAST, created_at
            """,
            transaction_id,
            ctx.tenant_id,
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
        exists = await conn.fetchval(
            """
            SELECT 1 FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
            """,
            transaction_id,
            ctx.tenant_id,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        if body.client_id is not None:
            client_exists = await conn.fetchval(
                """
                SELECT 1 FROM clients
                 WHERE id=$1 AND tenant_id=$2::uuid AND archived_at IS NULL
                """,
                body.client_id,
                ctx.tenant_id,
            )
            if not client_exists:
                raise HTTPException(status_code=404, detail="Client not found.")
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
        exists = await conn.fetchval(
            """
            SELECT 1 FROM transactions
             WHERE id=$1 AND tenant_id=$2::uuid
            """,
            transaction_id,
            ctx.tenant_id,
        )
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
               SET status=$3, due_at=$4, assigned_to=$5, metadata=$6::jsonb,
                   completed_at=$7, updated_at=now()
             WHERE id=$1 AND tenant_id=$2::uuid
            RETURNING *
            """,
            milestone_id,
            ctx.tenant_id,
            body.status,
            body.due_at,
            body.assigned_to,
            json.dumps(body.metadata),
            completed_at,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    return _row(row)
