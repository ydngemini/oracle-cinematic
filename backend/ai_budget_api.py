"""Who sees and changes the AI token budget (prompt 18, T18.07, AC18.05).

    GET/PUT /api/billing/budgets/ai-tokens   broker owner or platform admin
    GET/PUT /api/admin/ai-spend              platform admin (the kill switch)

An agent gets 403 here, and the 0170 trigger refuses an agent's connection
(42501) whatever route it came through. Every change is audited with its old
and new values — numbers and switches only, never the switch's free-text
reason. Enforcement itself lives in ai_budget.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

import ai_budget
import ai_chat_agent
from admin_ops import require_broker_owner_or_admin, require_platform_admin
from audit_ledger import AuditCategory
from audit_middleware import audit_now
from llm_gateway import SpendUnavailable
from tenancy import TenantContext

logger = logging.getLogger("oracle.ai_budget")

router = APIRouter(tags=["Billing"])

# bigint, and far above any month a brokerage could spend.
_MAX_CAP = 10**15

_UNREADABLE = "The AI budget can't be read right now. Nothing was changed; try again in a moment."
#: A write that failed may still have committed (a connection dropped during
#: COMMIT), so its outcome is not known here.
_UNCONFIRMED = "Could not confirm the AI budget change; reload to see the current budget."


class AiTokenBudget(BaseModel):
    """All three values, every time: a PUT that omitted one must not reset it."""

    model_config = ConfigDict(extra="forbid")

    #: null = no cap (not limited).
    monthly_cap_tokens: Optional[int] = Field(..., gt=0, le=_MAX_CAP)
    warn_pct: int = Field(..., ge=1, le=100)
    paused: bool


class PlatformSpendSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    halted: bool
    reason: Optional[str] = Field(default=None, max_length=300)


def _saved_unread(body: AiTokenBudget) -> dict:
    """A committed change whose month of usage could not then be read: the
    values written are in force, and usage is missing evidence, not a zero."""
    return {
        "channel": ai_budget.CHANNEL,
        "unit": "tokens",
        "saved": True,
        "cap": body.monthly_cap_tokens,
        "warn_pct": body.warn_pct,
        "paused": body.paused,
        "used": None,
        "reserved": None,
        "state": "unknown",
        "counts": ai_budget.COUNTS,
        "not_counted": list(ai_budget.NOT_COUNTED),
        "evidence_status": "unavailable",
    }


@router.get("/api/billing/budgets/ai-tokens")
async def get_ai_token_budget(ctx: TenantContext = Depends(require_broker_owner_or_admin)) -> dict:
    try:
        # Judged as the socket judges a turn, against the least any kind of
        # turn needs (ai_chat_agent.smallest_request_hold).
        return await ai_budget.state(ctx, floor=ai_chat_agent.smallest_request_hold())
    except SpendUnavailable:
        # Never "unlimited": an unread budget is not an absent one.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNREADABLE) from None


@router.put("/api/billing/budgets/ai-tokens")
async def put_ai_token_budget(
    body: AiTokenBudget, ctx: TenantContext = Depends(require_broker_owner_or_admin),
) -> dict:
    try:
        before = await ai_budget.settings(ctx)
    except Exception as exc:  # noqa: BLE001 — nothing was written yet
        logger.warning("ai budget: not saved, unreadable (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNREADABLE) from None
    try:
        await ai_budget.set_budget(ctx, cap=body.monthly_cap_tokens, warn_pct=body.warn_pct,
                                   paused=body.paused)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "sqlstate", "") == "42501":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only a broker owner or platform admin can change budgets.") from None
        logger.warning("ai budget: save not confirmed (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNCONFIRMED) from None
    after = body.model_dump()
    try:
        await audit_now(ctx, AuditCategory.ADMIN_ACTION, "ai_token_budget_changed",
                        target_id=ctx.tenant_id, metadata={"before": before, "after": after})
    except Exception:  # noqa: BLE001 — the change is committed; AuditMiddleware also logs the PUT
        logger.warning("ai budget: audit entry not written")
    try:
        return {**await ai_budget.state(ctx, floor=ai_chat_agent.smallest_request_hold()), "saved": True}
    except SpendUnavailable:
        # Committed: never "Nothing was changed" (the GET's 503) from here.
        return _saved_unread(body)


@router.get("/api/admin/ai-spend")
async def get_platform_ai_spend(ctx: TenantContext = Depends(require_platform_admin)) -> dict:
    try:
        return await ai_budget.platform_control(ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai spend switch: unreadable (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The platform AI switch can't be read right now.") from None


@router.put("/api/admin/ai-spend")
async def put_platform_ai_spend(
    body: PlatformSpendSwitch, ctx: TenantContext = Depends(require_platform_admin),
) -> dict:
    try:
        before = (await ai_budget.platform_control(ctx))["halted"]
        changed = await ai_budget.set_platform_halt(ctx, halted=body.halted, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai spend switch: not saved (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The platform AI switch can't be changed right now.") from None
    if not changed:
        # RLS let nothing through: this connection is not the platform login.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The platform AI switch can only be changed from the platform connection.")
    try:
        await audit_now(ctx, AuditCategory.ADMIN_ACTION, "platform_ai_spend_switched",
                        target_id=ai_budget.CHANNEL,
                        metadata={"halted_before": before, "halted_after": body.halted,
                                  "reason_given": bool(body.reason)})
    except Exception:  # noqa: BLE001
        logger.warning("ai spend switch: audit entry not written")
    return await get_platform_ai_spend(ctx)
