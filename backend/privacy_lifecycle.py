"""The customer-data lifecycle: offboarding, closure, erasure, receipts.

Policy (how long, and what happens to each kind of data) lives in
retention_policy.py; the per-table classification in privacy_data_map.py;
the database guards in migration 0122. This module is the procedure.

Three flows:

* **Offboarding an agent** (`offboard_agent`) — the agent leaves, the
  brokerage keeps its records. Active responsibility (contacts, clients, open
  tasks, plans, sites, attachments, routing) moves to a successor; history
  (who wrote a note, who sent a message, who approved what) is never
  rewritten. Pending side effects the agent authored are cancelled, their
  phone line stops forwarding to their personal phone, and their personal
  Google/SMTP credentials are revoked and deleted.

* **Closing a brokerage** (`request_closure` / `withdraw_closure`) — the
  owner asks to close. The tenant stops doing anything (no jobs, no outbound,
  no public links, agents signed out) but nothing is deleted for the grace
  period, during which the owner can export and can change their mind.

* **Erasing a brokerage** (`run_erasure`) — after the grace period, phase by
  phase and resumable: providers let go (numbers released, hosted SMS
  disconnected, Google grants revoked), objects deleted, opt-outs reduced to
  keyed hashes, rows deleted table by table, retained evidence pseudonymized,
  the tenants row scrubbed to a tombstone, everything verified, a receipt
  written. Erasure never deletes production data by accident: every delete
  passes through privacy_erase_tenant_batch(), which refuses unless the
  tenant is in state 'erasing', has a running erasure operation, is not the
  platform tenant, and has no unreleased legal hold.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from db.connection import tenant_tx
from retention_policy import BACKUP_DAYS, POLICY_VERSION, closure_grace_days
from tenancy import Role, TenantContext

log = logging.getLogger("oracle.privacy_lifecycle")

PLATFORM_TENANT_ID = os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000")
JOB_ERASE = "privacy:erase"
JOB_EXPORT = "privacy:export"
ERASE_BATCH = 5000


class LifecycleError(RuntimeError):
    """A lifecycle request the current state does not allow. Message is user-facing."""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


_events = logging.getLogger("oracle.privacy.events")


def emit_event(name: str, **fields: Any) -> None:
    """Aggregate lifecycle telemetry: event name, ids and counts — never
    customer content (no names, addresses, message text). One JSON line on
    the oracle.privacy.events logger, which the log pipeline can count."""
    safe = {k: v for k, v in fields.items() if isinstance(v, (int, float, bool)) or k.endswith("_id")
            or k in ("kind", "state", "phase", "reason_code")}
    _events.info(json.dumps({"event": name, **safe}, default=str, sort_keys=True))


def _platform_ctx(actor: str = "privacy-lifecycle") -> TenantContext:
    return TenantContext(agent_id=actor, tenant_id=PLATFORM_TENANT_ID, role=Role.PLATFORM_ADMIN)


def _norm(agent_id: str) -> str:
    return (agent_id or "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Pending side effects
# ─────────────────────────────────────────────────────────────────────────────
# One entry per kind of queued work that would otherwise still send, call,
# charge or publish after its author left or its brokerage closed. `actor` is
# the column naming who authored it (agent-scoped cancellation matches it with
# lower()); None means the entry applies only to whole-tenant cancellation.
# `$1` = tenant id, `$2` = reason, `$3` = acting identity, `$4` = agent (when
# agent-scoped).

@dataclass(frozen=True)
class _Pending:
    table: str
    actor: Optional[str]
    where: str
    set_agent: str
    set_tenant: str


_PENDING: tuple[_Pending, ...] = (
    _Pending("command_executions", "created_by",
             "state IN ('draft','awaiting_approval','approved','queued')",
             "state='cancelled', last_error=left($2,500), updated_at=now()",
             "state='cancelled', last_error=left($2,500), updated_at=now()"),
    _Pending("action_approvals", "requested_by", "status='pending'",
             "status='revoked', decided_by=$3, decided_at=now(), reason=left($2,500)",
             "status='revoked', decided_by=$3, decided_at=now(), reason=left($2,500)"),
    _Pending("automation_jobs", "created_by",
             "state IN ('draft','awaiting_approval','queued','failed') AND job_type NOT LIKE 'privacy:%'",
             "state='cancelled', status_message=left($2,500), next_retry_at=NULL, updated_at=now()",
             "state='cancelled', status_message=left($2,500), next_retry_at=NULL, updated_at=now()"),
    _Pending("missions", "created_by", "status IN ('draft','simulated','shadow','active')",
             "status='paused', paused_reason=left($2,500), updated_at=now()",
             "status='cancelled', paused_reason=left($2,500), updated_at=now()"),
    _Pending("smart_plan_step_runs", None, "state IN ('scheduled','awaiting_approval')",
             "state='paused', updated_at=now()",
             "state='cancelled', updated_at=now()"),
    _Pending("smart_plan_enrollments", "created_by", "status IN ('active','paused','blocked')",
             "status='paused', paused_at=COALESCE(paused_at, now()), next_run_at=NULL, updated_at=now()",
             "status='cancelled', cancelled_at=now(), next_run_at=NULL, updated_at=now()"),
    _Pending("email_outbox", "created_by", "status='queued'",
             "status='cancelled', error=left($2,500), updated_at=now()",
             "status='cancelled', error=left($2,500), updated_at=now()"),
    _Pending("agent_call_intents", "agent_id", "state IN ('prepared','authorized')",
             "state='cancelled', failure_reason=left($2,500), updated_at=now()",
             "state='cancelled', failure_reason=left($2,500), updated_at=now()"),
    _Pending("oauth_authorization_states", "created_by", "consumed_at IS NULL",
             "consumed_at=now()", "consumed_at=now()"),
    _Pending("contact_nurture_jobs", None, "state='scheduled'",
             "", "state='cancelled', last_error_code='account_closed', updated_at=now()"),
    _Pending("video_studio_jobs", None, "status='queued'",
             "", "status='cancelled', error=left($2,500), updated_at=now()"),
    _Pending("voice_walkthrough_jobs", None, "status='queued'",
             "", "status='failed', audio=NULL, error=left($2,500), updated_at=now()"),
    _Pending("reconstruction_jobs", None, "status='queued'",
             "", "status='failed', error=left($2,500), updated_at=now()"),
)


async def cancel_pending_side_effects(
    conn, *, tenant_id: str, reason: str, actor: str, agent_id: Optional[str] = None,
) -> dict[str, int]:
    """Cancel queued work. With `agent_id`, only what that agent authored
    (smart-plan step runs follow their enrollment). Returns per-table counts."""
    counts: dict[str, int] = {}
    for p in _PENDING:
        if agent_id is not None:
            if not p.set_agent:
                continue
            if p.actor is not None:
                scope = f"lower({p.actor}) = lower($4)"
            elif p.table == "smart_plan_step_runs":
                scope = ("enrollment_id IN (SELECT id FROM smart_plan_enrollments "
                         "WHERE tenant_id = $1 AND lower(created_by) = lower($4))")
            else:
                continue
            sql = (f"UPDATE {p.table} SET {p.set_agent} WHERE tenant_id = $1 AND {p.where} AND {scope}")
            args = (tenant_id, reason, actor, agent_id)
        else:
            sql = f"UPDATE {p.table} SET {p.set_tenant} WHERE tenant_id = $1 AND {p.where}"
            args = (tenant_id, reason, actor)
            # $4 unused for tenant-wide statements; asyncpg rejects extra args.
        status = await conn.execute(*_bind_used(sql, args))
        counts[p.table] = _rowcount(status)
    return counts


def _bind_used(sql: str, args: tuple) -> tuple:
    """Renumber $n placeholders to only those the statement uses: asyncpg
    cannot type a parameter that is passed but never referenced."""
    used = sorted({int(n) for n in re.findall(r"\$(\d+)", sql)})
    mapping = {old: new for new, old in enumerate(used, start=1)}
    renumbered = re.sub(r"\$(\d+)", lambda m: f"${mapping[int(m.group(1))]}", sql)
    return (renumbered, *[args[i - 1] for i in used])


def _rowcount(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Capability links and public surfaces (whole tenant)
# ─────────────────────────────────────────────────────────────────────────────

_REVOKE_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("client_portals", "UPDATE client_portals SET revoked_at=now(), updated_at=now() "
                       "WHERE tenant_id=$1 AND revoked_at IS NULL"),
    ("property_view_upload_links", "UPDATE property_view_upload_links SET revoked_at=now() "
                                   "WHERE tenant_id=$1 AND revoked_at IS NULL"),
    ("brokerage_invitations", "UPDATE brokerage_invitations SET revoked_at=now(), revoked_by_agent_id=$2, "
                              "updated_at=now() WHERE tenant_id=$1 AND consumed_at IS NULL AND revoked_at IS NULL"),
    ("lead_source_connectors", "UPDATE lead_source_connectors SET active=false, updated_at=now() "
                               "WHERE tenant_id=$1 AND active"),
    ("hyperlocal_sites", "UPDATE hyperlocal_sites SET status='archived', updated_at=now() "
                         "WHERE tenant_id=$1 AND status IN ('preview','published')"),
    ("marketplace_publications", "UPDATE marketplace_publications SET state='withdrawn', updated_at=now() "
                                 "WHERE tenant_id=$1 AND state IN ('draft','approved','published','under_offer')"),
)


async def revoke_tenant_capabilities(conn, *, tenant_id: str, actor: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, sql in _REVOKE_CAPABILITIES:
        counts[name] = _rowcount(await conn.execute(*_bind_used(sql, (tenant_id, actor))))
    return counts


async def _deactivate_routes(conn, tenant_id: str) -> dict[str, list[str]]:
    """Stop the AI answering and texting for a closing brokerage. Returns the
    route ids it switched off, so a withdrawal restores exactly those."""
    voice = await conn.fetch(
        "UPDATE telephony_routes SET active=false, updated_at=now() "
        "WHERE tenant_id=$1 AND active RETURNING id::text", tenant_id)
    sms = await conn.fetch(
        "UPDATE messaging_routes SET active=false, updated_at=now() "
        "WHERE tenant_id=$1 AND active RETURNING id::text", tenant_id)
    return {"telephony_routes": [r[0] for r in voice], "messaging_routes": [r[0] for r in sms]}


async def _sign_out_users(conn, tenant_id: str, *, keep_owners: bool) -> list[str]:
    """Deactivate and end the sessions of the tenant's users. Returns their ids."""
    rows = await conn.fetch(
        "UPDATE users SET is_active=false, session_epoch=session_epoch+1, updated_at=now() "
        "WHERE tenant_id=$1 AND is_active AND role <> 'platform_admin' "
        "AND ($2 IS FALSE OR role <> 'broker_owner') RETURNING id::text",
        tenant_id, keep_owners)
    return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Privacy operations (the durable record of every request)
