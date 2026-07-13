"""Immutable-payload human approvals for high-risk platform actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from audit_ledger import AuditCategory, ledger
from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from platform_policy import ActionRisk, validate_approval_reason
from tenancy import Role, TenantContext, require_role


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def approval_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["draft_payload"] = _json_value(result.get("draft_payload"))
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif key.endswith("_id") and value is not None:
            result[key] = str(value)
    return result


async def create_approval(
    ctx: TenantContext,
    *,
    action_type: str,
    risk: ActionRisk,
    target_type: str,
    target_id: str,
    draft_payload: Mapping[str, Any],
    expires_in_minutes: int = 24 * 60,
) -> dict[str, Any]:
    if risk is ActionRisk.READ_ONLY:
        raise ValueError("read-only actions do not require an approval")
    expires_in_minutes = max(5, min(7 * 24 * 60, expires_in_minutes))
    draft = dict(draft_payload)
    digest = payload_hash(draft)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO action_approvals (
                tenant_id, action_type, risk_class, target_type, target_id,
                payload_hash, draft_payload, requested_by, expires_at
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)
            RETURNING *
            """,
            ctx.tenant_id,
            action_type[:120],
            risk.value,
            target_type[:120],
            target_id[:240],
            digest,
            canonical_json(draft),
            ctx.agent_id,
            expires_at,
        )
    approval = approval_dict(row)
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="approval_requested",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(approval["id"]),
        metadata={
            "action_type": action_type,
            "risk_class": risk.value,
            "payload_hash": digest,
        },
    )
    return approval


async def decide_approval(
    ctx: TenantContext,
    approval_id: str,
    *,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    reason = validate_approval_reason(reason)
    async with tenant_tx(ctx) as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM action_approvals WHERE id=$1::uuid FOR UPDATE",
            approval_id,
        )
        if existing is None:
            raise LookupError("approval not found")
        if existing["risk_class"] == ActionRisk.ROLE_OVERRIDE.value:
            require_role(ctx, Role.BROKER_OWNER)
            if existing["requested_by"] == ctx.agent_id:
                raise ValueError("role overrides require a different approving broker")
        if existing["status"] != "pending":
            raise ValueError(f"approval is already {existing['status']}")
        if existing["expires_at"] <= datetime.now(timezone.utc):
            row = await conn.fetchrow(
                """
                UPDATE action_approvals
                   SET status='expired', decided_by=$2, decided_at=now(), reason=$3
                 WHERE id=$1::uuid
                RETURNING *
                """,
                approval_id,
                ctx.agent_id,
                reason,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE action_approvals
                   SET status=$2, decided_by=$3, decided_at=now(), reason=$4
                 WHERE id=$1::uuid
                RETURNING *
                """,
                approval_id,
                decision,
                ctx.agent_id,
                reason,
            )
    approval = approval_dict(row)
    await ledger.record(
        category=AuditCategory.ADMIN_ACTION
        if existing["risk_class"] == ActionRisk.ROLE_OVERRIDE.value
        else AuditCategory.USER_STATE_CHANGE,
        action=f"approval_{approval['status']}",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=approval_id,
        metadata={"reason": reason, "risk_class": existing["risk_class"]},
    )
    return approval


async def list_approvals(
    ctx: TenantContext,
    *,
    status_filter: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(200, limit))
    async with tenant_tx(ctx) as conn:
        if status_filter:
            rows = await conn.fetch(
                """
                SELECT * FROM action_approvals
                WHERE status=$1 ORDER BY requested_at DESC LIMIT $2
                """,
                status_filter,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM action_approvals ORDER BY requested_at DESC LIMIT $1",
                limit,
            )
    return [approval_dict(row) for row in rows]
