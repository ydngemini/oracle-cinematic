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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

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
    # Set only for an identity decoded from a session token. When true, every
    # tenant transaction re-checks the account (0117 app_begin_session): still
    # active, same tenant and role, same session epoch. Server-built contexts
    # (jobs, webhooks, pre-auth lookups) leave it false. Excluded from equality
    # so a decoded context still equals one built by hand for the same identity.
    verify_session: bool = field(default=False, compare=False)
    user_id: Optional[str] = field(default=None, compare=False)
    session_epoch: int = field(default=0, compare=False)
    # When the password was last proven; renewals carry it forward and stop
    # at auth.MAX_SESSION_AGE_SECONDS.
    auth_time: Optional[int] = field(default=None, compare=False)
    # Token expiry (epoch seconds). Long-lived sockets close when it passes.
    expires_at: Optional[float] = field(default=None, compare=False)
    # Which clients and contacts an AGENT context reads (0131/0134,
    # D-2026-10-13-01): "actor" = their own, the unassigned pool and what is
    # shared with them; "brokerage" = every client and contact of the tenant,
    # for system pipelines that act for the brokerage (client portal, inbound
    # SMS/voice matching and intake, the client-AI steward). Never
    # "brokerage" for a verified user session — see apply_rls_context and
    # brokerage_scope().
    record_scope: str = field(default="actor", compare=False)
    # The session was signed in without the second factor its account must
    # use (L17-19): only the policy and two-step-setup routes accept it.
    mfa_enrolment_pending: bool = field(default=False, compare=False)

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
    """clients / leads — the tenant wall (clients_tenant_isolation). For clients
    and contacts the agent scope narrows it further: see client_access() and
    contact_access()."""
    return ctx.is_platform_admin or row.get("tenant_id") == ctx.tenant_id


RECORD_SCOPES = ("actor", "brokerage")


def brokerage_scope(ctx: TenantContext) -> TenantContext:
    """The same identity, reading every client and contact of its brokerage
    (0131, 0134).

    For server-built pipeline contexts only. A verified user session keeps its
    own scope: apply_rls_context ignores this flag when verify_session is set."""
    from dataclasses import replace

    return replace(ctx, record_scope="brokerage")


def client_access(
    ctx: TenantContext,
    row: dict,
    actor_user_id: Optional[str],
    shares: dict | None = None,
) -> Optional[str]:
    """Mirror of app_client_access() (0131): None, "view" or "edit".

    `row` carries tenant_id, id and assignee_user_id; `actor_user_id` is the
    caller's users.id (app_current_user_id()); `shares` maps client id →
    permission of the caller's ACTIVE shares. Brokerage-wide identities edit
    everything in their tenant; otherwise the pool and the assignee edit, and a
    share grants exactly its permission."""
    return _record_access(ctx, row, row.get("assignee_user_id"), actor_user_id, shares)


def contact_access(
    ctx: TenantContext,
    row: dict,
    actor_user_id: Optional[str],
    shares: dict | None = None,
) -> Optional[str]:
    """Mirror of app_contact_access() (0134): the same rule as client_access,
    on agent_contacts.assigned_user_id; `shares` maps contact id → permission
    of the caller's ACTIVE 'contact' shares."""
    return _record_access(ctx, row, row.get("assigned_user_id"), actor_user_id, shares)


