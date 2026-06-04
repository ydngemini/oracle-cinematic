"""
Oracle — Multi-Tenant IAM enforcement layer.

The Python mirror of db/schema.sql. While the backend still runs on in-memory
stores, this module is the single source of truth for tenant isolation and
RBAC; once Postgres lands, the same TenantContext drives the session GUCs that
activate the RLS policies (see apply_rls_context()).

"Domain Controller" translation:
  Forest -> the Oracle platform        Domain -> a brokerage (tenant_id)
  Node   -> an agent/client (role)     Gatekeeper -> require_context() below

Isolation = HYBRID: private data (clients, leads, CRM) is walled off per
tenant; listings are visible to their own tenant + anyone if shared to the MLS
pool, but only ever writable by their owning tenant. platform_admin bypasses
all isolation (the IT-admin god-mode override).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import Header, HTTPException, status


class Role(str, Enum):
    PLATFORM_ADMIN = "platform_admin"  # god-mode: forest owner, bypasses isolation
    BROKER_OWNER = "broker_owner"      # admin of one brokerage/domain
    AGENT = "agent"                    # individual node within a domain


@dataclass(frozen=True)
class TenantContext:
    """The decoded identity for one request. Mirrors the Postgres session GUCs
    app.current_tenant / app.current_role."""
    agent_id: str
    tenant_id: str
    role: Role

    @property
    def is_platform_admin(self) -> bool:
        return self.role is Role.PLATFORM_ADMIN

    @property
    def is_broker_owner(self) -> bool:
        return self.role is Role.BROKER_OWNER


# ---------------------------------------------------------------------------
# Visibility predicates — the exact logic of the RLS policies in schema.sql.
# Each `row` is any mapping carrying a `tenant_id` (and `is_shared_mls` for
# listings). Keep these in lockstep with the CREATE POLICY statements.
# ---------------------------------------------------------------------------

def can_read_private(ctx: TenantContext, row: dict) -> bool:
    """clients / leads — hard wall, no sharing."""
    return ctx.is_platform_admin or row.get("tenant_id") == ctx.tenant_id


def can_read_listing(
    ctx: TenantContext,
    row: dict,
    granted_listing_ids: frozenset = frozenset(),
) -> bool:
    """listings — own tenant, the public MLS pool, OR an explicit co-broke grant.
    `granted_listing_ids` is the set of listing ids granted to ctx.tenant_id
    (the Python mirror of app_has_listing_grant() in 0002_listing_grants.sql)."""
    return (
        ctx.is_platform_admin
        or row.get("tenant_id") == ctx.tenant_id
        or bool(row.get("is_shared_mls"))
        or row.get("id") in granted_listing_ids
    )


def can_write(ctx: TenantContext, row: dict) -> bool:
    """Any mutation (clients, leads, listings) — own tenant only, even when a
    listing is shared. platform_admin may write anywhere."""
    return ctx.is_platform_admin or row.get("tenant_id") == ctx.tenant_id


def require_role(ctx: TenantContext, *allowed: Role) -> None:
    """RBAC gate. platform_admin is implicitly allowed everywhere."""
    if ctx.is_platform_admin or ctx.role in allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Role {ctx.role.value} not permitted for this action.",
    )


# ---------------------------------------------------------------------------
# Postgres bridge — set the per-request GUCs that drive RLS. No-op until a real
# connection is wired; call once at the start of each DB transaction.
# ---------------------------------------------------------------------------

async def apply_rls_context(conn, ctx: TenantContext) -> None:
    """SET LOCAL the session context an asyncpg/psycopg connection so the
    schema.sql RLS policies evaluate against this request's identity. Uses
    set_config so a parameterized, injection-safe path is available."""
    await conn.execute(
        "SELECT set_config('app.current_tenant', $1, true),"
        "       set_config('app.current_role',   $2, true)",
        ctx.tenant_id,
        ctx.role.value,
    )


# ---------------------------------------------------------------------------
# FastAPI dependency — the Auth Gatekeeper. Decodes the Bearer JWT into a
# TenantContext. Imported lazily to avoid a circular import with auth.py.
# ---------------------------------------------------------------------------

def require_context(authorization: Optional[str] = Header(default=None)) -> TenantContext:
    from auth import decode_token  # lazy: auth imports nothing from tenancy

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )

    payload = decode_token(authorization.removeprefix("Bearer ").strip())

    tenant_id = payload.get("tenant_id")
    raw_role = payload.get("role")
    if not tenant_id or not raw_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant_id/role — re-authenticate.",
        )

    try:
        role = Role(raw_role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unknown role in token: {raw_role}",
        )

    return TenantContext(agent_id=payload["sub"], tenant_id=tenant_id, role=role)