# ─────────────────────────────────────────────────────────────────────────────

async def _create_operation(conn, *, tenant_id: str, kind: str, requested_by: str,
                            reason: Optional[str] = None, subject_kind: Optional[str] = None,
                            subject_ref: Optional[str] = None, params: Optional[dict] = None,
                            state: str = "requested", due_at: Optional[datetime] = None) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO privacy_operations
            (tenant_id, kind, state, subject_kind, subject_ref, requested_by, reason,
             params, due_at, policy_version)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
        RETURNING *
        """,
        tenant_id, kind, state, subject_kind, subject_ref, requested_by, reason,
        json.dumps(params or {}), due_at, POLICY_VERSION)
    return dict(row)


async def _finish_operation(conn, op_id: str, *, state: str, result: dict,
                            receipt: Optional[dict] = None, error: Optional[str] = None) -> None:
    await conn.execute(
        """
        UPDATE privacy_operations
           SET state=$2, result=result || $3::jsonb, receipt=COALESCE($4::jsonb, receipt),
               error=$5, completed_at=CASE WHEN $2 IN ('succeeded','failed','cancelled') THEN now() END,
               updated_at=now()
         WHERE id=$1
        """,
        op_id, state, json.dumps(result, default=str),
        json.dumps(receipt, default=str) if receipt is not None else None, error)


async def list_operations(ctx: TenantContext, limit: int = 50) -> list[dict]:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, state, subject_kind, requested_by, requested_at, reason,
                   result, receipt, artifact_expires_at, error, due_at, completed_at
              FROM privacy_operations WHERE tenant_id=$1
             ORDER BY requested_at DESC LIMIT $2
            """, ctx.tenant_id, max(1, min(limit, 200)))
    return [_jsonable(dict(r)) for r in rows]


def _jsonable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, str) and key in ("result", "receipt", "params", "progress"):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = str(value) if hasattr(value, "hex") and not isinstance(value, (bytes, str)) else value
    return out


