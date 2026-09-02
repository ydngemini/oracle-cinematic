"""Secure passwordless client portal links (Neoh client-management layer).

Agent-facing: mint and revoke single-asset portal links for a lead.
Public-facing: a homeowner clicks the link; we resolve the token digest and
return a strictly-constrained portal session JWT (role=portal_client, pinned
to one tenant + one lead).

SECURITY:
  * The plaintext bearer token exists exactly once — in the response to the
    issuing agent. Only its SHA-256 digest is stored or queried, so neither a
    DB snapshot nor the pgaudit statement stream can replay a link.
  * Public resolution goes through resolve_portal_token() (SECURITY DEFINER,
    0008) — the only cross-tenant read path, keyed by exact digest.
  * Invalid, expired, and revoked tokens are indistinguishable to the caller
    (uniform 404) — no oracle for probing link state.
"""

import hashlib
import logging
import os
import secrets
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth import ALGORITHM, SECRET_KEY
from db.connection import get_pool, tenant_tx
from tenancy import Role, TenantContext, require_context

log = logging.getLogger("oracle.client_portal")

router = APIRouter(prefix="/portal", tags=["client-portal"])

import config as _config

PORTAL_BASE_URL = _config.public_base_url()
MAX_EXPIRY_DAYS = 90


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PortalAssetScope(BaseModel):
    """Closed allow-list: a portal can never request arbitrary table/field names."""

    model_config = ConfigDict(extra="forbid")
    summary: bool = True
    media: bool = False
    milestones: bool = False
    title_summary: bool = False
    zoning_summary: bool = False
    underwriting: bool = False
    documents: bool = False


class PortalLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    lead_id: UUID
    expiry_days: int = Field(default=7, ge=1, le=MAX_EXPIRY_DAYS)
    link_kind: Literal["seller", "joint_venture"] = "seller"
    asset_scope: PortalAssetScope = Field(default_factory=PortalAssetScope)
    issued_to_label: Optional[str] = Field(default=None, max_length=120)
    watermark_text: Optional[str] = Field(default=None, max_length=160)

    @field_validator("issued_to_label", "watermark_text")
    @classmethod
    def plain_single_line(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and any(ord(ch) < 32 for ch in value):
            raise ValueError("portal labels must be a single line")
        return value


class PortalLinkResponse(BaseModel):
    portal_id: str
    secure_url: str
    access_expires_at: str
    link_kind: str
    asset_scope: dict
    watermark_text: str


class PortalSessionResponse(BaseModel):
    authenticated: bool
    session_token: str
    lead_id: str
    expires_at: str
    link_kind: str
    asset_scope: dict
    watermark_text: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Agent endpoints — authenticated, RLS-scoped via tenant_tx
# ---------------------------------------------------------------------------

@router.post("/links", response_model=PortalLinkResponse)
async def create_portal_link(
    body: PortalLinkRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Mint a passwordless portal link for a lead owned by this tenant."""
    token = secrets.token_urlsafe(32)  # 256-bit; plaintext returned once below
    token_hash = _hash_token(token)
    scope = body.asset_scope.model_dump()
    # Seller dossiers never expose disposition documents by default; a broker
    # must explicitly opt in.  Every scope remains read-only.
    watermark = body.watermark_text or (
        f"CONFIDENTIAL — {body.issued_to_label}"
        if body.issued_to_label
        else "CONFIDENTIAL — REVOCABLE DOSSIER"
    )

    async with tenant_tx(ctx) as conn:
        # RLS scopes this lookup — a foreign lead_id reads as nonexistent.
        lead = await conn.fetchrow("SELECT id FROM leads WHERE id = $1", body.lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found.")

        row = await conn.fetchrow(
            """
            INSERT INTO client_portals
                (tenant_id, lead_id, token_hash, access_expires_at, created_by,
                 link_kind,asset_scope,watermark_text,issued_to_label)
            VALUES
                ($1, $2, $3, now() + make_interval(days => $4), $5,
                 $6,$7::jsonb,$8,$9)
            RETURNING id, access_expires_at
            """,
            UUID(ctx.tenant_id), body.lead_id, token_hash,
            body.expiry_days, ctx.agent_id, body.link_kind,
            json.dumps(scope, separators=(",", ":")), watermark,
            body.issued_to_label,
        )

    log.info(
        "Portal link minted: portal_id=%s lead_id=%s tenant_id=%s by agent=%s",
        row["id"], body.lead_id, ctx.tenant_id, ctx.agent_id,
    )
    return PortalLinkResponse(
        portal_id=str(row["id"]),
        secure_url=f"{PORTAL_BASE_URL}/vault/secure-access/{token}",
        access_expires_at=row["access_expires_at"].isoformat(),
        link_kind=body.link_kind,
        asset_scope=scope,
        watermark_text=watermark,
    )


@router.get("/links")
async def list_portal_links(
    lead_id: Optional[UUID] = None,
    ctx: TenantContext = Depends(require_context),
):
    """List link metadata only; bearer tokens can never be recovered."""
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,lead_id,link_kind,asset_scope,watermark_text,issued_to_label,
                   access_expires_at,revoked_at,last_accessed_at,access_count,
                   created_by,created_at
              FROM client_portals
             WHERE ($1::uuid IS NULL OR lead_id=$1)
             ORDER BY created_at DESC
            """,
            lead_id,
        )
    return {
        "links": [
            {
                **dict(row),
                "id": str(row["id"]),
                "lead_id": str(row["lead_id"]),
                "asset_scope": (
                    json.loads(row["asset_scope"])
                    if isinstance(row["asset_scope"], str)
                    else row["asset_scope"]
                ),
                "access_expires_at": row["access_expires_at"].isoformat(),
                "active": (
                    row["revoked_at"] is None
                    and row["access_expires_at"] > datetime.now(timezone.utc)
                ),
                "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
                "last_accessed_at": (
                    row["last_accessed_at"].isoformat() if row["last_accessed_at"] else None
                ),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/links/{portal_id}/revoke")
async def revoke_portal_link(
    portal_id: UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Kill a link immediately. RLS confines the UPDATE to this tenant."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE client_portals
               SET revoked_at = now()
             WHERE id = $1 AND revoked_at IS NULL
            RETURNING id
            """,
            portal_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Portal link not found.")
    log.info("Portal link revoked: portal_id=%s tenant_id=%s", portal_id, ctx.tenant_id)
    return {"revoked": True, "portal_id": str(portal_id)}


# ---------------------------------------------------------------------------
# Public endpoint — unauthenticated landing gate
# ---------------------------------------------------------------------------

@router.get("/session/{token}", response_model=PortalSessionResponse)
async def open_portal_session(token: str):
    """Resolve a portal link and mint a single-asset client session JWT.

    The JWT is pinned to (tenant, lead, portal) with role=portal_client and
    expires no later than the link itself.
    """
    # token_urlsafe(32) yields 43 chars; reject obviously malformed input
    # before touching the database.
    if not (40 <= len(token) <= 64):
        raise HTTPException(status_code=404, detail="Link invalid or expired.")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM resolve_portal_token($1)", _hash_token(token)
        )

    if row is None:
        # Uniform response for unknown / expired / revoked — no state oracle.
        raise HTTPException(status_code=404, detail="Link invalid or expired.")

    expires_at: datetime = row["access_expires_at"]
    scope = (
        json.loads(row["asset_scope"])
        if isinstance(row["asset_scope"], str)
        else dict(row["asset_scope"] or {})
    )
    claims = {
        "role": "portal_client",
        "tenant_id": str(row["tenant_id"]),
        "lead_id": str(row["lead_id"]),
        "portal_id": str(row["portal_id"]),
        "link_kind": row["link_kind"],
        "asset_scope": scope,
        "watermark_text": row["watermark_text"] or "CONFIDENTIAL — REVOCABLE DOSSIER",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    session_token = jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)

    return PortalSessionResponse(
        authenticated=True,
        session_token=session_token,
        lead_id=str(row["lead_id"]),
        expires_at=expires_at.isoformat(),
        link_kind=row["link_kind"],
        asset_scope=scope,
        watermark_text=row["watermark_text"] or "CONFIDENTIAL — REVOCABLE DOSSIER",
    )


def _portal_session_claims(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing portal session.")
    try:
        claims = jwt.decode(
            authorization.removeprefix("Bearer ").strip(),
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired portal session.") from exc
    if claims.get("role") != "portal_client":
        raise HTTPException(status_code=403, detail="Not a portal session.")
    try:
        UUID(str(claims["tenant_id"]))
        UUID(str(claims["lead_id"]))
        UUID(str(claims["portal_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Malformed portal session.") from exc
    return claims


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# Perception — what the homeowner actually did in here
# ---------------------------------------------------------------------------
#
# Until now this portal recorded nothing. A homeowner could open their dossier,
# read the title findings and download a contract, and the CRM would show the
# same silence as someone who never clicked the link. That silence is the
# single largest hole in the intent model: `interaction_logs` gained the
# behavioural types in 0095, but nothing produced them.
#
# THREE RULES GOVERN EVERYTHING BELOW.
#
# 1. Identity comes from the JWT, never the body. tenant, lead and portal are
#    read from the signed session. A portal client who could name their own
#    tenant_id would be able to write rows into someone else's CRM, and the
#    only reason that is impossible here is that the body has no say.
#
# 2. Repeat views are collapsed. A dossier page that reconnects, a phone waking
#    from sleep, or an impatient refresh must not read as engagement. Without
#    the cooldown a single left-open tab manufactures hundreds of rows and the
#    observed intent score — which weights repeats — climbs on its own. That
#    would be worse than no capture, because it would be confidently wrong.
#
# 3. actor_role is 'seller'. These rows are the homeowner's own actions on the
#    brokerage's own surface. intent_states reads only buyer/seller rows for
#    exactly this reason: agent activity must never be counted as client intent.

#: How long before another open of the same portal counts as a new visit.
#: Fifteen minutes is long enough to swallow refreshes and reconnects, short
#: enough that coming back after lunch registers as coming back.
PORTAL_VIEW_COOLDOWN = timedelta(minutes=15)

#: Ceiling on how much one portal link may write per hour. The endpoint is
#: reachable by anyone holding a live link, and per-asset events have no
#: cooldown, so without a cap a single holder can insert unbounded rows into the
#: brokerage's CRM. Set well above what a person reading a dossier produces and
#: well below what a script does; over the cap, events are dropped silently
#: rather than erroring, because the page must not be able to tell the
#: difference and a homeowner must never see a failure for reading their own
#: property record.
PORTAL_ACTIVITY_HOURLY_CAP = 120

#: What a portal page may report about itself. Deliberately tiny: these are the
#: things a homeowner can actually do in a read-only dossier. Anything not in
#: this set is rejected rather than stored, because an open-ended event sink
#: fills with whatever a future frontend happens to send and stops meaning
#: anything.
PORTAL_EVENTS: dict[str, str] = {
    "listing_view": "Opened the property record",
    "link_click": "Opened a document or media asset",
    "map_view": "Looked at the location",
}

#: Payload keys we keep. The rest is discarded. The body is client-controlled
#: and this table is read back into an intent model, so it stores only what the
#: server can make sense of.
_PORTAL_PAYLOAD_KEYS = ("asset", "asset_id", "section", "kind")


def _clean_portal_payload(payload: Optional[dict]) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = {}
    for key in _PORTAL_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            out[key] = str(value)[:120]
    return out


async def _record_portal_activity(
    conn,
    *,
    tenant_id: str,
    lead_id: str,
    portal_id: str,
    interaction_type: str,
    link_kind: Optional[str] = None,
    payload: Optional[dict] = None,
    cooldown: Optional[timedelta] = None,
) -> bool:
    """Write one homeowner action. Returns whether a row was actually created.

    Anchored to BOTH the lead and, when the lead names one, the client — so the
    row satisfies the 0012 anchor CHECK either way, and feeds per-person intent
    when the link exists. A lead with no seller_client_id still records; the
    row is simply not yet attributable to a person, and perception coverage can
    say so rather than the event being dropped.
    """
    over_cap = await conn.fetchval(
        """
        SELECT count(*) >= $2 FROM interaction_logs
         WHERE portal_id = $1::uuid AND created_at > now() - interval '1 hour'
        """,
        portal_id, PORTAL_ACTIVITY_HOURLY_CAP,
    )
    if over_cap:
        log.warning("portal %s exceeded the hourly activity cap; dropping", portal_id)
        return False

    if cooldown is not None:
        recent = await conn.fetchval(
            """
            SELECT 1 FROM interaction_logs
             WHERE portal_id = $1::uuid
               AND interaction_type = $2
               AND created_at > now() - $3::interval
             LIMIT 1
            """,
            portal_id, interaction_type, cooldown,
        )
        if recent:
            return False

    client_id = await conn.fetchval(
        "SELECT seller_client_id FROM leads WHERE id = $1::uuid", lead_id
    )
    # Mirrors the rule inside resolve_portal_token: a joint_venture link is
    # opened by a buyer, everything else by the seller. Hardcoding 'seller' here
    # would make the same person's activity carry two different actor_roles
    # depending on which code path recorded it, and intent_states reads that
    # column.
    actor_role = "buyer" if link_kind == "joint_venture" else "seller"
    await conn.execute(
        """
        INSERT INTO interaction_logs
            (tenant_id, lead_id, client_id, portal_id, actor_role,
             interaction_type, payload)
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7::jsonb)
        """,
        tenant_id, lead_id, client_id, portal_id, actor_role,
        interaction_type, json.dumps(_clean_portal_payload(payload)),
    )
    return True


class PortalActivity(BaseModel):
    """One thing the homeowner did, reported by the portal page.

    No identity fields. tenant, lead and portal are taken from the signed
    session; accepting them here would make the endpoint a cross-tenant write.
    """
    model_config = ConfigDict(extra="forbid")

    event: Literal["listing_view", "link_click", "map_view"]
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/activity", status_code=202)
async def record_activity(
    body: PortalActivity, authorization: Optional[str] = Header(default=None),
):
    """Record a homeowner action from inside a live portal session.

    202 rather than 201: a suppressed duplicate is a success, and the caller is
    a page that should not care which happened. Failure to record is never
    surfaced to the homeowner either — this is telemetry about them, and it
    must not be able to break the page they came to read.
    """
    claims = _portal_session_claims(authorization)
    ctx = TenantContext(
        agent_id=f"portal:{claims['portal_id']}",
        tenant_id=str(claims["tenant_id"]),
        role=Role.AGENT,
    )
    try:
        async with tenant_tx(ctx) as conn:
            live = await conn.fetchval(
                """
                SELECT 1 FROM client_portals
                 WHERE id=$1::uuid AND lead_id=$2::uuid
                   AND revoked_at IS NULL AND access_expires_at > now()
                """,
                claims["portal_id"], claims["lead_id"],
            )
            # A revoked link must stop producing signal immediately, not merely
            # stop serving content. Otherwise a session held open after
            # revocation keeps writing to the CRM.
            if not live:
                raise HTTPException(status_code=404, detail="Link invalid or expired.")
            recorded = await _record_portal_activity(
                conn,
                tenant_id=str(claims["tenant_id"]),
                lead_id=str(claims["lead_id"]),
                portal_id=str(claims["portal_id"]),
                interaction_type=body.event,
                link_kind=claims.get("link_kind"),
                payload=body.payload,
                # Per-asset events are individually meaningful; only the
                # whole-page view needs collapsing.
                cooldown=PORTAL_VIEW_COOLDOWN if body.event == "listing_view" else None,
            )
        return {"recorded": recorded}
    except HTTPException:
        raise
    except Exception:
        log.warning("portal activity not recorded", exc_info=True)
        return {"recorded": False}


@router.get("/dossier")
async def read_scoped_dossier(authorization: Optional[str] = Header(default=None)):
    """Return only the fields granted by a still-live, revocable portal row."""
    claims = _portal_session_claims(authorization)
    ctx = TenantContext(
        agent_id=f"portal:{claims['portal_id']}",
        tenant_id=str(claims["tenant_id"]),
        role=Role.AGENT,
    )
    async with tenant_tx(ctx) as conn:
        portal = await conn.fetchrow(
            """
            SELECT * FROM client_portals
             WHERE id=$1::uuid AND lead_id=$2::uuid
               AND revoked_at IS NULL AND access_expires_at > now()
            """,
            claims["portal_id"],
            claims["lead_id"],
        )
        if portal is None:
            raise HTTPException(status_code=404, detail="Link invalid or expired.")

        # The homeowner opened their dossier. Recorded server-side because it
        # is the one behavioural fact that needs no cooperation from the page
        # and cannot be forged by it — the request itself is the evidence.
        # Never allowed to break the read: someone who came to look at their
        # own property record must get it even if telemetry is failing.
        try:
            await _record_portal_activity(
                conn,
                tenant_id=str(claims["tenant_id"]),
                lead_id=str(claims["lead_id"]),
                portal_id=str(claims["portal_id"]),
                interaction_type="portal_view",
                link_kind=claims.get("link_kind"),
                cooldown=PORTAL_VIEW_COOLDOWN,
            )
        except Exception:
            log.warning("portal view not recorded", exc_info=True)

        scope = dict(_json_value(portal["asset_scope"]) or {})
        lead = await conn.fetchrow(
            """
            SELECT id,parcel_id,state,address,asking_price,dossier_status,
                   contract_execution_date,contract_expires_at,updated_at
              FROM leads WHERE id=$1::uuid
            """,
            claims["lead_id"],
        )
        if lead is None:
            raise HTTPException(status_code=404, detail="Dossier is unavailable.")

        assets: dict[str, Any] = {}
        if scope.get("summary"):
            assets["summary"] = {
                "lead_id": str(lead["id"]),
                "parcel_id": lead["parcel_id"],
                "state": lead["state"],
                "address": lead["address"],
                "asking_price": float(lead["asking_price"]) if lead["asking_price"] else None,
                "dossier_status": lead["dossier_status"],
                "contract_execution_date": (
                    lead["contract_execution_date"].isoformat()
                    if lead["contract_execution_date"] else None
                ),
                "contract_expires_at": (
                    lead["contract_expires_at"].isoformat()
                    if lead["contract_expires_at"] else None
                ),
                "updated_at": lead["updated_at"].isoformat(),
            }
        if scope.get("media"):
            rows = await conn.fetch(
                """
                SELECT id,kind,url,caption,sort_order,created_at
                  FROM property_media WHERE lead_id=$1::uuid
                 ORDER BY sort_order,id
                """,
                claims["lead_id"],
            )
            assets["media"] = [
                {
                    **dict(row),
                    "id": str(row["id"]),
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]
        if scope.get("milestones"):
            rows = await conn.fetch(
                """
                SELECT m.id,m.milestone_type,m.title,m.status,m.due_at,m.completed_at
                  FROM transactions t
                  JOIN transaction_milestones m ON m.transaction_id=t.id
                 WHERE t.lead_id=$1::uuid ORDER BY m.due_at NULLS LAST,m.created_at
                """,
                claims["lead_id"],
            )
            assets["milestones"] = [
                {
                    **dict(row),
                    "id": str(row["id"]),
                    "due_at": row["due_at"].isoformat() if row["due_at"] else None,
                    "completed_at": (
                        row["completed_at"].isoformat() if row["completed_at"] else None
                    ),
                }
                for row in rows
            ]
        if scope.get("title_summary"):
            rows = await conn.fetch(
                """
                SELECT finding_type,amount,recorded_at,released_at,match_status,
                       chain_gap,review_status,notes
                  FROM title_findings WHERE property_key=$1
                 ORDER BY created_at DESC LIMIT 50
                """,
                lead["parcel_id"],
            )
            assets["title_summary"] = [
                {
                    **dict(row),
                    "amount": float(row["amount"]) if row["amount"] is not None else None,
                    "recorded_at": row["recorded_at"].isoformat() if row["recorded_at"] else None,
                    "released_at": row["released_at"].isoformat() if row["released_at"] else None,
                    "warning": "Preliminary public-record finding; not an insured title search.",
                }
                for row in rows
            ]
        if scope.get("zoning_summary"):
            row = await conn.fetchrow(
                """
                SELECT zoning_district,effective_version,lot_area_sqft,
                       building_area_sqft,current_far,max_far,
                       remaining_buildable_sqft,permitted_uses,result,review_status
                  FROM zoning_analyses WHERE property_key=$1
                 ORDER BY created_at DESC LIMIT 1
                """,
                lead["parcel_id"],
            )
            assets["zoning_summary"] = (
                {
                    **dict(row),
                    "result": _json_value(row["result"]),
                    "warning": "Planning and zoning professional review required.",
                }
                if row else None
            )
        if scope.get("underwriting"):
            row = await conn.fetchrow(
                """
                SELECT analysis_type,observation_date,confidence,model_version,
                       evidence_status,result,trace,professional_review_status
                  FROM intelligence_scores
                 WHERE property_key=$1 AND analysis_type='underwriting'
                 ORDER BY observation_date DESC,created_at DESC LIMIT 1
                """,
                lead["parcel_id"],
            )
            assets["underwriting"] = (
                {
                    **dict(row),
                    "observation_date": row["observation_date"].isoformat(),
                    "confidence": float(row["confidence"]),
                    "result": _json_value(row["result"]),
                    "trace": _json_value(row["trace"]),
                }
                if row else None
            )
        if scope.get("documents"):
            rows = await conn.fetch(
                """
                SELECT id,document_type,template_key,template_version,status,
                       reviewed_at,artifact_sha256,created_at
                  FROM contract_documents
                 WHERE lead_id=$1::uuid AND status IN ('approved','signed')
                 ORDER BY created_at DESC
                """,
                claims["lead_id"],
            )
            assets["documents"] = [
                {
                    **dict(row),
                    "id": str(row["id"]),
                    "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]

    return {
        "read_only": True,
        "link_kind": portal["link_kind"],
        "asset_scope": scope,
        "watermark_text": portal["watermark_text"] or "CONFIDENTIAL — REVOCABLE DOSSIER",
        "assets": assets,
    }
