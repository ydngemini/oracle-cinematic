"""Immutable-payload human approvals for high-risk platform actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from audit_ledger import AuditCategory, ledger
from automation_jobs import canonical_json, payload_hash
from db.connection import tenant_tx
from decision_traces import SURFACE_APPROVAL, record_decision
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


def _same_agent(left: Optional[str], right: Optional[str]) -> bool:
    """Whether two agent_ids denote one account.

    Login resolves users with lower(agent_id), so spelling is not identity.
    """
    return (left or "").strip().lower() == (right or "").strip().lower()


async def decide_approval(
    ctx: TenantContext,
    approval_id: str,
    *,
    decision: str,
    reason: str,
    edited_payload: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Decide a pending approval, optionally recording what the human changed.

    `edited_payload` does NOT alter what gets executed — the approved action
    still uses the immutable `draft_payload`, which is the whole point of the
    payload hash. It exists so an approve-with-corrections carries its
    correction into the training corpus, where the (draft, corrected) pair is
    the single most valuable signal the platform produces. An edit that hashes
    to the draft is recorded as a plain acceptance.
    """
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
            # Case-folded deliberately. agent_id is matched with lower() at
            # login, so one account has many spellings; comparing them exactly
            # here let a single broker request under one spelling and approve
            # under another, which is the whole control defeated. The JWT now
            # carries the canonical spelling too — this is the second lock, not
            # the only one, because it is the line the guarantee lives on.
            if _same_agent(existing["requested_by"], ctx.agent_id):
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

    # Capture the judgement as training signal. This runs after the decision is
    # committed and audited, and cannot fail it — see decision_traces. The
    # latency is measured from the request, and is a weak confidence proxy only.
    # Read the raw row, not `approval`: approval_dict() isoformats every
    # datetime for the wire, and a string here would silently drop the latency
    # and insert a text value into a timestamptz column.
    decided_at = row["decided_at"] or datetime.now(timezone.utc)
    latency_ms: Optional[int] = None
    requested_at = existing["requested_at"]
    if isinstance(decided_at, datetime) and isinstance(requested_at, datetime):
        latency_ms = max(0, int((decided_at - requested_at).total_seconds() * 1000))
    await record_decision(
        ctx,
        surface=SURFACE_APPROVAL,
        action_type=existing["action_type"],
        risk_class=existing["risk_class"],
        source_table="action_approvals",
        source_id=approval_id,
        proposal=_json_value(existing["draft_payload"]) or {},
        final=edited_payload,
        # The row's own status, not the requested decision: an approval that had
        # already expired is written as 'expired' above, and recording it as
        # 'approved' would put a decision in the corpus that never happened.
        decision=approval["status"],
        decided_at=decided_at,
        decision_latency_ms=latency_ms,
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