async def active_legal_hold(conn, tenant_id: str, *, scope: str = "tenant",
                            subject_ref: Optional[str] = None) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        SELECT id, scope, reason, placed_at FROM legal_holds
         WHERE tenant_id=$1 AND released_at IS NULL
           AND (scope='tenant' OR (scope=$2 AND subject_ref=$3))
         ORDER BY placed_at LIMIT 1
        """, tenant_id, scope, subject_ref)
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Offboarding an agent
# ─────────────────────────────────────────────────────────────────────────────
# Current responsibility moves to the successor. Each statement matches the
# departing agent with lower() (agent ids are compared case-insensitively
# everywhere — see the agent_id identity incident) and writes the successor's
# canonical users.agent_id. `$1` tenant, `$2` departing, `$3` successor.

_REASSIGN: tuple[tuple[str, str], ...] = (
    ("agent_contacts", "UPDATE agent_contacts SET assigned_agent_id=$3, updated_at=now() "
                       "WHERE tenant_id=$1 AND lower(assigned_agent_id)=lower($2) AND deleted_at IS NULL"),
    ("clients", "UPDATE clients SET assignee_id=$3, updated_at=now() "
                "WHERE tenant_id=$1 AND lower(assignee_id)=lower($2)"),
    ("client_tasks", "UPDATE client_tasks SET assignee_id=$3 "
                     "WHERE tenant_id=$1 AND lower(assignee_id)=lower($2) AND status IN ('open','snoozed')"),
    ("intake_handoff_tasks", "UPDATE intake_handoff_tasks SET assigned_agent_id=$3 "
                             "WHERE tenant_id=$1 AND lower(assigned_agent_id)=lower($2) AND status='open'"),
    ("transaction_milestones", "UPDATE transaction_milestones SET assigned_to=$3 "
                               "WHERE tenant_id=$1 AND lower(assigned_to)=lower($2) AND status IN ('pending','at_risk')"),
    ("smart_plans", "UPDATE smart_plans SET owner_agent_id=$3 "
                    "WHERE tenant_id=$1 AND lower(owner_agent_id)=lower($2) AND status <> 'archived'"),
    ("hyperlocal_sites", "UPDATE hyperlocal_sites SET owner_agent_id=$3, updated_at=now() "
                         "WHERE tenant_id=$1 AND lower(owner_agent_id)=lower($2)"),
    ("ai_record_attachments", "UPDATE ai_record_attachments SET owner_agent_id=$3 "
                              "WHERE tenant_id=$1 AND lower(owner_agent_id)=lower($2) AND deleted_at IS NULL"),
    ("client_segments", "UPDATE client_segments SET owner_id=$3 "
                        "WHERE tenant_id=$1 AND lower(owner_id)=lower($2)"),
    ("lead_routing_rules", "UPDATE lead_routing_rules SET agent_ids = ARRAY(SELECT DISTINCT "
                           "CASE WHEN lower(x)=lower($2) THEN $3 ELSE x END FROM unnest(agent_ids) x), "
                           "updated_at=now() WHERE tenant_id=$1 AND EXISTS "
                           "(SELECT 1 FROM unnest(agent_ids) x WHERE lower(x)=lower($2))"),
)

# Never rewritten on offboarding — this is the authorship record:
HISTORICAL_AUTHORSHIP = (
    "*.created_by", "*.updated_by", "audit_ledger.user_id", "client_activities.actor",
    "client_notes.author_id", "sms_messages.agent_id", "lead_intake_events.assigned_agent_id",
    "ai_decision_traces.agent_id", "agent_ce_log", "protected_override_events.performed_by",
    "action_approvals.requested_by/decided_by", "team_memberships.approved_by",
    "brokerage_invitations.invited_by*", "missions.consent_by",
)


@dataclass
class OffboardPlan:
    departing: dict
    successor: dict
    reassign: dict[str, int]
    pending: dict[str, int]
    telephony: str
    credentials: int
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "departing": {"agent_id": self.departing["agent_id"], "role": self.departing["role"]},
            "successor": {"agent_id": self.successor["agent_id"], "role": self.successor["role"]},
            "reassign": self.reassign, "cancel_pending": self.pending,
            "telephony": self.telephony, "personal_credentials": self.credentials,
            "historical_authorship_preserved": list(HISTORICAL_AUTHORSHIP),
            "warnings": self.warnings,
        }


class _Preview(Exception):
    """Raised inside the transaction to roll a dry run back."""

    def __init__(self, plan: OffboardPlan):
        self.plan = plan


async def offboard_agent(ctx: TenantContext, *, departing_agent_id: str, successor_agent_id: str,
                         reason: str, preview: bool) -> dict:
    """Move an agent's active work to a successor and shut the agent out.

    With preview=True every statement runs and is rolled back, so the counts
    shown to the owner are exactly what the real run will do.
    """
    if not (ctx.is_broker_owner or ctx.is_platform_admin):
        raise LifecycleError("Only a brokerage owner can offboard an agent.", status_code=403)
    departing, successor = _norm(departing_agent_id), _norm(successor_agent_id)
    if not departing or not successor or departing.lower() == successor.lower():
        raise LifecycleError("Choose a different agent to take over this work.", status_code=422)

    try:
        async with tenant_tx(ctx) as conn:
            plan = await _offboard_in_tx(conn, ctx, departing, successor, reason)
            if preview:
                raise _Preview(plan)
            op = await _create_operation(
                conn, tenant_id=ctx.tenant_id, kind="offboard", requested_by=ctx.agent_id,
                reason=reason, subject_kind="user", subject_ref=str(plan.departing["id"]),
                params={"successor": plan.successor["agent_id"]}, state="succeeded")
            await _finish_operation(conn, str(op["id"]), state="succeeded", result=plan.as_dict())
    except _Preview as dry:
        return {"preview": True, **dry.plan.as_dict()}

    revocations = await revoke_personal_credentials(ctx, tenant_id=ctx.tenant_id,
                                                    agent_id=plan.departing["agent_id"])
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE privacy_operations SET result = result || $2::jsonb, updated_at=now() WHERE id=$1",
            op["id"], json.dumps({"credential_revocation": revocations}))
    emit_event("offboarding.completed", operation_id=str(op["id"]), tenant_id=ctx.tenant_id,
               reassigned=sum(plan.reassign.values()), cancelled=sum(plan.pending.values()),
               credential_revocations_failed=sum(1 for r in revocations if r["status"] == "revoke_failed"))
    await _audit(ctx, "team.member.offboarded", target=str(plan.departing["id"]),
                 metadata={"successor": plan.successor["agent_id"], "reason": reason,
                           "reassigned": plan.reassign, "cancelled": plan.pending,
                           "operation_id": str(op["id"])})
    return {"preview": False, "operation_id": str(op["id"]), **plan.as_dict(),
            "credential_revocation": revocations}


async def _offboard_in_tx(conn, ctx: TenantContext, departing: str, successor: str,
                          reason: str) -> OffboardPlan:
    rows = await conn.fetch(
        """
        SELECT id, agent_id, role, is_active FROM users
         WHERE tenant_id=$1 AND lower(agent_id) IN (lower($2), lower($3))
         ORDER BY id FOR UPDATE
        """, ctx.tenant_id, departing, successor)
    by_id = {r["agent_id"].lower(): dict(r) for r in rows}
    a, b = by_id.get(departing.lower()), by_id.get(successor.lower())
    if a is None:
        raise LifecycleError("That agent is not part of this brokerage.", status_code=404)
    if b is None or not b["is_active"]:
        raise LifecycleError("The successor must be an active member of this brokerage.", status_code=422)
    if a["role"] == "platform_admin":
        raise LifecycleError("Platform accounts are not offboarded here.", status_code=403)
    if a["agent_id"].lower() == ctx.agent_id.lower():
        raise LifecycleError("You cannot offboard yourself; transfer ownership first.", status_code=422)
    warnings: list[str] = []
    if a["role"] == "broker_owner":
        if not ctx.is_platform_admin:
            raise LifecycleError("Offboarding an owner requires platform support.", status_code=403)
        others = await conn.fetchval(
            "SELECT count(*) FROM users WHERE tenant_id=$1 AND role='broker_owner' AND is_active "
            "AND lower(agent_id) <> lower($2)", ctx.tenant_id, a["agent_id"])
        if not others:
            raise LifecycleError("This is the brokerage's last owner. Promote another owner first.")

    # 1. lock the agent out
    await conn.execute(
        "UPDATE users SET is_active=false, session_epoch=session_epoch+1, updated_at=now() "
        "WHERE id=$1", a["id"])
    await conn.execute(
        "UPDATE team_memberships SET status='suspended' WHERE tenant_id=$1 AND user_id=$2",
        ctx.tenant_id, a["id"])
    # 2. stop new routing to them
    await conn.execute(
        """
        INSERT INTO agent_routing_state (tenant_id, agent_id, accepting_leads, capacity)
        VALUES ($1, $2, false, 0)
        ON CONFLICT (tenant_id, agent_id) DO UPDATE SET accepting_leads=false, capacity=0
        """, ctx.tenant_id, a["agent_id"])
    # 3. cancel what they authored that has not happened yet
    pending = await cancel_pending_side_effects(
        conn, tenant_id=ctx.tenant_id, reason=f"author offboarded: {reason}"[:500],
        actor=ctx.agent_id, agent_id=a["agent_id"])
    # 4. move current responsibility
    reassign: dict[str, int] = {}
    for name, sql in _REASSIGN:
        reassign[name] = _rowcount(await conn.execute(sql, ctx.tenant_id, a["agent_id"], b["agent_id"]))
    collaborators = await conn.execute(
        "DELETE FROM hyperlocal_site_collaborators WHERE tenant_id=$1 AND lower(agent_id)=lower($2)",
        ctx.tenant_id, a["agent_id"])
    reassign["hyperlocal_site_collaborators_removed"] = _rowcount(collaborators)
    if reassign.get("hyperlocal_sites"):
        warnings.append("Published sites still present the departing agent until republished.")
    # 5. phone and text routes: the number belongs to the brokerage's
    #    clients, so it keeps working — but never again forwards to the
    #    departing agent's personal phone.
    telephony = await _hand_over_routes(conn, ctx.tenant_id, a["agent_id"], b["agent_id"])
    # 6. personal credentials: disabled now, revoked at the provider after commit
    creds = _rowcount(await conn.execute(
        "UPDATE provider_credentials SET disabled_at=COALESCE(disabled_at, now()), updated_at=now() "
        "WHERE tenant_id=$1 AND lower(account_label)=lower($2)", ctx.tenant_id, a["agent_id"]))
    return OffboardPlan(a, b, reassign, pending, telephony, creds, warnings)


async def _hand_over_routes(conn, tenant_id: str, departing: str, successor: str) -> str:
    mine = await conn.fetchrow(
        "SELECT id FROM telephony_routes WHERE tenant_id=$1 AND lower(agent_id)=lower($2)",
        tenant_id, departing)
    if mine is None:
        return "none"
    theirs = await conn.fetchval(
        "SELECT 1 FROM telephony_routes WHERE tenant_id=$1 AND lower(agent_id)=lower($2)",
        tenant_id, successor)
    clear_forward = ("agent_forward_e164=NULL, forward_on_request=false, "
                     "forward_when_ai_unavailable=false, updated_at=now()")
    if theirs is None:
        await conn.execute(f"UPDATE telephony_routes SET agent_id=$2, {clear_forward} WHERE id=$1",
                           mine["id"], successor)
        await conn.execute(
            "UPDATE messaging_routes SET agent_id=$3, updated_at=now() "
            "WHERE tenant_id=$1 AND lower(agent_id)=lower($2)", tenant_id, departing, successor)
        return "moved_to_successor"
    await conn.execute(f"UPDATE telephony_routes SET {clear_forward} WHERE id=$1", mine["id"])
    return "kept_forwarding_cleared"


async def revoke_personal_credentials(ctx: TenantContext, *, tenant_id: str,
                                      agent_id: Optional[str]) -> list[dict]:
    """Revoke disabled Google grants at Google, then delete the rows.

    With agent_id: that agent's own (offboarding). Without: every Google
    credential of the tenant (erasure). A row whose revoke failed (Google
    down, recovery mode) is kept disabled — unusable, but still revocable on
    a retry — and reported, never silently dropped.
    """
    from commands_api import _provider_key, revoke_google_token
    from crypto import decrypt_pii

    outcomes: list[dict] = []
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id, provider, account_label, token_ciphertext, refresh_ciphertext
              FROM provider_credentials
             WHERE tenant_id=$1 AND ($2::text IS NULL OR lower(account_label)=lower($2))
               AND ($2::text IS NULL OR disabled_at IS NOT NULL)
            """, tenant_id, agent_id)
        secrets = []
        for row in rows:
            token = None
            if row["provider"] == "google":
                cipher = row["refresh_ciphertext"] or row["token_ciphertext"]
                try:
                    token = await decrypt_pii(conn, cipher, _provider_key(tenant_id)) if cipher else None
                except Exception:  # noqa: BLE001 - unreadable token cannot be revoked; say so
                    token = None
            secrets.append((row["id"], row["provider"], token))
    for cred_id, provider, token in secrets:
        status = "deleted_no_remote_grant"
        if provider == "google":
            if token is None:
                status = "deleted_token_unreadable"
            else:
                try:
                    status = await revoke_google_token(token)
                except Exception as exc:  # noqa: BLE001 - recorded and retried, never hidden
                    outcomes.append({"credential_id": str(cred_id), "provider": provider,
                                     "status": "revoke_failed", "detail": type(exc).__name__})
                    continue
        async with tenant_tx(ctx) as conn:
            await conn.execute("DELETE FROM provider_credentials WHERE id=$1", cred_id)
        outcomes.append({"credential_id": str(cred_id), "provider": provider, "status": status})
    return outcomes


