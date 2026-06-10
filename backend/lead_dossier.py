"""
Lead Dossier — Kanban-style state transitions for the deal pipeline.

PATCH /api/leads/{lead_id}/dossier-status moves a lead through the 0007 state
machine. The one load-bearing side effect: the first transition into
'under_contract' stamps contract_execution_date, which fires the 0007 sync
trigger (materializing contract_expires_at) and puts the lead on the
disposition enforcer's radar. Dragging a card on the PipelineBoard is what
starts the assignment clock.

RLS does the tenant scoping — the UPDATE physically cannot touch another
brokerage's lead. Every successful move is broadcast to the tenant's
dashboards (DOSSIER_MOVED) so other open sessions re-sync and LivePulse logs
the action.
"""

import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context
import ws_hub

logger = logging.getLogger("oracle.lead_dossier")

router = APIRouter(prefix="/api/leads", tags=["Lead Dossier"])

# Mirror of chk_dossier_status in 0007 — validated here for a clean 422
# instead of a constraint-violation 500.
DOSSIER_STATUSES = {
    "draft", "under_contract", "marketing", "assigned", "closed", "expired", "dead",
}


class DossierMove(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in DOSSIER_STATUSES:
            raise ValueError(f"must be one of {sorted(DOSSIER_STATUSES)}")
        return v


def _jsonb(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


@router.get("/{lead_id}/dossier")
async def get_dossier(
    lead_id: str,
    ctx: TenantContext = Depends(require_context),
):
    """The full intelligence file for one lead — fetched on demand when the
    DossierPanel opens. Deliberately NOT in the DEAL_PIPELINE frame: marketing
    email bodies for 500 leads would bloat every pipeline push."""
    try:
        uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lead_id must be a UUID")

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                SELECT parcel_id, state, motivation_score, underwriting, payload,
                       dossier_status, contract_execution_date, contract_expires_at,
                       marketing_payload, marketing_generated_at, acquisition_entity
                  FROM leads WHERE id = $1
                """,
                lead_id,
            )
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")
            logs = await conn.fetch(
                """
                SELECT actor_role, interaction_type, payload, created_at
                  FROM interaction_logs
                 WHERE lead_id = $1
                 ORDER BY created_at DESC
                 LIMIT 10
                """,
                lead_id,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline ({exc})",
        )

    return {
        "parcel_id": row["parcel_id"],
        "state": row["state"],
        "motivation_score": row["motivation_score"],
        "underwriting": _jsonb(row["underwriting"]),
        "payload": _jsonb(row["payload"]),
        "dossier_status": row["dossier_status"],
        "contract_execution_date": row["contract_execution_date"].isoformat()
            if row["contract_execution_date"] else None,
        "contract_expires_at": row["contract_expires_at"].isoformat()
            if row["contract_expires_at"] else None,
        "marketing_payload": _jsonb(row["marketing_payload"]) or None,
        "marketing_generated_at": row["marketing_generated_at"].isoformat()
            if row["marketing_generated_at"] else None,
        "acquisition_entity": _jsonb(row["acquisition_entity"]) or None,
        "interactions": [
            {
                "actor_role": l["actor_role"],
                "interaction_type": l["interaction_type"],
                "payload": _jsonb(l["payload"]),
                "created_at": l["created_at"].isoformat(),
            }
            for l in logs
        ],
    }


_EIN_RE = re.compile(r"^\d{2}-?\d{7}$")


class AcquisitionEntity(BaseModel):
    """The entity taking title. Formed externally (attorney / registered
    agent / Stripe Atlas dashboard — Atlas has no formation API); recorded
    here so the legal agent can name the correct purchaser in the P&S."""
    entity_name: str = Field(..., min_length=2, max_length=160)
    ein: str = Field("", max_length=10)
    formation_state: str = Field("", max_length=2)
    notes: str = Field("", max_length=500)

    @field_validator("ein")
    @classmethod
    def _check_ein(cls, v: str) -> str:
        v = v.strip()
        if v and not _EIN_RE.match(v):
            raise ValueError("EIN must be 9 digits (XX-XXXXXXX)")
        return v

    @field_validator("formation_state")
    @classmethod
    def _check_state(cls, v: str) -> str:
        v = v.strip().upper()
        if v and not re.match(r"^[A-Z]{2}$", v):
            raise ValueError("formation_state must be a 2-letter code")
        return v


@router.put("/{lead_id}/entity")
async def set_acquisition_entity(
    lead_id: str,
    body: AcquisitionEntity,
    ctx: TenantContext = Depends(require_context),
):
    try:
        uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lead_id must be a UUID")

    entity = {
        "entity_name": body.entity_name.strip(),
        "ein": body.ein,
        "formation_state": body.formation_state,
        "notes": body.notes.strip(),
    }
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                UPDATE leads
                   SET acquisition_entity =
                       $1::jsonb || jsonb_build_object('recorded_at', now()::text)
                 WHERE id = $2
             RETURNING acquisition_entity
                """,
                json.dumps(entity),
                lead_id,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — entity not persisted ({exc})",
        )

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")

    logger.info(
        "Acquisition entity recorded: lead=%s entity=%r (tenant=%s)",
        lead_id, entity["entity_name"], ctx.tenant_id,
    )
    return {"status": "recorded", "acquisition_entity": _jsonb(row["acquisition_entity"])}


@router.patch("/{lead_id}/dossier-status")
async def move_dossier(
    lead_id: str,
    body: DossierMove,
    ctx: TenantContext = Depends(require_context),
):
    try:
        uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lead_id must be a UUID")

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                UPDATE leads
                   SET dossier_status = $1,
                       contract_execution_date = CASE
                           WHEN $1 = 'under_contract' AND contract_execution_date IS NULL
                           THEN now()
                           ELSE contract_execution_date
                       END
                 WHERE id = $2
             RETURNING parcel_id, payload, dossier_status, contract_expires_at
                """,
                body.status,
                lead_id,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — move not persisted ({exc})",
        )

    if row is None:
        # Unknown id OR another tenant's lead — RLS makes them indistinguishable,
        # which is exactly the point.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")

    prop = row["payload"]
    if isinstance(prop, str):
        try:
            prop = json.loads(prop)
        except json.JSONDecodeError:
            prop = {}
    address = (prop or {}).get("address") or row["parcel_id"]

    await ws_hub.broadcast(ctx.tenant_id, {
        "type": "DOSSIER_MOVED",
        "lead_id": lead_id,
        "parcel_id": row["parcel_id"],
        "address": address,
        "status": row["dossier_status"],
        "contract_expires_at": row["contract_expires_at"].isoformat()
            if row["contract_expires_at"] else None,
    })

    logger.info(
        "Dossier move: lead=%s → %s (tenant=%s, agent=%s)",
        lead_id, body.status, ctx.tenant_id, ctx.agent_id,
    )
    return {
        "status": "moved",
        "dossier_status": row["dossier_status"],
        "contract_expires_at": row["contract_expires_at"].isoformat()
            if row["contract_expires_at"] else None,
    }
