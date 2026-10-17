"""
property_match_api.py — a broker owner decides whether a public property
record is a linked client's house (prompt 11, T11.03, F11.04).

Route:
  POST /api/crm/houses/{lead_id}/record-match — confirm, reject or withdraw
       that public record R is the house this tenant's lead (linked to a
       client) refers to. One append-only row in property_match_decisions
       (0168) per decision; a reversal is a new row naming the head it
       supersedes.

The Houses search names what it matched (an exact key, or a possible match)
and never more; that claim is not changed here. This records a separate fact
about the house, which mls_portal reads as `identity.verified` only while the
newest decision on (lead, this record) is a confirmation for the client the
house is still linked to (`confirmation_state`).
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

import sealed_text
from audit_ledger import AuditCategory, ledger
from db.connection import tenant_tx
from platform_policy import validate_approval_reason
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/crm", tags=["Agent CRM"])

# Mirrors the CHECK in 0168, for a clean 422 before any SQL.
Evidence = Literal["parcel_number_checked", "deed_or_title", "tax_bill",
                   "client_confirmed", "site_visit", "other"]

_STALE = ("This record was decided by someone else since you opened it. "
          "Reload the house and decide again.")


class RecordMatchDecision(BaseModel):
    """A person's decision on one (house, public record) pair. `reason` is
    checked by validate_approval_reason (8-500 characters once whitespace is
    collapsed). `supersedes_id` is the head decision the person saw (None when
    they saw none). The client is never taken from the request."""

    model_config = ConfigDict(extra="forbid")
    public_record_id: UUID
    decision: Literal["confirmed", "rejected", "withdrawn"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=6)
    reason: str = Field(max_length=2_000)
    supersedes_id: Optional[UUID] = None

    @field_validator("evidence")
    @classmethod
    def _each_kind_once(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("each evidence kind may be named once")
        return value


def can_decide(ctx: TenantContext) -> bool:
    """Whether Houses offers the decision form: to a broker owner. A display
    hint only; the route is the gate (require_role below), and it also admits
    a platform admin. Houses does not offer them the form: their RLS spans
    every tenant, so the lead on a card is the newest one for that parcel in
    any brokerage (mls_portal._lead_ids_for_records), not one the operator
    chose. A platform admin decides by an explicit lead id."""
    return ctx.is_broker_owner


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def confirmation_state(head: Optional[dict]) -> dict[str, Any]:
    """The newest decision on one (lead, record) pair, as Houses shows it.

    `head` carries the decision's id, `decision`, `decided_at` and
    `same_client` (its client_id still equals the lead's seller_client_id).
    Only a confirmation for the current client is `confirmed`; a confirmation
    or rejection for a client the house is no longer linked to is
    `out_of_date`; a withdrawal, or no decision, is `none` (a withdrawal keeps
    its id: the next decision supersedes it)."""
    if not head:
        return {"state": "none", "head_id": None, "decided_at": None}
    decision = head.get("decision")
    if decision == "withdrawn":
        state = "none"
    elif decision in ("confirmed", "rejected") and head.get("same_client") is True:
        state = decision
    else:
        state = "out_of_date"
    return {"state": state, "head_id": str(head["id"]), "decided_at": _iso(head.get("decided_at"))}


@router.post("/houses/{lead_id}/record-match", status_code=status.HTTP_201_CREATED)
async def decide_record_match(
    lead_id: UUID,
    body: RecordMatchDecision,
    ctx: TenantContext = Depends(require_context),
):
    """Confirm, reject or withdraw that a public record is this house (T11.03).

    A broker owner decides, as for title findings (L11-13): the link between a
    client and a public record is the brokerage's record. 0168's insert policy
    is the database floor whatever this gate becomes: the lead's client must
    be editable by the caller.

    The record must be on the house's own parcel (the exact parcel key Houses
    joins on), and the house must be linked to a client: the decision is
    about that client's house, and the client is read from the lead under FOR
    UPDATE, never from the request. A decision made against a head that is no
    longer the newest is a 409, so nobody overwrites a decision they never
    saw.

    The reason often names an owner, so it is stored only sealed (0168,
    CONTRACT C5.1), under the key of the lead's own tenant: a platform admin
    may decide another tenant's house, and that tenant's audit trail records
    it. The audit record names the decision's own ids and the decision only.
    """
    require_role(ctx, Role.BROKER_OWNER)
    reason = validate_approval_reason(body.reason)
    if body.decision == "confirmed" and not body.evidence:
        raise HTTPException(
            status_code=422,
            detail="Name at least one piece of evidence to confirm this house's record.",
        )
    record_id = str(body.public_record_id)
    async with tenant_tx(ctx) as conn:
        # Read and lock the house first: RLS decides whether the caller sees
        # it, its client is the one decided on, and its tenant names the key.
        lead = await conn.fetchrow(
            "SELECT tenant_id,parcel_id,state,seller_client_id FROM leads WHERE id=$1::uuid FOR UPDATE",
            str(lead_id),
        )
        if lead is None:
            raise HTTPException(status_code=404, detail="House not found.")
        if lead["seller_client_id"] is None:
            raise HTTPException(
                status_code=409,
                detail="Link this house to a client before deciding its parcel.",
            )
        record = await conn.fetchrow(
            "SELECT id,parcel_id,state,record_refreshed_at FROM public_property_records WHERE id=$1::uuid",
            record_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Public property record not found.")
        if (record["parcel_id"], record["state"]) != (lead["parcel_id"], lead["state"]):
            raise HTTPException(
                status_code=422,
                detail="Only a record on this house's parcel can be decided.",
            )
        head = await conn.fetchrow(
            """
            SELECT h.id,h.decision,h.client_id
              FROM property_match_decisions h
             WHERE h.lead_id=$1::uuid AND h.public_record_id=$2::uuid
               AND NOT EXISTS (SELECT 1 FROM property_match_decisions n WHERE n.supersedes_id=h.id)
            """,
            str(lead_id),
            record_id,
        )
        head_id = str(head["id"]) if head else None
        if head_id != (str(body.supersedes_id) if body.supersedes_id else None):
            raise HTTPException(status_code=409, detail=_STALE)
        if body.decision == "withdrawn" and (head is None or head["decision"] == "withdrawn"):
            raise HTTPException(status_code=409, detail="There is no decision on this record to withdraw.")
        if (head is not None and head["decision"] == body.decision
                and head["client_id"] == lead["seller_client_id"]):
            raise HTTPException(
                status_code=409,
                detail=f"This record is already {body.decision} for this house.",
            )
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO property_match_decisions
                    (tenant_id,lead_id,public_record_id,client_id,decision,supersedes_id,
                     evidence,record_refreshed_at,reason_ciphertext)
                VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6::uuid,$7::text[],$8,
                        pgp_sym_encrypt($9::text,$10::text,'s2k-mode=1'))
                RETURNING id,decided_at
                """,
                lead["tenant_id"],
                str(lead_id),
                record_id,
                lead["seller_client_id"],
                body.decision,
                head_id,
                list(body.evidence),
                record["record_refreshed_at"],
                reason,
                sealed_text.key_for(lead["tenant_id"]),
            )
        except asyncpg.UniqueViolationError:
            # Another decision on the same head (or a first one) landed between
            # the read above and this write: uq_pmd_successor / uq_pmd_first.
            raise HTTPException(status_code=409, detail=_STALE) from None
    decision_id = str(row["id"])
    # Every agent of the tenant may read its ledger (0005's
    # audit_ledger_select), while 0168 hides this decision from an agent who
    # cannot see the house. So the entry names the decision only: no lead,
    # client or public record (whose id resolves to a street address in the
    # global catalog). The row keeps those under RLS. The HTTP audit
    # middleware's own row logs the request path, which holds the lead id;
    # the portfolio pulse, the one page every agent reads it through, returns
    # neither metadata nor target ids and masks ids in paths (L11-15).
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action=f"property_match_{body.decision}",
        tenant_id=str(lead["tenant_id"]),
        user_id=ctx.agent_id,
        target_id=decision_id,
        metadata={
            "decision_id": decision_id,
            "decision": body.decision,
            "supersedes_id": head_id,
        },
    )
    confirmation = confirmation_state({
        "id": decision_id,
        "decision": body.decision,
        "same_client": True,
        "decided_at": row["decided_at"],
    })
    # The lead-derived part of Houses' identity only: the search's status and
    # basis are the caller's to keep, and are never changed by a decision.
    return {
        "decision_id": decision_id,
        "decision": body.decision,
        "decided_at": _iso(row["decided_at"]),
        "identity": {
            "verified": confirmation["state"] == "confirmed",
            "confirmation": confirmation,
            "confirmable": can_decide(ctx),
        },
    }