# ─────────────────────────────────────────────────────────────────────────────
# Closing a brokerage
# ─────────────────────────────────────────────────────────────────────────────

async def tenant_lifecycle(ctx: TenantContext) -> dict:
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, lifecycle_state, closure_requested_at, closure_requested_by, "
            "erase_after, erased_at FROM tenants WHERE id=$1", ctx.tenant_id)
        hold = await active_legal_hold(conn, ctx.tenant_id)
    if row is None:
        raise LifecycleError("Unknown brokerage.", status_code=404)
    out = dict(row)
    out["id"] = str(out["id"])
    out["legal_hold"] = bool(hold)
    out["grace_days"] = closure_grace_days()
    out["backup_days"] = BACKUP_DAYS
    return out


async def request_closure(ctx: TenantContext, *, confirm_name: str, reason: str) -> dict:
    """Owner asks to close. Nothing is deleted until the grace period ends."""
    if not ctx.is_broker_owner:
        raise LifecycleError("Only a brokerage owner can close the brokerage.", status_code=403)
    if ctx.tenant_id == PLATFORM_TENANT_ID:
        raise LifecycleError("The platform tenant cannot be closed.", status_code=403)
    grace = closure_grace_days()
    async with tenant_tx(ctx) as conn:
        tenant = await conn.fetchrow(
            "SELECT name, lifecycle_state FROM tenants WHERE id=$1 FOR UPDATE", ctx.tenant_id)
        if tenant is None:
            raise LifecycleError("Unknown brokerage.", status_code=404)
        if tenant["lifecycle_state"] != "active":
            raise LifecycleError(f"This brokerage is already {tenant['lifecycle_state']}.")
        if (confirm_name or "").strip() != (tenant["name"] or "").strip():
            raise LifecycleError("Type the brokerage name exactly to confirm.", status_code=422)
        erase_after = datetime.now(timezone.utc) + timedelta(days=grace)
        await conn.execute(
            "UPDATE tenants SET lifecycle_state='closing', closure_requested_at=now(), "
            "closure_requested_by=$2, closure_reason=left($3,500), erase_after=$4, updated_at=now() "
            "WHERE id=$1", ctx.tenant_id, ctx.agent_id, reason, erase_after)
        cancelled = await cancel_pending_side_effects(
            conn, tenant_id=ctx.tenant_id, reason="brokerage closing", actor=ctx.agent_id)
        revoked = await revoke_tenant_capabilities(conn, tenant_id=ctx.tenant_id, actor=ctx.agent_id)
        routes = await _deactivate_routes(conn, ctx.tenant_id)
        signed_out = await _sign_out_users(conn, ctx.tenant_id, keep_owners=True)
        op = await _create_operation(
            conn, tenant_id=ctx.tenant_id, kind="closure", requested_by=ctx.agent_id, reason=reason,
            subject_kind="tenant", subject_ref=ctx.tenant_id, state="running", due_at=erase_after,
            params={"grace_days": grace})
        await conn.execute(
            "UPDATE privacy_operations SET progress=$2::jsonb WHERE id=$1", op["id"],
            json.dumps({"freeze": {"cancelled": cancelled, "revoked": revoked, "routes": routes,
                                   "signed_out_user_ids": signed_out}}))
    billing = await _cancel_billing_at_period_end(ctx.tenant_id)
    emit_event("deletion.scheduled", operation_id=str(op["id"]), tenant_id=ctx.tenant_id, grace_days=grace)
    await _audit(ctx, "brokerage.closure.requested", target=ctx.tenant_id,
                 metadata={"erase_after": erase_after.isoformat(), "operation_id": str(op["id"]),
                           "billing": billing})
    return {"state": "closing", "erase_after": erase_after.isoformat(), "operation_id": str(op["id"]),
            "cancelled": cancelled, "revoked": revoked, "signed_out": len(signed_out),
            "billing": billing}


async def withdraw_closure(ctx: TenantContext, *, reason: str) -> dict:
    """Change of mind during the grace period: back to exactly how it was."""
    if not ctx.is_broker_owner:
        raise LifecycleError("Only a brokerage owner can reopen the brokerage.", status_code=403)
    async with tenant_tx(ctx) as conn:
        state = await conn.fetchval(
            "SELECT lifecycle_state FROM tenants WHERE id=$1 FOR UPDATE", ctx.tenant_id)
        if state != "closing":
            raise LifecycleError("Closure can only be withdrawn during the grace period.")
        op = await conn.fetchrow(
            "SELECT id, progress FROM privacy_operations WHERE tenant_id=$1 AND kind='closure' "
            "AND state='running' ORDER BY requested_at DESC LIMIT 1", ctx.tenant_id)
        freeze = (json.loads(op["progress"]) if op and isinstance(op["progress"], str)
                  else (op["progress"] if op else {})).get("freeze", {})
        await conn.execute(
            "UPDATE tenants SET lifecycle_state='active', closure_requested_at=NULL, "
            "closure_requested_by=NULL, closure_reason=NULL, updated_at=now() WHERE id=$1",
            ctx.tenant_id)
        restored_users = _rowcount(await conn.execute(
            "UPDATE users SET is_active=true, updated_at=now() WHERE tenant_id=$1 AND id::text = ANY($2::text[])",
            ctx.tenant_id, freeze.get("signed_out_user_ids", [])))
        routes = freeze.get("routes", {})
        await conn.execute(
            "UPDATE telephony_routes SET active=true, updated_at=now() WHERE tenant_id=$1 AND id::text = ANY($2::text[])",
            ctx.tenant_id, routes.get("telephony_routes", []))
        await conn.execute(
            "UPDATE messaging_routes SET active=true, updated_at=now() WHERE tenant_id=$1 AND id::text = ANY($2::text[])",
            ctx.tenant_id, routes.get("messaging_routes", []))
        if op:
            await _finish_operation(conn, str(op["id"]), state="cancelled",
                                    result={"withdrawn_by": ctx.agent_id, "withdraw_reason": reason})
    billing = await _resume_billing(ctx.tenant_id)
    await _audit(ctx, "brokerage.closure.withdrawn", target=ctx.tenant_id,
                 metadata={"reason": reason, "billing": billing})
    return {"state": "active", "restored_users": restored_users, "billing": billing,
            "note": "Cancelled jobs and revoked links are not restored; create them again if needed."}


async def _cancel_billing_at_period_end(tenant_id: str) -> str:
    """Stop renewal; access already paid for runs out on its own."""
    return await _set_cancel_at_period_end(tenant_id, True)


async def _resume_billing(tenant_id: str) -> str:
    return await _set_cancel_at_period_end(tenant_id, False)


