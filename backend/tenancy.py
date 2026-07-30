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

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from policy_contract import PLATFORM_POLICY_VERSION

# ---------------------------------------------------------------------------
# Logging — never log token strings or raw passphrase material.
# ---------------------------------------------------------------------------

log = logging.getLogger("oracle.tenancy")

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

# RFC 4122 UUID — 8-4-4-4-12 hex groups, case-insensitive.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Defensive caps to prevent oversized values from polluting logs or DB GUCs.
_MAX_ROLE_LEN = 64
_MAX_AGENT_ID_LEN = 128


def _validate_tenant_id(tenant_id: str) -> None:
    """Raise 401 if tenant_id is not a well-formed UUID.

    All tenant_id values in Oracle are UUIDs — any other shape indicates a
    tampered or corrupt token. Validating here (before the value touches a DB
    GUC or any lookup) is an injection-hardening belt-and-suspenders measure;
    asyncpg's parameterised set_config already prevents SQL injection, but we
    want to reject nonsense early and log it.
    """
    if not tenant_id or not _UUID_RE.match(tenant_id):
        log.warning(
            "Token contains non-UUID tenant_id (length=%d) — rejecting.",
            len(tenant_id) if tenant_id else 0,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token contains invalid tenant_id — re-authenticate.",
        )


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
#
# INJECTION SAFETY: asyncpg's parameterised execute() uses server-side binding
# ($1, $2) which means the tenant_id and role values are transmitted as typed
# bind parameters, never interpolated into the SQL string. set_config itself
# only stores the value in the GUC table — there is no secondary SQL execution.
# UUID validation in require_context() provides a further pre-flight guard.
#
# GUC RESET: the caller (db/connection.py:tenant_tx) wraps every call inside
# `async with conn.transaction()`, making these SET LOCAL. On transaction
# commit OR rollback the GUCs revert to their prior values automatically —
# there is no leakage of one request's identity into a subsequent connection
# that reuses the same socket from the pool.
# ---------------------------------------------------------------------------

async def apply_rls_context(conn, ctx: TenantContext) -> None:
    """SET LOCAL the session context on an asyncpg/psycopg connection so the
    schema.sql RLS policies evaluate against this request's identity."""
    await conn.execute(
        "SELECT set_config('app.current_tenant', $1, true),"
        "       set_config('app.current_role',   $2, true),"
        "       set_config('app.current_agent',  $3, true)",
        ctx.tenant_id,
        ctx.role.value,
        ctx.agent_id,
    )
    log.debug(
        "RLS context applied: tenant_id=%r role=%r agent_id=%r.",
        ctx.tenant_id,
        ctx.role.value,
        ctx.agent_id,
    )


# ---------------------------------------------------------------------------
# FastAPI dependency — the Auth Gatekeeper. Decodes the Bearer JWT into a
# TenantContext. Imported lazily to avoid a circular import with auth.py.
# ---------------------------------------------------------------------------

def _context_from_authorization(
    authorization: Optional[str],
    *,
    allow_policy_pending: bool,
    allow_stale_policy: bool,
) -> TenantContext:
    from auth import decode_token  # lazy: auth imports nothing from tenancy

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )

    payload = decode_token(authorization.removeprefix("Bearer ").strip())

    if payload.get("purpose"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not valid for this endpoint.",
        )

    if payload.get("policy_pending") and not allow_policy_pending:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accept the platform policy to continue.",
        )

    if payload.get("policy_version") != PLATFORM_POLICY_VERSION and not allow_stale_policy:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accept the current platform policy to continue.",
        )

    tenant_id = payload.get("tenant_id")
    raw_role = payload.get("role")

    if not tenant_id or not raw_role:
        log.debug(
            "Token missing required claims — tenant_id present: %s, role present: %s.",
            bool(tenant_id),
            bool(raw_role),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant_id/role — re-authenticate.",
        )

    # Validate tenant_id is a well-formed UUID before it touches any lookup or GUC.
    _validate_tenant_id(tenant_id)

    # Guard against oversized role strings before enum lookup / log emission.
    if len(raw_role) > _MAX_ROLE_LEN:
        log.debug("Token contains oversized role value (length=%d) — rejecting.", len(raw_role))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token contains unrecognised role — re-authenticate.",
        )

    try:
        role = Role(raw_role)
    except ValueError:
        log.debug("Token contains unknown role %r — rejecting.", raw_role)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unrecognised role in token — re-authenticate.",
        )

    agent_id = payload.get("sub", "")
    if len(agent_id) > _MAX_AGENT_ID_LEN:
        log.debug("Token subject exceeds maximum length (%d) — rejecting.", len(agent_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        )

    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=role)
    log.debug(
        "Tenant context established: agent_id=%r tenant_id=%r role=%r.",
        ctx.agent_id,
        ctx.tenant_id,
        ctx.role.value,
    )
    return ctx


def _request_authorization(request: Request, authorization: Optional[str]) -> Optional[str]:
    """Prefer an explicit bearer token, otherwise authenticate with the secure session cookie."""
    # Preserve direct-call compatibility used by internal WebSocket validation
    # and unit tests while FastAPI supplies a Request for dependency injection.
    if isinstance(request, str):
        return request
    if isinstance(authorization, str) and authorization:
        return authorization
    token = request.cookies.get("oracle_session", "") if request is not None else ""
    return f"Bearer {token}" if token else None


def require_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> TenantContext:
    """Decode a normal application token, rejecting pending or stale-policy sessions."""
    return _context_from_authorization(
        _request_authorization(request, authorization),
        allow_policy_pending=False,
        allow_stale_policy=False,
    )


def require_policy_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> TenantContext:
    """Permit the policy endpoints to renew stale or pending signed sessions."""
    return _context_from_authorization(
        _request_authorization(request, authorization),
        allow_policy_pending=True,
        allow_stale_policy=True,
    )