def _record_access(
    ctx: TenantContext,
    row: dict,
    assignee: Any,
    actor_user_id: Optional[str],
    shares: dict | None,
) -> Optional[str]:
    if not can_read_private(ctx, row):
        return None
    if (
        ctx.is_platform_admin
        or ctx.is_broker_owner
        or (ctx.record_scope == "brokerage" and not ctx.verify_session)
    ):
        return "edit"
    if assignee is None or (actor_user_id is not None and str(assignee) == str(actor_user_id)):
        return "edit"
    if actor_user_id is None:
        return None
    granted = (shares or {}).get(str(row.get("id")))
    return granted if granted in ("view", "edit") else None


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
    schema.sql RLS policies evaluate against this request's identity.

    JIT is off for the transaction too. The agent-scope policies (0131-0142)
    are costed as one lookup per row, so a scan of a few thousand rows crosses
    jit_above_cost and compiled for 1.5-3 s to return a handful of rows
    (L03-06, L16-03). Request transactions are short OLTP reads."""
    if ctx.verify_session:
        # Same three GUCs, plus the account check that makes revocation real:
        # a token issued before a password change, reset, demotion or removal
        # fails here (SQLSTATE 28000 → 401) on its next tenant transaction.
        # A user session always reads as itself (0131): record_scope 'actor'.
        await conn.execute(
            "SELECT app_begin_session($1, $2, $3, true, $4::uuid, $5),"
            "       set_config('app.record_scope', 'actor', true),"
            "       set_config('jit', 'off', true)",
            ctx.tenant_id,
            ctx.role.value,
            ctx.agent_id,
            ctx.user_id,
            ctx.session_epoch,
        )
    else:
        scope = ctx.record_scope if ctx.record_scope in RECORD_SCOPES else "actor"
        await conn.execute(
            "SELECT set_config('app.current_tenant', $1, true),"
            "       set_config('app.current_role',   $2, true),"
            "       set_config('app.current_agent',  $3, true),"
            "       set_config('app.record_scope',   $4, true),"
            "       set_config('jit',                'off', true)",
            ctx.tenant_id,
            ctx.role.value,
            ctx.agent_id,
            scope,
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
    allow_mfa_enrolment: bool = False,
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

    mfa_enrolment = payload.get("mfa_enrol", False)
    if not isinstance(mfa_enrolment, bool):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    if mfa_enrolment and not allow_mfa_enrolment:
        # A broker owner (or a member of a brokerage that requires it) signed
        # in without two-step sign-in set up. Until it is, the session can
        # reach the policy routes, logout, CSRF and the setup routes only.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "mfa_enrolment_required",
                    "message": "Set up two-step sign-in to continue."},
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

    user_id = payload.get("uid")
    if user_id is not None and (not isinstance(user_id, str) or not _UUID_RE.match(user_id)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    epoch = payload.get("sep", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not 0 <= epoch < 2**31:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")

    from auth import MAX_SESSION_AGE_SECONDS

    auth_time = payload.get("auth_time", payload.get("iat"))
    if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    if time.time() - auth_time > MAX_SESSION_AGE_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has ended. Sign in again.",
        )

    ctx = TenantContext(
        agent_id=agent_id,
        tenant_id=tenant_id,
        role=role,
        verify_session=not _is_environment_identity(agent_id, tenant_id, role),
        user_id=user_id,
        session_epoch=epoch,
        auth_time=int(auth_time),
        expires_at=float(payload["exp"]) if isinstance(payload.get("exp"), (int, float)) else None,
        mfa_enrolment_pending=mfa_enrolment,
    )
    log.debug(
        "Tenant context established: agent_id=%r tenant_id=%r role=%r.",
        ctx.agent_id,
        ctx.tenant_id,
        ctx.role.value,
    )
    return ctx


def _is_environment_identity(agent_id: str, tenant_id: str, role: Role) -> bool:
    """The operator/demo identities configured by environment (auth.DEMO_TENANCY)
    have no users row to check; every other subject must have one. Exact match
    on all three — an account that merely shares the operator's address in some
    other tenant or role is an ordinary account and is checked."""
    from auth import DEMO_TENANCY  # lazy: auth imports nothing from tenancy

    return DEMO_TENANCY.get(agent_id) == (tenant_id, role.value)


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


async def verify_session_current(ctx: TenantContext) -> None:
    """Prove a decoded session is still live right now (0117), without waiting
    for the handler's first tenant transaction.

    Every tenant transaction already runs this check, so ordinary data routes
    need nothing extra. Privileged gates call it because some of their routes
    answer from process memory and never open a transaction — a demoted or
    deactivated admin's token would otherwise still read them for its last day.
    """
    if not ctx.verify_session:
        return
    import asyncpg
    from db.connection import tenant_tx  # lazy: db.connection imports tenancy

    try:
        async with tenant_tx(ctx):
            pass
    except asyncpg.exceptions.InvalidAuthorizationSpecificationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has ended. Sign in again.",
        ) from None


def require_policy_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> TenantContext:
    """Permit the policy endpoints to renew stale or pending signed sessions.

    A session waiting for two-step setup (L17-19) may accept the policy too;
    every re-mint carries its `mfa_enrol` claim forward."""
    return _context_from_authorization(
        _request_authorization(request, authorization),
        allow_policy_pending=True,
        allow_stale_policy=True,
        allow_mfa_enrolment=True,
    )


def require_mfa_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> TenantContext:
    """The two-step sign-in routes (mfa_api.py): reachable while setup is still
    owed, never with a pending or stale policy (the policy gate comes first)."""
    return _context_from_authorization(
        _request_authorization(request, authorization),
        allow_policy_pending=False,
        allow_stale_policy=False,
        allow_mfa_enrolment=True,
    )