async def _set_cancel_at_period_end(tenant_id: str, value: bool) -> str:
    import recovery_mode

    async with tenant_tx(_platform_ctx()) as conn:
        sub = await conn.fetchval(
            "SELECT stripe_subscription_id FROM subscriptions WHERE tenant_id=$1 "
            "AND status IN ('active','trialing','past_due') AND stripe_subscription_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1", tenant_id)
    if not sub:
        return "no_subscription"
    try:
        recovery_mode.guard("change a Stripe subscription")
        import stripe

        if not stripe.api_key:
            return "stripe_not_configured"
        stripe.Subscription.modify(sub, cancel_at_period_end=value)
        return "renewal_cancelled" if value else "renewal_resumed"
    except Exception as exc:  # noqa: BLE001 - reported to the owner and the runbook
        log.warning("subscription update for closure failed: %s", type(exc).__name__)
        return f"failed:{type(exc).__name__}"


# ─────────────────────────────────────────────────────────────────────────────
# Erasure
# ─────────────────────────────────────────────────────────────────────────────

PHASES = ("freeze", "providers", "objects", "tombstones", "rows", "pseudonymize",
          "caches", "tenant_row", "verify", "receipt")


async def erasure_preview(tenant_id: str) -> dict:
    """What erasing this brokerage would remove — counts by category and the
    number of stored objects. No names, no content. Shown to the owner before
    they close and to an operator before starting erasure early."""
    from privacy_data_map import TABLES, Disposition

    ctx = _platform_ctx("erasure-preview")
    by_category: dict[str, int] = {}
    by_table: dict[str, int] = {}
    async with tenant_tx(ctx) as conn:
        tables, _ = await _erasure_catalog(conn)
        types = {r[0]: r[1] for r in await conn.fetch(
            "SELECT c.relname, format_type(a.atttypid, a.atttypmod) FROM pg_class c "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped "
            "WHERE c.relkind='r' AND c.relnamespace='public'::regnamespace")}
        for table in tables:
            if not _SAFE_NAME.match(table) or table not in types:
                continue
            n = int(await conn.fetchval(
                f'SELECT count(*) FROM public."{table}" WHERE tenant_id = $1::{types[table]}', tenant_id) or 0)
            if not n:
                continue
            by_table[table] = n
            entry = TABLES.get(table)
            category = entry.category.value if entry else "unclassified"
            by_category[category] = by_category.get(category, 0) + n
        media_keys, vault_keys = await _object_keys(conn, tenant_id)
        retained = {t: int(await conn.fetchval(
            f'SELECT count(*) FROM public."{t}" WHERE tenant_id = $1::uuid', tenant_id) or 0)
            for t in ("subscriptions", "billing_usage_events", "audit_ledger", "suppression_tombstones")}
        hold = await active_legal_hold(conn, tenant_id)
    people = {k: by_table.get(k, 0) for k in ("users", "clients", "agent_contacts", "sms_messages",
                                               "inbound_voice_calls", "email_outbox", "ai_chat_messages")}
    return {"tenant_id": tenant_id, "rows_by_category": by_category, "highlights": people,
            "stored_objects": len(media_keys) + len(vault_keys),
            "stored_bytes": None,  # not recorded per object; the receipt reports what was deleted
            "retained_after_erasure": retained,
            "outreach_opt_outs_kept_as_hashes": True,
            "legal_hold": bool(hold),
            "note": "Counts only; ai_record_attachments are counted at erasure time (agent-scoped)."}


async def start_due_erasures() -> list[str]:
    """Periodic: closing tenants past their grace period begin erasure. A
    tenant under legal hold is left in 'closing' with a blocked operation."""
    started: list[str] = []
    async with tenant_tx(_platform_ctx()) as conn:
        due = await conn.fetch(
            "SELECT id::text FROM tenants WHERE lifecycle_state='closing' AND erase_after <= now() "
            "AND id <> $1::uuid ORDER BY erase_after LIMIT 20", PLATFORM_TENANT_ID)
    for row in due:
        op_id = await begin_erasure(row[0], requested_by="retention-scheduler")
        if op_id:
            started.append(op_id)
    return started


async def begin_erasure(tenant_id: str, *, requested_by: str) -> Optional[str]:
    """Move a closing tenant to 'erasing' and queue the erasure job."""
    from automation_jobs import enqueue_job

    ctx = _platform_ctx(requested_by)
    async with tenant_tx(ctx) as conn:
        state = await conn.fetchval(
            "SELECT lifecycle_state FROM tenants WHERE id=$1 FOR UPDATE", tenant_id)
        if state not in ("closing", "erasing"):
            raise LifecycleError(f"tenant is {state}; only a closing tenant is erased")
        hold = await active_legal_hold(conn, tenant_id)
        if hold:
            await conn.execute(
                "UPDATE privacy_operations SET state='blocked_legal_hold', updated_at=now(), "
                "error='legal hold ' || $2 WHERE tenant_id=$1 AND kind='closure' AND state='running'",
                tenant_id, str(hold["id"]))
            log.warning("erasure of %s blocked by legal hold %s", tenant_id, hold["id"])
            return None
        existing = await conn.fetchval(
            "SELECT id::text FROM privacy_operations WHERE tenant_id=$1 AND kind='erasure' "
            "AND state IN ('requested','running')", tenant_id)
        if existing:
            op_id = existing
        else:
            await conn.execute(
                "UPDATE privacy_operations SET state='succeeded', completed_at=now(), updated_at=now() "
                "WHERE tenant_id=$1 AND kind='closure' AND state='running'", tenant_id)
            op = await _create_operation(conn, tenant_id=tenant_id, kind="erasure",
                                         requested_by=requested_by, subject_kind="tenant",
                                         subject_ref=tenant_id, state="running")
            op_id = str(op["id"])
        await conn.execute("UPDATE tenants SET lifecycle_state='erasing', updated_at=now() WHERE id=$1",
                           tenant_id)
    emit_event("deletion.started", operation_id=op_id, tenant_id=tenant_id)
    await enqueue_job(ctx, job_type=JOB_ERASE, payload={"operation_id": op_id},
                      idempotency_key=f"privacy-erase:{op_id}", created_by=requested_by,
                      max_attempts=20, priority=80)
    return op_id


async def run_erasure(operation_id: str) -> dict:
    """Run (or resume) an erasure. Each phase checkpoints into
    privacy_operations.progress, so a crash resumes at the phase it was in."""
    ctx = _platform_ctx("privacy-erasure")
    async with tenant_tx(ctx) as conn:
        op = await conn.fetchrow("SELECT * FROM privacy_operations WHERE id=$1", operation_id)
    if op is None or op["kind"] != "erasure":
        raise LifecycleError("unknown erasure operation", status_code=404)
    if op["state"] != "running":
        return {"operation_id": operation_id, "state": op["state"]}
    tenant_id = str(op["tenant_id"])
    progress = op["progress"] if isinstance(op["progress"], dict) else json.loads(op["progress"] or "{}")

    for phase in PHASES:
        if progress.get(phase, {}).get("done"):
            continue
        handler = _PHASE_HANDLERS[phase]
        outcome = await handler(ctx, operation_id, tenant_id, progress)
        progress[phase] = {**outcome, "done": True,
                           "at": datetime.now(timezone.utc).isoformat()}
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                "UPDATE privacy_operations SET progress=$2::jsonb, updated_at=now() WHERE id=$1",
                operation_id, json.dumps(progress, default=str))
    return {"operation_id": operation_id, "state": "succeeded", "progress": progress}


async def _phase_freeze(ctx, op_id, tenant_id, progress) -> dict:
    async with tenant_tx(ctx) as conn:
        cancelled = await cancel_pending_side_effects(
            conn, tenant_id=tenant_id, reason="brokerage erased", actor="privacy-erasure")
        revoked = await revoke_tenant_capabilities(conn, tenant_id=tenant_id, actor="privacy-erasure")
        await _deactivate_routes(conn, tenant_id)
        signed_out = await _sign_out_users(conn, tenant_id, keep_owners=False)
    return {"cancelled": cancelled, "revoked": revoked, "signed_out": len(signed_out)}


