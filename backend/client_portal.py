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
from datetime import datetime, timezone
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import ALGORITHM, SECRET_KEY
from db.connection import get_pool, tenant_tx
from tenancy import TenantContext, require_context

log = logging.getLogger("oracle.client_portal")

router = APIRouter(prefix="/portal", tags=["client-portal"])

PORTAL_BASE_URL = os.environ.get("ORACLE_BASE_URL", "http://localhost:5173")
MAX_EXPIRY_DAYS = 90


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PortalLinkRequest(BaseModel):
    lead_id: UUID
    expiry_days: int = Field(default=7, ge=1, le=MAX_EXPIRY_DAYS)


class PortalLinkResponse(BaseModel):
    portal_id: str
    secure_url: str
    access_expires_at: str


class PortalSessionResponse(BaseModel):
    authenticated: bool
    session_token: str
    lead_id: str
    expires_at: str


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

    async with tenant_tx(ctx) as conn:
        # RLS scopes this lookup — a foreign lead_id reads as nonexistent.
        lead = await conn.fetchrow("SELECT id FROM leads WHERE id = $1", body.lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found.")

        row = await conn.fetchrow(
            """
            INSERT INTO client_portals
                (tenant_id, lead_id, token_hash, access_expires_at, created_by)
            VALUES
                ($1, $2, $3, now() + make_interval(days => $4), $5)
            RETURNING id, access_expires_at
            """,
            UUID(ctx.tenant_id), body.lead_id, token_hash,
            body.expiry_days, ctx.agent_id,
        )

    log.info(
        "Portal link minted: portal_id=%s lead_id=%s tenant_id=%s by agent=%s",
        row["id"], body.lead_id, ctx.tenant_id, ctx.agent_id,
    )
    return PortalLinkResponse(
        portal_id=str(row["id"]),
        secure_url=f"{PORTAL_BASE_URL}/vault/secure-access/{token}",
        access_expires_at=row["access_expires_at"].isoformat(),
    )


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
    claims = {
        "role": "portal_client",
        "tenant_id": str(row["tenant_id"]),
        "lead_id": str(row["lead_id"]),
        "portal_id": str(row["portal_id"]),
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    session_token = jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)

    return PortalSessionResponse(
        authenticated=True,
        session_token=session_token,
        lead_id=str(row["lead_id"]),
        expires_at=expires_at.isoformat(),
    )
