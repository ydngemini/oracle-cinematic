"""Two-step sign-in routes for tenant users (L17-19).

    GET  /auth/mfa                  status: on/off, required and why, codes left
    POST /auth/mfa/totp/setup       a new authenticator key (QR + manual key)
    POST /auth/mfa/totp/confirm     first code from the app → on + recovery codes
    POST /auth/mfa/recovery-codes   new recovery codes (password + a code)
    POST /auth/mfa/disable          turn it off (password + a code)

These accept a session that is still owed two-step setup (tenancy
require_mfa_context): a broker owner who signs in without it is held on
exactly these routes, the policy routes, logout and CSRF until it is on.

Resets by someone else live with the people allowed to do them —
`reset_member_mfa` is called by the brokerage team route (a broker owner
resets an agent) and the operator route (a platform admin resets anyone,
owners included). Both are audited.

Turning it on, off, or a reset ends the account's other sessions (session
epoch, 0117); the device that made the change gets a fresh session.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

import user_mfa
from tenancy import TenantContext, require_mfa_context

log = logging.getLogger("oracle.mfa")

router = APIRouter(prefix="/auth/mfa", tags=["auth"])

_SQL_BUMP_EPOCH = (
    "UPDATE users SET session_epoch = session_epoch + 1, updated_at = now() "
    " WHERE id = $1 AND tenant_id = $2::uuid RETURNING session_epoch"
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SetupRequest(_Body):
    # Needed unless the password was typed at sign-in in the last 10 minutes.
    password: Optional[str] = Field(default=None, max_length=256)


class CodeRequest(_Body):
    code: str = Field(min_length=6, max_length=32)


class ProtectedRequest(_Body):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=32)


def _raise(exc: user_mfa.MfaError) -> None:
    raise HTTPException(exc.status_code, exc.detail) from None


async def _account(ctx: TenantContext):
    """The caller's users row, proven to be this live session (the check
    change-password makes). Environment identities have no row: they manage
    their factor in configuration (ORACLE_ADMIN_TOTP_SECRET)."""
    from auth import _lookup_user

    row = await _lookup_user(ctx.agent_id)
    if row is None or not row["password_hash"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This account's sign-in is managed by configuration, not here.")
    if (
        str(row["tenant_id"]) != str(ctx.tenant_id)
        or row["role"] != ctx.role.value
        or int(row["session_epoch"] or 0) != int(ctx.session_epoch or 0)
        or (ctx.user_id and str(ctx.user_id) != str(row["id"]))
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended. Sign in again.")
    return row


def _required(row) -> Optional[str]:
    return user_mfa.required_reason(row["role"], row["tenant_requires_mfa"])


async def _renew(response: Response, ctx: TenantContext, row, *, epoch: int, enrolment_owed: bool) -> Optional[str]:
    from auth import _browser_token, _issue_jwt, _set_session_cookie

    token = _issue_jwt(
        str(row["agent_id"]), str(row["tenant_id"]), str(row["role"]),
        user_id=str(row["id"]), session_epoch=epoch, auth_time=ctx.auth_time,
        mfa_enrolment=enrolment_owed,
    )
    _set_session_cookie(response, token)
    return _browser_token(token)


async def _check_code(row, code: str) -> str:
    """A current code (or recovery code) for an enabled factor; same counter
    and lockout as sign-in."""
    from auth import _require_user_second_factor

    return await _require_user_second_factor(row, code)


@router.get("")
async def mfa_status(ctx: TenantContext = Depends(require_mfa_context)) -> dict[str, Any]:
    from auth import _admin_ctx, _lookup_user
    from db.connection import tenant_tx

    row = await _lookup_user(ctx.agent_id)
    if row is None or not row["password_hash"] or str(row["tenant_id"]) != str(ctx.tenant_id):
        return {"available": False, "enabled": False, "required": False, "required_reason": None,
                "enrolment_required": False}
    async with tenant_tx(_admin_ctx()) as conn:
        current = await user_mfa.state(conn, row["id"])
    reason = _required(row)
    return {
        "available": True,
        "enabled": current["enabled"],
        "enrolled_at": current["enrolled_at"],
        "recovery_codes_remaining": current["recovery_codes_remaining"],
        "locked_until": current["locked_until"],
        "required": reason is not None,
        "required_reason": reason,
        # This session is held on the setup screens until two-step is on.
        "enrolment_required": ctx.mfa_enrolment_pending and not current["enabled"],
        "method": "totp",
    }


@router.post("/totp/setup")
async def start_setup(body: SetupRequest, ctx: TenantContext = Depends(require_mfa_context)) -> dict[str, Any]:
    from auth import _admin_ctx, _check_rate_limit, confirm_password
    from db.connection import tenant_tx

    row = await _account(ctx)
    fresh = ctx.auth_time is not None and time.time() - ctx.auth_time <= user_mfa.FRESH_SIGN_IN_SECONDS
    if not fresh:
        # A stolen session must not be able to enrol the thief's phone.
        await confirm_password(ctx, body.password or "")
    else:
        await _check_rate_limit(ctx.agent_id)
    try:
        async with tenant_tx(_admin_ctx()) as conn:
            setup = await user_mfa.begin_enrolment(
                conn, user_id=row["id"], tenant_id=row["tenant_id"], account=str(row["agent_id"]))
    except user_mfa.MfaError as exc:
        _raise(exc)
    log.info("Two-step setup started (user_id=%s)", row["id"])
    return setup


@router.post("/totp/confirm")
async def confirm_setup(body: CodeRequest, response: Response,
                        ctx: TenantContext = Depends(require_mfa_context)) -> dict[str, Any]:
    from auth import _admin_ctx, _audit_mfa, _check_rate_limit
    from db.connection import tenant_tx

    row = await _account(ctx)
    await _check_rate_limit(ctx.agent_id)
    try:
        async with tenant_tx(_admin_ctx()) as conn:
            codes = await user_mfa.confirm_enrolment(conn, user_id=row["id"], code=body.code)
            epoch = await conn.fetchval(_SQL_BUMP_EPOCH, row["id"], str(row["tenant_id"]))
    except user_mfa.MfaError as exc:
        _raise(exc)
    token = await _renew(response, ctx, row, epoch=int(epoch), enrolment_owed=False)
    log.info("Two-step sign-in turned on (user_id=%s)", row["id"])
    await _audit_mfa("auth.mfa.enabled", tenant_id=row["tenant_id"], actor=str(row["agent_id"]),
                     target_user_id=row["id"], metadata={"method": "totp"})
    return {"enabled": True, "recovery_codes": codes, "token": token,
            "detail": "Two-step sign-in is on. Your other sessions were signed out."}


@router.post("/recovery-codes")
async def new_recovery_codes(body: ProtectedRequest, ctx: TenantContext = Depends(require_mfa_context)) -> dict[str, Any]:
    from auth import _admin_ctx, _audit_mfa, confirm_password
    from db.connection import tenant_tx

    row = await _account(ctx)
    await confirm_password(ctx, body.password)
    await _check_code(row, body.code)
    try:
        async with tenant_tx(_admin_ctx()) as conn:
            codes = await user_mfa.regenerate_recovery_codes(conn, user_id=row["id"])
    except user_mfa.MfaError as exc:
        _raise(exc)
    await _audit_mfa("auth.mfa.recovery_codes_regenerated", tenant_id=row["tenant_id"],
                     actor=str(row["agent_id"]), target_user_id=row["id"])
    return {"recovery_codes": codes}


@router.post("/disable")
async def disable(body: ProtectedRequest, response: Response,
                  ctx: TenantContext = Depends(require_mfa_context)) -> dict[str, Any]:
    """Turn two-step sign-in off. For an account that must use it, this is
    how a new phone is set up: the session goes straight back to setup."""
    from auth import _admin_ctx, _audit_mfa, confirm_password
    from db.connection import tenant_tx

    row = await _account(ctx)
    await confirm_password(ctx, body.password)
    await _check_code(row, body.code)
    async with tenant_tx(_admin_ctx()) as conn:
        await user_mfa.remove(conn, user_id=row["id"])
        epoch = await conn.fetchval(_SQL_BUMP_EPOCH, row["id"], str(row["tenant_id"]))
    reason = _required(row)
    token = await _renew(response, ctx, row, epoch=int(epoch), enrolment_owed=reason is not None)
    log.info("Two-step sign-in turned off (user_id=%s)", row["id"])
    await _audit_mfa("auth.mfa.disabled", tenant_id=row["tenant_id"], actor=str(row["agent_id"]),
                     target_user_id=row["id"], metadata={"required": reason})
    return {"enabled": False, "enrolment_required": reason is not None, "token": token}


# ── resets by someone else ──────────────────────────────────────────────────

_SQL_RESET_TARGET = (
    "SELECT id, agent_id, role, tenant_id FROM users "
    " WHERE id = $1 AND role <> 'platform_admin' AND ($2 OR role = 'agent') "
    "   AND lower(agent_id) <> lower($3)"
)


async def reset_member_mfa(ctx: TenantContext, user_id: UUID, reason: str) -> dict[str, Any]:
    """Remove someone's two-step sign-in so they can set it up again (a lost
    phone, no recovery codes left). A broker owner may reset an agent of
    their own brokerage; a platform admin may reset anyone except another
    platform admin. Their sessions end. Audited.

    The target is found in the caller's own context, so RLS decides which
    brokerage's people a broker owner can reach; the change then runs in the
    platform context, the only one 0150 admits to the factor tables."""
    from auth import _admin_ctx, _audit_mfa
    from db.connection import tenant_tx

    if not (ctx.is_broker_owner or ctx.is_platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role {ctx.role.value} not permitted for this action.")
    async with tenant_tx(ctx) as conn:
        target = await conn.fetchrow(_SQL_RESET_TARGET, user_id, ctx.is_platform_admin, ctx.agent_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No team member you can reset has that id.")
    async with tenant_tx(_admin_ctx()) as conn:
        existed = await user_mfa.remove(conn, user_id=target["id"])
        if not existed:
            raise HTTPException(status.HTTP_409_CONFLICT, "That member has not set up two-step sign-in.")
        await conn.fetchval(_SQL_BUMP_EPOCH, target["id"], str(target["tenant_id"]))
    admin = ctx.is_platform_admin
    await _audit_mfa(
        "admin.user.mfa_reset" if admin else "team.member.mfa_reset",
        tenant_id=target["tenant_id"], actor=ctx.agent_id, target_user_id=target["id"],
        metadata={"target_agent_id": target["agent_id"], "target_role": target["role"], "reason": reason},
        admin=admin,
    )
    log.info("Two-step sign-in reset (user_id=%s) by a %s", target["id"], ctx.role.value)
    return {"id": str(target["id"]), "agent_id": target["agent_id"], "mfa_enabled": False,
            "detail": "Two-step sign-in was reset and their sessions ended. If their role or the "
                      "brokerage requires it, they set it up again at their next sign-in."}