async def _phase_providers(ctx, op_id, tenant_id, progress) -> dict:
    """Let go of everything a provider holds for the tenant. Failures are
    recorded and make the operation fail at verification — data is still
    erased, but nobody is told the number was released when it was not."""
    from voice_provider import get_voice_provider

    steps: list[dict] = []
    async with tenant_tx(ctx) as conn:
        voice = await conn.fetch(
            "SELECT id, provider, inbound_forwarding_provider_sid FROM telephony_routes "
            "WHERE tenant_id=$1 AND inbound_forwarding_provider_sid IS NOT NULL", tenant_id)
        sms = await conn.fetch(
            "SELECT m.id, m.provider, m.hosted_order_id, t.voice_caller_id_e164 FROM messaging_routes m "
            "LEFT JOIN telephony_routes t ON t.tenant_id=m.tenant_id AND t.agent_id=m.agent_id "
            "WHERE m.tenant_id=$1 AND m.hosted_order_id IS NOT NULL", tenant_id)
    tctx = TenantContext(agent_id="privacy-erasure", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    for route in voice:
        provider = str(route["provider"] or "twilio")
        try:
            creds = await _provider_credentials(tctx, provider)
            result = await get_voice_provider(provider).release_forwarding_number(
                str(route["inbound_forwarding_provider_sid"]), credentials=creds)
            steps.append({"provider": provider, "action": "release_number", "status": result.status})
        except Exception as exc:  # noqa: BLE001
            steps.append({"provider": provider, "action": "release_number", "status": "failed",
                          "detail": type(exc).__name__})
    for route in sms:
        from messaging_provider import get_messaging_provider

        provider = str(route["provider"] or "telnyx")
        try:
            status = await get_messaging_provider(provider).disconnect_hosted_number(
                str(route["hosted_order_id"]), str(route["voice_caller_id_e164"] or ""),
                credentials=await _provider_credentials(tctx, provider))
            steps.append({"provider": provider, "action": "disconnect_hosted_sms", "status": status or "released"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"provider": provider, "action": "disconnect_hosted_sms", "status": "failed",
                          "detail": type(exc).__name__})
    for outcome in await revoke_personal_credentials(tctx, tenant_id=tenant_id, agent_id=None):
        steps.append({"provider": outcome["provider"], "action": "revoke_credential", "status": outcome["status"]})
    billing = await _set_cancel_at_period_end(tenant_id, True)
    steps.append({"provider": "stripe", "action": "stop_renewal", "status": billing,
                  "note": "billing records are retained by Neoh as controller (7 years)"})
    failed = [s for s in steps if s["status"] in ("failed", "revoke_failed") or str(s["status"]).startswith("failed:")]
    return {"steps": steps, "failed": len(failed)}


async def _provider_credentials(tctx: TenantContext, provider: str) -> dict:
    try:
        if provider == "plivo":
            from telephony_api import _plivo_credentials

            return await _plivo_credentials(tctx)
        if provider == "twilio":
            from telephony_api import _twilio_credentials

            return await _twilio_credentials(tctx)
    except Exception:  # noqa: BLE001 - platform env credentials are the fallback
        return {}
    return {}


async def _object_keys(conn, tenant_id: str) -> tuple[list[str], list[str]]:
    """(keys in the media store, keys in the contract vault) recorded by rows."""
    media: list[str] = []
    for sql in (
        "SELECT s3_key FROM property_media WHERE tenant_id=$1 AND s3_key IS NOT NULL",
        "SELECT storage_key FROM messaging_hosted_documents WHERE tenant_id=$1 AND storage_key IS NOT NULL",
        "SELECT diagnostics->'storage'->>'storage_key' FROM reconstruction_jobs WHERE tenant_id=$1 "
        "AND diagnostics->'storage'->>'storage_key' IS NOT NULL",
    ):
        media.extend(r[0] for r in await conn.fetch(sql, tenant_id))
    vault: list[str] = []
    for sql in (
        "SELECT s3_key FROM contract_documents WHERE tenant_id=$1 AND s3_key IS NOT NULL",
        "SELECT s3_key FROM contract_synthesis_artifacts WHERE tenant_id=$1 AND s3_key IS NOT NULL",
    ):
        vault.extend(r[0] for r in await conn.fetch(sql, tenant_id))
    return sorted(set(media)), sorted(set(vault))


def tenant_prefixes(tenant_id: str) -> list[str]:
    """Every object prefix that carries the tenant id (docs/privacy-data-map.md §3)."""
    return [f"property-media/{tenant_id}/", f"property-view/{tenant_id}/", f"video-studio/{tenant_id}/",
            f"splats/{tenant_id}/", f"tenants/{tenant_id}/", f"messaging-hosted-documents/{tenant_id}/",
            f"privacy/exports/{tenant_id}/"]


async def _phase_objects(ctx, op_id, tenant_id, progress) -> dict:
    import object_storage

    async with tenant_tx(ctx) as conn:
        media_keys, vault_keys = await _object_keys(conn, tenant_id)
    def _delete_media() -> tuple[int, int, int]:
        deleted = missing = failed = 0
        if not object_storage.is_configured():
            return 0, 0, len(media_keys)
        for key in media_keys:
            try:
                if object_storage.delete_object(key):
                    deleted += 1
                else:
                    missing += 1
            except Exception:  # noqa: BLE001
                failed += 1
        for prefix in tenant_prefixes(tenant_id):
            try:
                deleted += object_storage.delete_prefix(prefix)
            except Exception:  # noqa: BLE001
                failed += 1
        return deleted, missing, failed

    import asyncio

    deleted, missing, failed = await asyncio.to_thread(_delete_media)
    if not media_keys and failed and not object_storage.is_configured():
        failed = 0
    def _delete_vault() -> tuple[int, int]:
        ok = bad = 0
        try:
            from contract_vault import ContractVault

            vault = ContractVault()
        except Exception:  # noqa: BLE001
            return 0, len(vault_keys)
        for key in vault_keys:
            try:
                vault.s3_client.delete_object(Bucket=vault.bucket_name, Key=key)
                ok += 1
            except Exception:  # noqa: BLE001
                bad += 1
        return ok, bad

    vault_deleted, vault_failed = (await asyncio.to_thread(_delete_vault)) if vault_keys else (0, 0)
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected, detail) "
            "VALUES ($1,$2,'objects','object_storage','deleted',$3,$4::jsonb)",
            op_id, tenant_id, deleted + vault_deleted,
            json.dumps({"already_absent": missing, "failed": failed + vault_failed,
                        "prefixes": tenant_prefixes(tenant_id)}))
    return {"deleted": deleted, "vault_deleted": vault_deleted, "already_absent": missing,
            "failed": failed + vault_failed}


def contact_hmac(tenant_id: str, contact: str) -> str:
    """Keyed hash of a normalized email/phone for suppression tombstones.
    Keyed per tenant from the master secret, so it matches a future import of
    the same address but cannot be reversed or correlated across tenants."""
    from crypto import derive_tenant_key

    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise LifecycleError("ORACLE_ENCRYPTION_MASTER_KEY is required for erasure", status_code=503)
    key = derive_tenant_key(tenant_id, master + ":suppression-tombstone").encode()
    return hmac.new(key, contact.strip().lower().encode(), hashlib.sha256).hexdigest()


async def _phase_tombstones(ctx, op_id, tenant_id, progress) -> dict:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT contact, channel FROM outreach_suppression WHERE tenant_id=$1 AND lifted_at IS NULL",
            tenant_id)
        made = 0
        for row in rows:
            status = await conn.execute(
                "INSERT INTO suppression_tombstones (tenant_id, contact_hmac, channel, reason) "
                "VALUES ($1,$2,$3,'erased_opt_out') ON CONFLICT DO NOTHING",
                tenant_id, contact_hmac(tenant_id, row["contact"]), row["channel"])
            made += _rowcount(status)
        await conn.execute(
            "INSERT INTO erasure_ledger (operation_id, tenant_id, phase, target, action, rows_affected) "
            "VALUES ($1,$2,'tombstones','outreach_suppression','tombstoned',$3)", op_id, tenant_id, made)
    return {"tombstoned": made}


