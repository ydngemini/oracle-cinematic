"""One-time NEOH platform-policy acceptance for newly registered users."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from policy_contract import (
    ACCOUNT_SECURITY_ESA_DOCUMENT,
    ACCOUNT_SECURITY_ESA_VERSION,
    PLATFORM_POLICY_DOCUMENT,
    PLATFORM_POLICY_VERSION,
)
from tenancy import TenantContext, require_policy_context


router = APIRouter(prefix="/auth", tags=["auth"])


class PolicyAcceptanceRequest(BaseModel):
    policy_version: str = Field(min_length=1, max_length=96)
    account_security_version: str = Field(min_length=1, max_length=96)


class AccountSecurityAcceptanceRequest(BaseModel):
    agreement_version: str = Field(min_length=1, max_length=96)


class PolicySection(BaseModel):
    heading: str
    paragraphs: list[str]


class PolicyDocument(BaseModel):
    title: str
    operator: str
    effective_date: str
    introduction: str
    sections: list[PolicySection]


class PolicyAcceptanceStatus(BaseModel):
    required: bool
    policy_version: str
    accepted_at: Optional[datetime] = None
    account_security_required: bool
    account_security_version: str
    account_security_accepted_at: Optional[datetime] = None
    policy: PolicyDocument
    token: Optional[str] = None


class AccountSecurityAgreement(BaseModel):
    title: str
    operator: str
    effective_date: str
    introduction: str
    sections: list[PolicySection]


class AccountSecurityAgreementStatus(BaseModel):
    agreement_version: str
    agreement: AccountSecurityAgreement


class AccountSecurityAcceptanceResult(BaseModel):
    accepted: bool
    required: bool
    agreement_version: str
    accepted_at: datetime


class PolicyAcceptanceResult(PolicyAcceptanceStatus):
    accepted: bool
    token: Optional[str] = None
    agent_id: str
    tenant_id: str
    role: str


async def _current_user(conn, ctx: TenantContext):
    return await conn.fetchrow(
        "SELECT id, policy_acceptance_required FROM users "
        "WHERE tenant_id = $1 AND lower(agent_id) = lower($2) AND is_active",
        ctx.tenant_id,
        ctx.agent_id,
    )


async def _acceptance_row(conn, user_id: str):
    return await conn.fetchrow(
        "SELECT policy_version, accepted_at FROM user_policy_acceptances "
        "WHERE user_id = $1 AND policy_version = $2",
        user_id,
        PLATFORM_POLICY_VERSION,
    )


def _normalise_agent_id(agent_id: str) -> str:
    return agent_id.strip().lower()


async def _security_acceptance_row(conn, ctx: TenantContext):
    return await conn.fetchrow(
        "SELECT agreement_version, accepted_at FROM account_security_acceptances "
        "WHERE tenant_id = $1 AND agent_id_normalized = $2 AND agreement_version = $3",
        ctx.tenant_id,
        _normalise_agent_id(ctx.agent_id),
        ACCOUNT_SECURITY_ESA_VERSION,
    )


async def _record_security_acceptance(conn, ctx: TenantContext):
    return await conn.fetchrow(
        "INSERT INTO account_security_acceptances "
        "(tenant_id, agent_id_normalized, agreement_version) VALUES ($1, $2, $3) "
        "ON CONFLICT (tenant_id, agent_id_normalized, agreement_version) DO UPDATE "
        "SET accepted_at = now() RETURNING agreement_version, accepted_at",
        ctx.tenant_id,
        _normalise_agent_id(ctx.agent_id),
        ACCOUNT_SECURITY_ESA_VERSION,
    )


@router.get("/policy-acceptance", response_model=PolicyAcceptanceStatus)
async def policy_acceptance_status(
    ctx: TenantContext = Depends(require_policy_context),
    response: Response = None,
) -> PolicyAcceptanceStatus:
    """Return the authoritative policy and whether the caller must acknowledge it."""
    from auth import _browser_token, _issue_jwt, _set_session_cookie
    from db.connection import tenant_tx

    response = response or Response()
    async with tenant_tx(ctx) as conn:
        user = await _current_user(conn, ctx)
        security_acceptance = await _security_acceptance_row(conn, ctx)
        if not user:
            # Environment-managed/demo identities are not self-serve accounts.
            token = _issue_jwt(ctx.agent_id, ctx.tenant_id, ctx.role.value)
            _set_session_cookie(response, token)
            return PolicyAcceptanceStatus(
                required=False,
                policy_version=PLATFORM_POLICY_VERSION,
                account_security_required=security_acceptance is None,
                account_security_version=ACCOUNT_SECURITY_ESA_VERSION,
                account_security_accepted_at=(
                    security_acceptance["accepted_at"] if security_acceptance else None
                ),
                policy=PLATFORM_POLICY_DOCUMENT,
                token=_browser_token(token),
            )
        acceptance = await _acceptance_row(conn, user["id"])
        required = bool(user["policy_acceptance_required"]) or acceptance is None
        token = None if required else _issue_jwt(ctx.agent_id, ctx.tenant_id, ctx.role.value)
        if token:
            _set_session_cookie(response, token)
        return PolicyAcceptanceStatus(
            required=required,
            policy_version=PLATFORM_POLICY_VERSION,
            accepted_at=acceptance["accepted_at"] if acceptance else None,
            account_security_required=security_acceptance is None,
            account_security_version=ACCOUNT_SECURITY_ESA_VERSION,
            account_security_accepted_at=(
                security_acceptance["accepted_at"] if security_acceptance else None
            ),
            policy=PLATFORM_POLICY_DOCUMENT,
            token=_browser_token(token) if token else None,
        )


@router.post("/policy-acceptance", response_model=PolicyAcceptanceResult)
async def accept_policy(
    body: PolicyAcceptanceRequest,
    ctx: TenantContext = Depends(require_policy_context),
    response: Response = None,
) -> PolicyAcceptanceResult:
    """Persist acknowledgement and exchange a pending signup JWT for a normal one."""
    if body.policy_version != PLATFORM_POLICY_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This policy version is no longer current. Refresh and review it again.",
        )
    if body.account_security_version != ACCOUNT_SECURITY_ESA_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This account-security agreement is no longer current. Refresh and review it again.",
        )

    from auth import _browser_token, _issue_jwt, _set_session_cookie
    from db.connection import tenant_tx

    response = response or Response()

    async with tenant_tx(ctx) as conn:
        user = await _current_user(conn, ctx)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")
        acceptance = await conn.fetchrow(
            "INSERT INTO user_policy_acceptances (tenant_id, user_id, policy_version) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, policy_version) DO UPDATE "
            "SET accepted_at = now() "
            "RETURNING policy_version, accepted_at",
            ctx.tenant_id,
            user["id"],
            PLATFORM_POLICY_VERSION,
        )
        security_acceptance = await _record_security_acceptance(conn, ctx)
        await conn.execute(
            "UPDATE users SET policy_acceptance_required = false "
            "WHERE id = $1 AND tenant_id = $2",
            user["id"],
            ctx.tenant_id,
        )

    token = _issue_jwt(ctx.agent_id, ctx.tenant_id, ctx.role.value)
    _set_session_cookie(response, token)
    return PolicyAcceptanceResult(
        accepted=True,
        required=False,
        policy_version=PLATFORM_POLICY_VERSION,
        accepted_at=acceptance["accepted_at"],
        account_security_required=False,
        account_security_version=ACCOUNT_SECURITY_ESA_VERSION,
        account_security_accepted_at=security_acceptance["accepted_at"],
        policy=PLATFORM_POLICY_DOCUMENT,
        token=_browser_token(token),
        agent_id=ctx.agent_id,
        tenant_id=ctx.tenant_id,
        role=ctx.role.value,
    )


@router.get("/account-security-esa", response_model=AccountSecurityAgreementStatus)
async def account_security_esa() -> AccountSecurityAgreementStatus:
    """Return the NEOH account-security addendum used when policy status checks fail."""
    return AccountSecurityAgreementStatus(
        agreement_version=ACCOUNT_SECURITY_ESA_VERSION,
        agreement=ACCOUNT_SECURITY_ESA_DOCUMENT,
    )


@router.post("/account-security-esa", response_model=AccountSecurityAcceptanceResult)
async def accept_account_security_esa(
    body: AccountSecurityAcceptanceRequest,
    ctx: TenantContext = Depends(require_policy_context),
) -> AccountSecurityAcceptanceResult:
    """Persist the current ESA acknowledgement for any authenticated identity."""
    if body.agreement_version != ACCOUNT_SECURITY_ESA_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This account-security agreement is no longer current. Refresh and review it again.",
        )

    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        acceptance = await _record_security_acceptance(conn, ctx)

    return AccountSecurityAcceptanceResult(
        accepted=True,
        required=False,
        agreement_version=acceptance["agreement_version"],
        accepted_at=acceptance["accepted_at"],
    )
