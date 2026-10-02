"""HTTP surface of the customer-data lifecycle (privacy_lifecycle.py,
privacy_export.py, privacy_requests.py).

Every irreversible action — export of the whole brokerage, offboarding an
agent, closing the brokerage — requires the owner to re-enter their password
in the same request (auth.confirm_password): a stolen session alone can
neither take every record nor destroy them. A preview of an offboarding needs
no password; it changes nothing.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from admin_ops import require_platform_admin
from db.connection import tenant_tx
from tenancy import TenantContext, require_context

router = APIRouter(prefix="/api/privacy", tags=["Privacy & data lifecycle"])
admin_router = APIRouter(prefix="/api/admin/privacy", tags=["Privacy & data lifecycle"])


def _raise(exc: Exception) -> None:
    from privacy_lifecycle import LifecycleError

    if isinstance(exc, LifecycleError):
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise exc


def _owner(ctx: TenantContext) -> None:
    if not (ctx.is_broker_owner or ctx.is_platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Brokerage owner only.")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExportRequest(_Body):
    password: str = Field(min_length=1, max_length=256)
    include_media: bool = True


class PasswordOnly(_Body):
    password: str = Field(min_length=1, max_length=256)


class OffboardRequest(_Body):
    agent_id: str = Field(min_length=3, max_length=320)
    successor_agent_id: str = Field(min_length=3, max_length=320)
    reason: str = Field(min_length=3, max_length=500)
    preview: bool = True
    password: Optional[str] = Field(default=None, max_length=256)


class ClosureRequest(_Body):
    confirm_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=500)
    password: str = Field(min_length=1, max_length=256)


class WithdrawRequest(_Body):
    reason: str = Field(min_length=3, max_length=500)
    password: str = Field(min_length=1, max_length=256)


class SubjectRequest(_Body):
    kind: Literal["dsr_access", "dsr_delete"]
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=32)
    reason: str = Field(min_length=3, max_length=500)
    preview: bool = True
    password: Optional[str] = Field(default=None, max_length=256)


@router.get("/policy")
async def retention_policy(ctx: TenantContext = Depends(require_context)) -> dict:
    """The retention schedule, as published (FTC: written, finite, published)."""
    from retention_policy import BACKUP_DAYS, POLICY_VERSION, all_policies, closure_grace_days

    return {"policy_version": POLICY_VERSION, "backup_days": BACKUP_DAYS,
            "closure_grace_days": closure_grace_days(),
            "categories": all_policies()}


@router.get("/lifecycle")
async def lifecycle(ctx: TenantContext = Depends(require_context)) -> dict:
    from privacy_lifecycle import list_operations, tenant_lifecycle

    _owner(ctx)
    try:
        state = await tenant_lifecycle(ctx)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {**state, "operations": await list_operations(ctx)}


@router.post("/exports", status_code=202)
async def create_export(body: ExportRequest, ctx: TenantContext = Depends(require_context)) -> dict:
    from auth import confirm_password
    from privacy_export import request_export

    _owner(ctx)
    await confirm_password(ctx, body.password)
    try:
        return await request_export(ctx, include_media=body.include_media)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/exports/{operation_id}/download")
async def download_export(operation_id: str, body: PasswordOnly,
                          ctx: TenantContext = Depends(require_context)):
    from auth import confirm_password
    from privacy_export import export_download

    _owner(ctx)
    await confirm_password(ctx, body.password)
    try:
        target = await export_download(ctx, _uuid(operation_id))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if "url" in target:
        return target
    return FileResponse(target["path"], media_type="application/zip",
                        filename=f"neoh-export-{operation_id}.zip",
                        headers={"Cache-Control": "private, no-store"})


@router.post("/offboard")
async def offboard(body: OffboardRequest, ctx: TenantContext = Depends(require_context)) -> dict:
    from auth import confirm_password
    from privacy_lifecycle import offboard_agent

    _owner(ctx)
    if not body.preview:
        await confirm_password(ctx, body.password or "")
    try:
        return await offboard_agent(ctx, departing_agent_id=body.agent_id,
                                    successor_agent_id=body.successor_agent_id,
                                    reason=body.reason, preview=body.preview)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/closure")
async def close_brokerage(body: ClosureRequest, ctx: TenantContext = Depends(require_context)) -> dict:
    from auth import confirm_password
    from privacy_lifecycle import request_closure

    _owner(ctx)
    await confirm_password(ctx, body.password)
    try:
        return await request_closure(ctx, confirm_name=body.confirm_name, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/closure/withdraw")
async def reopen_brokerage(body: WithdrawRequest, ctx: TenantContext = Depends(require_context)) -> dict:
    from auth import confirm_password
    from privacy_lifecycle import withdraw_closure

    _owner(ctx)
    await confirm_password(ctx, body.password)
    try:
        return await withdraw_closure(ctx, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/requests")
async def subject_request(body: SubjectRequest, ctx: TenantContext = Depends(require_context)) -> dict:
    """A person (a client, a lead) asks the brokerage what it holds on them,
    or to delete it. The brokerage is the controller and decides; Neoh does it."""
    from auth import confirm_password
    from privacy_requests import handle_subject_request

    _owner(ctx)
    if not (body.email or body.phone):
        raise HTTPException(422, "Give the person's email or phone number.")
    if body.kind == "dsr_delete" and not body.preview:
        await confirm_password(ctx, body.password or "")
    try:
        return await handle_subject_request(ctx, kind=body.kind, email=body.email, phone=body.phone,
                                            reason=body.reason, preview=body.preview)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ── Platform operators ─────────────────────────────────────────────────────

class LegalHoldRequest(_Body):
    tenant_id: str = Field(min_length=36, max_length=36)
    scope: Literal["tenant", "user", "contact"] = "tenant"
    subject_ref: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(min_length=3, max_length=2000)


@admin_router.post("/legal-holds", status_code=201)
async def place_legal_hold(body: LegalHoldRequest, ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "INSERT INTO legal_holds (tenant_id, scope, subject_ref, reason, placed_by) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id, placed_at",
            _uuid(body.tenant_id), body.scope, body.subject_ref, body.reason, ctx.agent_id)
    await _admin_audit(ctx, "privacy.legal_hold.placed", body.tenant_id, {"scope": body.scope})
    return {"id": str(row["id"]), "placed_at": row["placed_at"].isoformat()}


@admin_router.post("/legal-holds/{hold_id}/release")
async def release_legal_hold(hold_id: str, ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "UPDATE legal_holds SET released_at=now(), released_by=$2 "
            "WHERE id=$1 AND released_at IS NULL RETURNING tenant_id", _uuid(hold_id), ctx.agent_id)
    if row is None:
        raise HTTPException(404, "No active hold with that id.")
    await _admin_audit(ctx, "privacy.legal_hold.released", str(row["tenant_id"]), {"hold_id": hold_id})
    return {"released": True}


@admin_router.post("/erasures/{tenant_id}/start", status_code=202)
async def start_erasure(tenant_id: str, ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    """Begin erasure of a closing brokerage now, before its grace period ends
    (the owner asked for immediate deletion in writing)."""
    from privacy_lifecycle import begin_erasure

    try:
        op_id = await begin_erasure(_uuid(tenant_id), requested_by=ctx.agent_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if op_id is None:
        raise HTTPException(409, "Erasure is blocked by a legal hold.")
    await _admin_audit(ctx, "privacy.erasure.started", tenant_id, {"operation_id": op_id})
    return {"operation_id": op_id}


class MlsPurgeRequest(_Body):
    confirm_mls_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)
    preview: bool = True


@admin_router.post("/mls-feeds/{mls_id}/purge")
async def purge_mls_feed(mls_id: str, body: MlsPurgeRequest,
                         ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    """The MLS licence ended: delete the feed's listings (runbook §R2)."""
    from privacy_lifecycle import purge_mls_feed as _purge

    if body.confirm_mls_id != mls_id:
        raise HTTPException(422, "Type the feed id exactly to confirm.")
    try:
        out = await _purge(mls_id, requested_by=ctx.agent_id, reason=body.reason, preview=body.preview)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if not body.preview:
        await _admin_audit(ctx, "privacy.mls.purged", mls_id, {"deleted": out.get("deleted")})
    return out


@admin_router.get("/operations")
async def operations(tenant_id: Optional[str] = None, ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT id, tenant_id, kind, state, requested_by, requested_at, completed_at, error, receipt "
            "FROM privacy_operations WHERE ($1::uuid IS NULL OR tenant_id=$1::uuid) "
            "ORDER BY requested_at DESC LIMIT 200", _uuid(tenant_id) if tenant_id else None)
    from privacy_lifecycle import _jsonable

    return {"operations": [_jsonable(dict(r)) for r in rows]}


def _uuid(value: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise HTTPException(422, "Not a valid id.") from exc


async def _admin_audit(ctx: TenantContext, action: str, target: str, metadata: dict) -> None:
    from audit_ledger import AuditCategory, ledger

    try:
        await ledger.record(AuditCategory.ADMIN_ACTION, action, tenant_id=ctx.tenant_id,
                            user_id=ctx.agent_id, target_id=target, metadata=metadata)
    except Exception:  # noqa: BLE001
        pass