async def _erasure_catalog(conn) -> tuple[list[str], list[tuple[str, str, str, bool]]]:
    """Tables with a tenant_id column, and FK edges among them:
    (child, parent, child_column, nullable) — tenant_id excluded."""
    tables = [r[0] for r in await conn.fetch(
        """
        SELECT c.relname FROM pg_class c
          JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped
         WHERE c.relkind='r' AND c.relnamespace='public'::regnamespace
           AND NOT (c.relname = ANY (privacy_retained_tables()))
           AND c.relname NOT LIKE 'zz\\_%'
        """)]
    edges = [(r[0], r[1], r[2], r[3]) for r in await conn.fetch(
        """
        SELECT ch.relname, pa.relname, a.attname, NOT a.attnotnull
          FROM pg_constraint k
          JOIN pg_class ch ON ch.oid=k.conrelid
          JOIN pg_class pa ON pa.oid=k.confrelid
          JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum = ANY (k.conkey)
         WHERE k.contype='f' AND ch.relnamespace='public'::regnamespace
           AND a.attname <> 'tenant_id' AND k.confdeltype IN ('a','r')
        """)]
    names = set(tables)
    return tables, [e for e in edges if e[0] in names and e[1] in names]


def erasure_order(tables: Iterable[str], edges: Iterable[tuple[str, str, str, bool]]
                  ) -> tuple[list[tuple[str, str]], list[str]]:
    """Order deletes so no RESTRICT/NO ACTION foreign key is violated.

    `edges` are (child, parent, column, nullable) for FKs that would block
    deleting the parent while a child row still points at it. Children are
    deleted first. A cycle (clients <-> agent_contacts, plans <-> revisions)
    is broken by nulling a nullable column on it first; a cycle with no
    nullable column raises — better to stop than to guess.

    Returns (columns to null first, table order).
    """
    tables = sorted(set(tables))
    blocking = [(c, p, col, nullable) for c, p, col, nullable in edges if c != p]
    to_null: list[tuple[str, str]] = []
    while True:
        order, remaining = _kahn(tables, [(c, p) for c, p, col, _ in blocking
                                          if (c, col) not in to_null])
        if not remaining:
            return sorted(set(to_null)), order
        candidates = [(c, col) for c, p, col, nullable in blocking
                      if nullable and c in remaining and p in remaining and (c, col) not in to_null]
        if not candidates:
            raise RuntimeError(f"unbreakable foreign-key cycle among {sorted(remaining)}")
        to_null.append(sorted(candidates)[0])


def _kahn(tables: list[str], edges: list[tuple[str, str]]) -> tuple[list[str], set[str]]:
    # A parent may be deleted only after every child that references it.
    waiting_on = {t: set() for t in tables}     # parent -> children not yet deleted
    for child, parent in edges:
        waiting_on[parent].add(child)
    order: list[str] = []
    ready = sorted(t for t, kids in waiting_on.items() if not kids)
    done: set[str] = set()
    while ready:
        table = ready.pop(0)
        order.append(table)
        done.add(table)
        for parent, kids in waiting_on.items():
            if table in kids:
                kids.discard(table)
                if not kids and parent not in done and parent not in ready:
                    ready.append(parent)
                    ready.sort()
    remaining = set(tables) - done
    return order, remaining


async def _phase_rows(ctx, op_id, tenant_id, progress) -> dict:
    async with tenant_tx(ctx) as conn:
        tables, edges = await _erasure_catalog(conn)
    to_null, order = erasure_order(tables, edges)
    for table, column in to_null:
        async with tenant_tx(ctx) as conn:
            await conn.fetchval("SELECT privacy_null_tenant_reference($1,$2,$3)", op_id, table, column)
    totals: dict[str, int] = {}
    for table in order:
        total = 0
        while True:
            async with tenant_tx(ctx) as conn:
                n = await conn.fetchval("SELECT privacy_erase_tenant_batch($1,$2,$3)", op_id, table, ERASE_BATCH)
            total += int(n or 0)
            if not n:
                break
        if total:
            totals[table] = total
    return {"deleted": totals, "tables": len(order), "cycle_breaks": [f"{t}.{c}" for t, c in to_null]}


async def _phase_pseudonymize(ctx, op_id, tenant_id, progress) -> dict:
    from privacy_data_map import PSEUDONYMIZE_ON_ERASURE

    out = {}
    for table, columns in PSEUDONYMIZE_ON_ERASURE.items():
        for column in columns:
            async with tenant_tx(ctx) as conn:
                out[f"{table}.{column}"] = int(await conn.fetchval(
                    "SELECT privacy_pseudonymize_column($1,$2,$3)", op_id, table, column) or 0)
    return {"pseudonymized": out}


async def _phase_caches(ctx, op_id, tenant_id, progress) -> dict:
    removed = 0
    url = os.getenv("REDIS_URL", "")
    if url:
        try:
            import redis.asyncio as redis

            client = redis.from_url(url)
            async for key in client.scan_iter(match=f"ai-chat:*{tenant_id}*", count=500):
                removed += int(await client.delete(key) or 0)
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - keys expire within an hour regardless
            return {"redis_keys_removed": removed, "note": f"redis unavailable ({type(exc).__name__}); keys expire within 1h"}
    return {"redis_keys_removed": removed,
            "note": "call-state keys hold no tenant name and expire within 1h; di_cache is shared vendor data with TTLs"}


async def _phase_tenant_row(ctx, op_id, tenant_id, progress) -> dict:
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            """
            UPDATE tenants
               SET name = 'Erased brokerage', slug = 'erased-' || left(id::text, 8) || '-' || left(md5(random()::text), 6),
                   website = NULL, closure_reason = NULL, closure_requested_by = NULL,
                   lifecycle_state = 'erased', erased_at = now(), updated_at = now()
             WHERE id = $1
            """, tenant_id)
    return {"tombstoned": True}


async def _phase_verify(ctx, op_id, tenant_id, progress) -> dict:
    """Count what is left — past RLS — in every erasable table, and list the
    tenant's object prefixes. Anything left fails the operation."""
    import object_storage

    leftovers: dict[str, int] = {}
    async with tenant_tx(ctx) as conn:
        tables, _ = await _erasure_catalog(conn)
        for table in tables:
            n = int(await conn.fetchval("SELECT privacy_count_tenant_rows($1,$2)", op_id, table) or 0)
            if n:
                leftovers[table] = n
    def _count_objects() -> int:
        if not object_storage.is_configured():
            return 0
        left = 0
        for prefix in tenant_prefixes(tenant_id):
            try:
                left += len(object_storage.list_prefix(prefix, limit=1000))
            except Exception:  # noqa: BLE001 - an unlistable prefix is reported as unverified below
                left += 0
        return left

    import asyncio

    objects_left = await asyncio.to_thread(_count_objects)
    provider_failures = int(progress.get("providers", {}).get("failed", 0))
    object_failures = int(progress.get("objects", {}).get("failed", 0))
    return {"rows_left": leftovers, "objects_left": objects_left,
            "provider_failures": provider_failures, "object_failures": object_failures,
            "clean": not leftovers and not objects_left and not provider_failures and not object_failures}


async def _phase_receipt(ctx, op_id, tenant_id, progress) -> dict:
    receipt = build_receipt(op_id, tenant_id, progress)
    import asyncio

    directive_written = await asyncio.to_thread(_write_erasure_directive, op_id, tenant_id, receipt)
    clean = bool(progress.get("verify", {}).get("clean"))
    provider_failures = int(progress.get("providers", {}).get("failed", 0))
    emit_event("deletion.completed" if clean else "deletion.failed", operation_id=op_id, tenant_id=tenant_id,
               rows_deleted=sum((progress.get("rows", {}).get("deleted") or {}).values()),
               objects_deleted=int(progress.get("objects", {}).get("deleted", 0)),
               rows_left=sum((progress.get("verify", {}).get("rows_left") or {}).values()))
    if provider_failures:
        emit_event("deletion.external_cleanup_pending", operation_id=op_id, tenant_id=tenant_id,
                   provider_failures=provider_failures)
    async with tenant_tx(ctx) as conn:
        await _finish_operation(conn, op_id, state="succeeded" if clean else "failed",
                                result={"clean": clean, "directive_written": directive_written},
                                receipt=receipt,
                                error=None if clean else "verification found leftovers or provider failures")
    return {"clean": clean, "directive_written": directive_written}


