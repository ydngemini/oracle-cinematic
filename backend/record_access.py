"""Who may see and change a client or a contact: assignment, the brokerage
pool, shares.

Owner decision D-2026-10-13-01 (prompt 03):
  * a record with no assignee is the brokerage pool — every agent sees it;
  * an agent-created record is not auto-assigned;
  * a share grants 'view' or 'edit', chosen per share.

The decision itself lives in the database: app_client_access() (0131) and
app_contact_access() (0134) are used by the RLS policies AND read back here,
so the API never re-implements them (`tenancy.client_access` /
`tenancy.contact_access` are the Python mirrors the tests hold them to). This
module adds what RLS cannot say: 403 versus 404, who may assign, who may share.

Routes:
  GET    /api/crm/colleagues                           — active members to assign or share with
  GET    /api/crm/clients/{id}/access                  — the caller's access + active shares
  POST   /api/crm/clients/{id}/shares                  — share (or change a share's permission)
  DELETE /api/crm/clients/{id}/shares/{share_id}       — revoke (kept as history)
  GET    /api/crm/contacts/{id}/access                 — the same, for a contact (0134)
  POST   /api/crm/contacts/{id}/shares
  DELETE /api/crm/contacts/{id}/shares/{share_id}
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from db.connection import DatabaseUnavailable, tenant_tx
from tenancy import Role, TenantContext, require_context

log = logging.getLogger("oracle.record_access")

router = APIRouter(prefix="/api/crm", tags=["Agent CRM"])

NOT_FOUND = "client not found"
VIEW_ONLY = "This client is shared with you to view only."
CONTACT_NOT_FOUND = "contact not found"
CONTACT_VIEW_ONLY = "This contact is shared with you to view only."


@dataclass(frozen=True)
class _Kind:
    """One assignable, shareable record type. Every SQL identifier below comes
    from these two fixed instances, never from input."""
    kind: str            # record_shares.resource_kind
    table: str
    assignee_user: str   # canonical uuid column (derived by trigger)
    assignee_text: str   # legacy text column the trigger canonicalizes
    access_fn: str       # the database decision
    noun: str
    not_found: str
    view_only: str


CLIENT = _Kind("client", "clients", "assignee_user_id", "assignee_id", "app_client_access",
               "client", NOT_FOUND, VIEW_ONLY)
CONTACT = _Kind("contact", "agent_contacts", "assigned_user_id", "assigned_agent_id", "app_contact_access",
                "contact", CONTACT_NOT_FOUND, CONTACT_VIEW_ONLY)


# ── the decision, read from the database ───────────────────────────────────

async def _access_level(conn, k: _Kind, record_id: str) -> Optional[str]:
    row = await conn.fetchrow(
        f"SELECT {k.access_fn}(r.{k.assignee_user}, r.id, true) AS can_edit "
        f"FROM {k.table} r WHERE r.id = $1::uuid",
        record_id,
    )
    if row is None:
        return None
    return "edit" if row["can_edit"] else "view"


async def _require_edit(conn, k: _Kind, record_id: str) -> None:
    level = await _access_level(conn, k, record_id)
    if level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, k.not_found)
    if level != "edit":
        raise HTTPException(status.HTTP_403_FORBIDDEN, k.view_only)


async def _require_visible(conn, k: _Kind, record_id: str) -> str:
    level = await _access_level(conn, k, record_id)
    if level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, k.not_found)
    return level


async def client_access_level(conn, client_id: str) -> Optional[str]:
    """None when the caller cannot see the client (or it does not exist —
    indistinguishable on purpose), else 'edit' or 'view'."""
    return await _access_level(conn, CLIENT, client_id)


async def require_client_edit(conn, client_id: str) -> None:
    """404 when invisible, 403 when shared view-only."""
    await _require_edit(conn, CLIENT, client_id)


async def require_client_visible(conn, client_id: str) -> str:
    return await _require_visible(conn, CLIENT, client_id)


async def contact_access_level(conn, contact_id: str) -> Optional[str]:
    """The same as client_access_level, for agent_contacts (0134)."""
    return await _access_level(conn, CONTACT, contact_id)


async def require_contact_edit(conn, contact_id: str) -> None:
    await _require_edit(conn, CONTACT, contact_id)


async def require_contact_visible(conn, contact_id: str) -> str:
    return await _require_visible(conn, CONTACT, contact_id)


async def _actor_user_id(conn) -> Optional[str]:
    value = await conn.fetchval("SELECT app_current_user_id()")
    return str(value) if value else None


async def find_member(conn, tenant_id: str, ref: str) -> Optional[dict[str, Any]]:
    """An ACTIVE member of this brokerage by agent id (case-insensitive) or
    users.id. The same lower() match app_begin_session and the 0131 trigger use."""
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        as_uuid: Optional[uuid.UUID] = uuid.UUID(ref)
    except ValueError:
        as_uuid = None
    row = await conn.fetchrow(
        """SELECT id, agent_id, role, full_name, email FROM users
            WHERE tenant_id = $1::uuid AND is_active
              AND (lower(agent_id) = lower($2) OR id = $3::uuid)
            LIMIT 1""",
        tenant_id, ref, as_uuid,
    )
    return dict(row) if row else None


# ── assignment rules ───────────────────────────────────────────────────────

async def _decide_assignment(
    conn, ctx: TenantContext, k: _Kind, current_assignee: Optional[Any], new_value: Optional[str],
) -> Optional[str]:
    """The owner (or platform admin) assigns to any active member. An agent may
    take a record from the pool, or hand back to the pool one assigned to
    them; giving one to a colleague — or taking a colleague's, even with an
    edit share — is the owner's call (F03.02; app_may_reassign() in 0134 says
    the same at the database)."""
    target = None
    if new_value is not None and str(new_value).strip():
        target = await find_member(conn, ctx.tenant_id, str(new_value))
        if target is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That person is not an active member of this brokerage.",
            )
    if ctx.is_platform_admin or ctx.is_broker_owner:
        return target["agent_id"] if target else None

    me = await _actor_user_id(conn)
    assigned_to_me = me is not None and str(current_assignee or "") == me
    if current_assignee is not None and not assigned_to_me:
        if target is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Only the broker owner can move a colleague's {k.noun} to the pool.")
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Only the broker owner can reassign a colleague's {k.noun}.")
    if target is None:
        return None
    if str(target["id"]) == me:
        return target["agent_id"]
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        f"Only the broker owner can assign a {k.noun} to a colleague. Share it instead.",
    )


async def _resolve_existing(conn, ctx: TenantContext, k: _Kind, record_id: str, new_value: Optional[str]) -> Optional[str]:
    current = await conn.fetchrow(
        f"SELECT {k.assignee_user} AS assignee, {k.access_fn}({k.assignee_user}, id, true) AS can_edit "
        f"FROM {k.table} WHERE id = $1::uuid",
        record_id,
    )
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, k.not_found)
    if not current["can_edit"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, k.view_only)
    return await _decide_assignment(conn, ctx, k, current["assignee"], new_value)


async def resolve_assignment(conn, ctx: TenantContext, client_id: str, new_value: Optional[str]) -> Optional[str]:
    """Validate a client's assignee change and return the canonical agent id (or None)."""
    return await _resolve_existing(conn, ctx, CLIENT, client_id, new_value)