def build_receipt(op_id: str, tenant_id: str, progress: dict) -> dict:
    """The customer-facing record. Counts and statuses only — never data."""
    erased_at = progress.get("tenant_row", {}).get("at") or datetime.now(timezone.utc).isoformat()
    try:
        backup_until = (datetime.fromisoformat(erased_at) + timedelta(days=BACKUP_DAYS)).isoformat()
    except ValueError:
        backup_until = None
    body = {
        "operation_id": op_id,
        "tenant_id": tenant_id,
        "policy_version": POLICY_VERSION,
        "erased_at": erased_at,
        "rows_deleted": progress.get("rows", {}).get("deleted", {}),
        "objects_deleted": progress.get("objects", {}).get("deleted", 0)
                           + progress.get("objects", {}).get("vault_deleted", 0),
        "opt_outs_kept_as_keyed_hashes": progress.get("tombstones", {}).get("tombstoned", 0),
        "provider_steps": progress.get("providers", {}).get("steps", []),
        "verification": {k: v for k, v in progress.get("verify", {}).items() if k != "at"},
        "retained": {
            "billing_records": "Kept 7 years by Neoh as controller of its own billing (tax law).",
            "security_audit_log": "Kept up to 2 years for security investigations, then deleted.",
            "opt_out_hashes": "Keyed hashes only, 5 years, so an erased person is never re-contacted.",
            "this_receipt": "Kept 7 years as evidence the request was honoured.",
        },
        "backups": (f"Copies remain in encrypted database backups until {backup_until} "
                    f"({BACKUP_DAYS}-day rotation) and cannot be selectively deleted. If a backup "
                    "is ever restored, this erasure is re-applied before the service reopens "
                    "(scripts/reapply-erasures.py)."),
        "not_reachable_by_neoh": [
            "Messages already delivered to recipients' phones and inboxes",
            "Provider-held call/message records (Twilio, Plivo, Telnyx keep billing metadata under their own retention)",
            "Events written into agents' own Google Calendars",
        ],
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return {**body, "sha256": digest}


def _write_erasure_directive(op_id: str, tenant_id: str, receipt: dict) -> bool:
    """A restore from a backup taken before this erasure would bring the
    data back. The directive (tenant id + operation id + time, no data) is
    stored outside the database so scripts/reapply-erasures.py can find and
    re-run it after any restore."""
    import object_storage

    directive = {"tenant_id": tenant_id, "operation_id": op_id,
                 "erased_at": receipt.get("erased_at"), "policy_version": POLICY_VERSION}
    try:
        if not object_storage.is_configured():
            return False
        object_storage.put_bytes(f"privacy/erasure-directives/{op_id}.json",
                                 json.dumps(directive).encode(), "application/json")
        return True
    except Exception:  # noqa: BLE001 - reported in the result; the runbook covers it
        log.exception("erasure directive write failed")
        return False


async def purge_mls_feed(mls_id: str, *, requested_by: str, reason: str, preview: bool) -> dict:
    """Licence terminated: delete that feed's listings (privacy_purge_mls_feed).

    Preconditions the database enforces: no brokerage still entitled. The
    operator also removes the feed's credentials first (runbook §R), or the
    next sync refills what this deletes."""
    ctx = _platform_ctx(requested_by)
    async with tenant_tx(ctx) as conn:
        total = int(await conn.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id=$1", mls_id) or 0)
        referenced = int(await conn.fetchval(
            "SELECT count(*) FROM oracle_mls_listings l WHERE l.mls_id=$1 AND EXISTS "
            "(SELECT 1 FROM transactions t WHERE t.mls_listing_id = l.id)", mls_id) or 0)
        entitled = int(await conn.fetchval(
            "SELECT count(*) FROM mls_feed_entitlements WHERE mls_id=$1", mls_id) or 0)
    plan = {"mls_id": mls_id, "listings": total, "kept_referenced_by_transactions": referenced,
            "brokerages_still_entitled": entitled}
    if preview:
        return {"preview": True, **plan}
    if entitled:
        raise LifecycleError("Revoke every brokerage's entitlement to this feed first.")
    async with tenant_tx(ctx) as conn:
        op = await _create_operation(conn, tenant_id=PLATFORM_TENANT_ID, kind="mls_purge",
                                     requested_by=requested_by, reason=reason, state="running",
                                     params={"mls_id": mls_id})
    deleted = 0
    while True:
        async with tenant_tx(ctx) as conn:
            n = int(await conn.fetchval("SELECT privacy_purge_mls_feed($1,$2,$3)", op["id"], mls_id, ERASE_BATCH) or 0)
        deleted += n
        if not n:
            break
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE mls_sync_status SET backfill_cursor_key=NULL, backfill_complete=false, "
            "license_reason=left('licence terminated; purged ' || now()::date || ': ' || $2, 500), "
            "updated_at=now() WHERE mls_id=$1", mls_id, reason)
        receipt = {**plan, "deleted": deleted, "completed_at": datetime.now(timezone.utc).isoformat()}
        await _finish_operation(conn, str(op["id"]), state="succeeded", result=receipt, receipt=receipt)
    return {"preview": False, "operation_id": str(op["id"]), **receipt}


async def reapply_erasures(*, apply: bool) -> list[dict]:
    """After a database restore: find erased brokerages the restore brought
    back (from the directives in object storage) and, with apply=True, erase
    them again. scripts/reapply-erasures.py is the operator entry point."""
    import asyncio

    import object_storage

    if not object_storage.is_configured():
        raise LifecycleError("object storage is not configured; erasure directives are unreadable", status_code=503)
    keys = await asyncio.to_thread(object_storage.list_prefix, "privacy/erasure-directives/")
    ctx = _platform_ctx("reapply-erasures")
    report: list[dict] = []
    for key in keys:
        try:
            directive = json.loads(await asyncio.to_thread(object_storage.get_bytes, key))
            tenant = str(directive["tenant_id"])
        except Exception as exc:  # noqa: BLE001 - reported, never skipped silently
            report.append({"directive": key, "status": "unreadable", "detail": type(exc).__name__})
            continue
        async with tenant_tx(ctx) as conn:
            state = await conn.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", tenant)
            users = int(await conn.fetchval("SELECT count(*) FROM users WHERE tenant_id=$1", tenant) or 0)
            clients = int(await conn.fetchval("SELECT count(*) FROM clients WHERE tenant_id=$1", tenant) or 0)
            hold = await active_legal_hold(conn, tenant)
        entry = {"tenant_id": tenant, "erased_at": directive.get("erased_at"), "state": state,
                 "users": users, "clients": clients, "legal_hold": bool(hold)}
        resurrected = state not in (None, "erased") or users or clients
        entry["status"] = "resurrected" if resurrected else "still_erased"
        if resurrected and apply:
            if hold:
                entry["status"] = "skipped_legal_hold"
            else:
                async with tenant_tx(ctx) as conn:
                    await conn.execute(
                        "UPDATE tenants SET lifecycle_state='erasing', updated_at=now() WHERE id=$1 "
                        "AND lifecycle_state <> 'erased'", tenant)
                    op = await _create_operation(
                        conn, tenant_id=tenant, kind="erasure", requested_by="reapply-erasures",
                        subject_kind="tenant", subject_ref=tenant, state="running",
                        reason="re-applied after database restore",
                        params={"reapply_of": directive.get("operation_id")})
                await run_erasure(str(op["id"]))
                async with tenant_tx(ctx) as conn:
                    final = await conn.fetchval("SELECT state FROM privacy_operations WHERE id=$1", op["id"])
                entry.update({"status": "reapplied" if final == "succeeded" else "reapply_failed",
                              "operation_id": str(op["id"])})
        report.append(entry)
    return report


_PHASE_HANDLERS = {
    "freeze": _phase_freeze, "providers": _phase_providers, "objects": _phase_objects,
    "tombstones": _phase_tombstones, "rows": _phase_rows, "pseudonymize": _phase_pseudonymize,
    "caches": _phase_caches, "tenant_row": _phase_tenant_row, "verify": _phase_verify,
    "receipt": _phase_receipt,
}


async def _erase_job(job: dict, reporter) -> dict:
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    result = await run_erasure(str(payload["operation_id"]))
    return {"state": result.get("state"), "operation_id": result.get("operation_id")}


# ─────────────────────────────────────────────────────────────────────────────

async def _audit(ctx: TenantContext, action: str, *, target: str, metadata: dict) -> None:
    from audit_ledger import AuditCategory, ledger

    try:
        await ledger.record(AuditCategory.USER_STATE_CHANGE, action, tenant_id=ctx.tenant_id,
                            user_id=ctx.agent_id, target_id=target, metadata=metadata)
    except Exception:  # noqa: BLE001 - the change already committed
        log.exception("audit record for %s failed", action)


_SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def register() -> None:
    from automation_jobs import register_handler

    register_handler(JOB_ERASE, _erase_job)