async def resolve_contact_assignment(
    conn, ctx: TenantContext, contact_id: Optional[str], new_value: Optional[str],
) -> Optional[str]:
    """The same rules for a contact. contact_id None = a new contact: the
    caller may create it in the pool or for themselves, and only the owner
    creates one for a colleague."""
    if contact_id is None:
        return await _decide_assignment(conn, ctx, CONTACT, None, new_value)
    return await _resolve_existing(conn, ctx, CONTACT, contact_id, new_value)


# ── sharing ────────────────────────────────────────────────────────────────

class ShareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantee: str = Field(..., min_length=1, max_length=160)
    permission: Literal["view", "edit"]
    expires_at: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=500)


def _share_json(r) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "grantee_user_id": str(r["grantee_user_id"]),
        "grantee_agent_id": r["agent_id"],
        "grantee_name": r["full_name"],
        "permission": r["permission"],
        "granted_by": r["granted_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "note": r["note"],
    }


async def _active_shares(conn, k: _Kind, record_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT s.id, s.grantee_user_id, s.permission, s.granted_by, s.created_at,
                  s.expires_at, s.note, u.agent_id, u.full_name
             FROM record_shares s JOIN users u ON u.id = s.grantee_user_id
            WHERE s.resource_kind = $2 AND s.resource_id = $1::uuid
              AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > now())
            ORDER BY s.created_at""",
        record_id, k.kind,
    )
    return [_share_json(r) for r in rows]


async def _may_manage_shares(conn, ctx: TenantContext, k: _Kind, record_id: str) -> dict[str, Any]:
    """The assignee or the owner manages a record's shares. An edit share does
    not let its holder re-share: nobody grants more than they were given."""
    row = await conn.fetchrow(
        f"SELECT id, {k.assignee_user} AS assignee_user_id, {k.assignee_text} AS assignee_id "
        f"FROM {k.table} WHERE id = $1::uuid",
        record_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, k.not_found)
    if ctx.is_platform_admin or ctx.is_broker_owner:
        return dict(row)
    me = await _actor_user_id(conn)
    if row["assignee_user_id"] is not None and me is not None and str(row["assignee_user_id"]) == me:
        return dict(row)
    raise HTTPException(status.HTTP_403_FORBIDDEN,
                        f"Only the assigned agent or the broker owner can share this {k.noun}.")


async def _audit(ctx: TenantContext, action: str, record_id: str, metadata: dict[str, Any]) -> None:
    from audit_ledger import AuditCategory, ledger

    try:
        await ledger.record(
            AuditCategory.ACCESS_GRANT, action,
            tenant_id=ctx.tenant_id, user_id=ctx.agent_id, target_id=record_id, metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — the change committed; the timeline row is the durable record
        log.exception("audit record for %s failed", action)


def _require_uuid(value: str, name: str) -> None:
    try:
        uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} must be a UUID") from None


# A timeline entry inside the share's transaction, where the record has one.
_Timeline = Optional[Callable[[Any, TenantContext, str, str, dict[str, Any]], Awaitable[None]]]


async def _client_timeline(conn, ctx: TenantContext, client_id: str, text: str, meta: dict[str, Any]) -> None:
    from crm import _log_activity

    await _log_activity(conn, ctx, client_id, "system", text, meta)


@router.get("/colleagues")
async def list_colleagues(ctx: TenantContext = Depends(require_context)):
    """Active members of this brokerage — the people a record can be assigned
    or shared to. Names and agent ids only."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """SELECT id, agent_id, role, full_name FROM users
                    WHERE tenant_id = $1::uuid AND is_active AND role IN ('agent','broker_owner')
                    ORDER BY lower(coalesce(full_name, agent_id))""",
                ctx.tenant_id,
            )
            me = await _actor_user_id(conn)
    except DatabaseUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable.") from None
    return {"colleagues": [
        {"user_id": str(r["id"]), "agent_id": r["agent_id"], "role": r["role"],
         "name": r["full_name"] or r["agent_id"], "is_me": str(r["id"]) == me}
        for r in rows
    ]}


async def _get_access(k: _Kind, record_id: str, ctx: TenantContext) -> dict[str, Any]:
    _require_uuid(record_id, f"{k.noun}_id")
    try:
        async with tenant_tx(ctx) as conn:
            level = await _require_visible(conn, k, record_id)
            row = await conn.fetchrow(
                f"SELECT {k.assignee_user} AS assignee_user_id, {k.assignee_text} AS assignee_id "
                f"FROM {k.table} WHERE id = $1::uuid",
                record_id)
            me = await _actor_user_id(conn)
            can_manage = bool(
                ctx.is_platform_admin or ctx.is_broker_owner
                or (row["assignee_user_id"] is not None and me is not None and str(row["assignee_user_id"]) == me)
            )
            shares = await _active_shares(conn, k, record_id) if can_manage else []
    except DatabaseUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable.") from None
    return {
        f"{k.noun}_id": record_id,
        "access": level,
        "pool": row["assignee_user_id"] is None,
        "assignee_id": row["assignee_id"],
        "can_manage_shares": can_manage,
        "shares": shares,
    }


async def _share(k: _Kind, record_id: str, body: ShareCreate, ctx: TenantContext, timeline: _Timeline) -> dict[str, Any]:
    _require_uuid(record_id, f"{k.noun}_id")
    if body.expires_at is not None:
        expires = body.expires_at if body.expires_at.tzinfo else body.expires_at.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expires_at must be in the future")
    try:
        async with tenant_tx(ctx) as conn:
            record = await _may_manage_shares(conn, ctx, k, record_id)
            if record["assignee_user_id"] is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"This {k.noun} is in the brokerage pool: everyone in the brokerage already sees it.",
                )
            grantee = await find_member(conn, ctx.tenant_id, body.grantee)
            if grantee is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "That person is not an active member of this brokerage.")
            if grantee["role"] != Role.AGENT.value:
                raise HTTPException(status.HTTP_409_CONFLICT, f"The broker owner already sees every {k.noun}.")
            if str(grantee["id"]) == str(record["assignee_user_id"]):
                raise HTTPException(status.HTTP_409_CONFLICT, f"That agent is already assigned this {k.noun}.")
            me = await _actor_user_id(conn)
            # A changed permission is a new grant: the old row is revoked, not edited.
            previous = await conn.fetchval(
                """UPDATE record_shares SET revoked_at = now(), revoked_by = $3
                    WHERE resource_kind = $4 AND resource_id = $1::uuid
                      AND grantee_user_id = $2 AND revoked_at IS NULL
                RETURNING permission""",
                record_id, grantee["id"], ctx.agent_id, k.kind,
            )
            share = await conn.fetchrow(
                """INSERT INTO record_shares
                       (tenant_id, resource_kind, resource_id, grantee_user_id, permission,
                        granted_by_user_id, granted_by, note, expires_at)
                   VALUES ($1::uuid, $9, $2::uuid, $3, $4, $5::uuid, $6, $7, $8)
                RETURNING id, grantee_user_id, permission, granted_by, created_at, expires_at, note""",
                ctx.tenant_id, record_id, grantee["id"], body.permission, me, ctx.agent_id,
                body.note, body.expires_at, k.kind,
            )
            if timeline is not None:
                who = grantee["full_name"] or grantee["agent_id"]
                await timeline(
                    conn, ctx, record_id,
                    f"Shared with {who} ({'can edit' if body.permission == 'edit' else 'view only'})",
                    {"share_id": str(share["id"]), "grantee": grantee["agent_id"],
                     "permission": body.permission, "previous": previous},
                )
    except DatabaseUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable.") from None
    await _audit(ctx, f"{k.noun}_shared", record_id,
                 {"grantee": grantee["agent_id"], "permission": body.permission, "previous": previous,
                  "expires_at": body.expires_at.isoformat() if body.expires_at else None})
    return _share_json({**dict(share), "agent_id": grantee["agent_id"], "full_name": grantee["full_name"]})


async def _revoke(k: _Kind, record_id: str, share_id: str, ctx: TenantContext, timeline: _Timeline) -> None:
    _require_uuid(record_id, f"{k.noun}_id")
    _require_uuid(share_id, "share_id")
    try:
        async with tenant_tx(ctx) as conn:
            await _may_manage_shares(conn, ctx, k, record_id)
            revoked = await conn.fetchrow(
                """UPDATE record_shares s SET revoked_at = now(), revoked_by = $3
                     FROM users u
                    WHERE s.id = $1::uuid AND s.resource_kind = $4 AND s.resource_id = $2::uuid
                      AND s.revoked_at IS NULL AND u.id = s.grantee_user_id
                RETURNING u.agent_id, u.full_name, s.permission""",
                share_id, record_id, ctx.agent_id, k.kind,
            )
            if revoked is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "share not found")
            if timeline is not None:
                await timeline(
                    conn, ctx, record_id,
                    f"Stopped sharing with {revoked['full_name'] or revoked['agent_id']}",
                    {"share_id": share_id, "grantee": revoked["agent_id"], "permission": revoked["permission"]},
                )
    except DatabaseUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable.") from None
    await _audit(ctx, f"{k.noun}_share_revoked", record_id,
                 {"grantee": revoked["agent_id"], "permission": revoked["permission"]})


@router.get("/clients/{client_id}/access")
async def get_client_access(client_id: str, ctx: TenantContext = Depends(require_context)):
    return await _get_access(CLIENT, client_id, ctx)


@router.post("/clients/{client_id}/shares", status_code=status.HTTP_201_CREATED)
async def share_client(client_id: str, body: ShareCreate, ctx: TenantContext = Depends(require_context)):
    return await _share(CLIENT, client_id, body, ctx, _client_timeline)


@router.delete("/clients/{client_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(client_id: str, share_id: str, ctx: TenantContext = Depends(require_context)):
    await _revoke(CLIENT, client_id, share_id, ctx, _client_timeline)


# Contacts have no timeline of their own; the audit ledger records the share.
@router.get("/contacts/{contact_id}/access")
async def get_contact_access(contact_id: str, ctx: TenantContext = Depends(require_context)):
    return await _get_access(CONTACT, contact_id, ctx)


@router.post("/contacts/{contact_id}/shares", status_code=status.HTTP_201_CREATED)
async def share_contact(contact_id: str, body: ShareCreate, ctx: TenantContext = Depends(require_context)):
    return await _share(CONTACT, contact_id, body, ctx, None)


@router.delete("/contacts/{contact_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_contact_share(contact_id: str, share_id: str, ctx: TenantContext = Depends(require_context)):
    await _revoke(CONTACT, contact_id, share_id, ctx, None)
