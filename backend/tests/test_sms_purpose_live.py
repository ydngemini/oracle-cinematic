"""The person approving a one-to-one text declares its purpose, and the send
gate applies it (external audit 26.5), against real PostgreSQL with FORCE RLS
as the application role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Informational needs prior express consent (written in any evidence status, or
oral; a prior business relationship does not count for a text); marketing,
the default, needs documented written consent. A colleague who may not decide
the approval cannot declare a purpose, nobody can change it after approval,
and a speed-to-lead text cannot be declared informational.

Nothing is sent: the worker runs to the compliance gate, which is the real
gate, and the test stops it there before any provider.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

# A Monday, 12:00 in Delaware: inside the calling window.
IN_WINDOW = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)
STOP = "stopped by the test before any provider"


def test_the_approver_declares_a_texts_purpose_and_the_gate_applies_it(monkeypatch):
    assert asyncio.run(_scenario(monkeypatch)) == {
        "colleague declares at approval": 403,
        "colleague attempt left the approval": ("pending", None),
        "informational stored": {"purpose": "informational", "declared": True, "approval": "current"},
        "audit entry for the approval": {"purpose": "informational", "purpose_declared": True},
        "approve again after approval": 409,
        "colleague after approval": 409,
        "edit after approval": 409,
        "purpose after those attempts": "informational",
        "default stored": {"purpose": "marketing", "declared": False, "approval": "current"},
        "speed-to-lead declared informational": 422,
        "speed-to-lead approval after the refusal": "pending",
        "speed-to-lead approved as marketing": "marketing",
        "gate: oral consent, informational": ("informational", True),
        "gate: oral consent, default (marketing)": ("marketing", False),
        "gate: prior business only, informational": ("informational", False),
        "gate: speed-to-lead": ("marketing", False),
        # Review of PR #170: an edit that drops the context cannot launder a
        # campaign text into a one-to-one one.
        "edited campaign context kept": {"speed-to-lead": "speed-to-lead", "smart-plan": "smart-plan",
                                         "sales-agent": "our-ai-sales-agent", "mission": "mission_id kept"},
        "edited campaign declared informational": {"speed-to-lead": 422, "smart-plan": 422,
                                                   "sales-agent": 422, "mission": 422},
        "edited campaign approvals after the refusal": {"speed-to-lead": "pending", "smart-plan": "pending",
                                                        "sales-agent": "pending", "mission": "pending"},
        "gate: campaign text, context emptied, stale informational declaration": ("marketing", False),
    }


async def _scenario(monkeypatch) -> dict:
    import asyncpg
    from fastapi import HTTPException

    import approval_service
    import billing
    import commands_api as ca
    import outreach_compliance
    from commands_api import ApprovalDecision, CommandCreate, CommandEdit, CommandType
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    agent_a, agent_b = f"a-{s}@live.test", f"b-{s}@live.test"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    actx = TenantContext(agent_id=agent_a, tenant_id=T, role=Role.AGENT)
    bctx = TenantContext(agent_id=agent_b, tenant_id=T, role=Role.AGENT)
    phones = {"oral": "+13025550861", "ebr": "+13025550862"}
    clients: dict[str, str] = {}
    out: dict = {}

    audits: list[dict] = []
    real_record = approval_service.ledger.record

    async def capture(**kwargs):
        audits.append(kwargs)
        return await real_record(**kwargs)

    monkeypatch.setattr(approval_service.ledger, "record", capture)

    async def stage(key: str, who: str, context: dict | None = None, idem: str | None = None) -> dict:
        target = {"phone": phones[who], "client_id": clients[who], "state_code": "DE",
                  "timezone": "America/New_York"}
        created = await ca.create_command(CommandCreate(
            command_type=CommandType.SMS, target=target,
            draft={"body": "Your showing at 12 Elm St is confirmed for 3 pm."},
            idempotency_key=idem or f"purpose-{s}-{key}", context=context or {}), ctx=actx)
        return await ca._get_command(actx, created["command"]["id"])

    async def stored(cid) -> tuple:
        row = await admin.fetchrow(
            "SELECT ce.draft->'declared_purpose' AS d, ce.approval_id, aa.status FROM command_executions ce "
            "JOIN action_approvals aa ON aa.id = ce.approval_id WHERE ce.id=$1", uuid.UUID(str(cid)))
        record = json.loads(row["d"]) if row["d"] else None
        return record, str(row["approval_id"]), row["status"]

    def summary(record, approval_id) -> dict:
        return {"purpose": record["purpose"], "declared": record["declared"],
                "approval": "current" if record["approval_id"] == approval_id else record["approval_id"]}

    async def status_of(call) -> int:
        try:
            await call
        except HTTPException as exc:
            return exc.status_code
        return 200

    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name, postal_address) "
                                "VALUES ($1,$2,$3,'1 Main St, Dover, DE 19901')", T, f"purpose-{s}", f"B {s}")
            for agent in (agent_a, agent_b):
                await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,'agent',$2)",
                                    T, agent)
            for who, phone in phones.items():     # brokerage pool clients: both agents may edit them
                clients[who] = str(await admin.fetchval(
                    "INSERT INTO clients (tenant_id, full_name, phone) VALUES ($1,$2,$3) RETURNING id",
                    T, f"{who} {s}", phone))
            await admin.execute(
                "INSERT INTO outreach_consent (tenant_id, contact, channel, consent_type) VALUES "
                "($1,$2,'sms','express_oral'), ($1,$3,'sms','prior_business')", T, phones["oral"], phones["ebr"])

        # 1. A colleague who may not decide A's approval cannot declare its purpose.
        informational = await stage("informational", "oral")
        out["colleague declares at approval"] = await status_of(ca.approve_command(
            str(informational["id"]), ApprovalDecision(reason="colleague approving", purpose="informational"),
            ctx=bctx))
        record, _approval, status = await stored(informational["id"])
        out["colleague attempt left the approval"] = (status, record)

        # 2. The requester approves and declares it informational.
        await ca.approve_command(str(informational["id"]),
                                 ApprovalDecision(reason="showing confirmation", purpose="informational"), ctx=actx)
        record, approval_id, _status = await stored(informational["id"])
        out["informational stored"] = summary(record, approval_id)
        entry = next(a for a in audits if a.get("action") == "approval_approved" and a.get("target_id") == approval_id)
        assert entry["user_id"] == agent_a
        out["audit entry for the approval"] = {k: entry["metadata"][k] for k in ("purpose", "purpose_declared")}

        # 3. Nobody changes it after approval.
        out["approve again after approval"] = await status_of(ca.approve_command(
            str(informational["id"]), ApprovalDecision(reason="second click marketing", purpose="marketing"), ctx=actx))
        out["colleague after approval"] = await status_of(ca.approve_command(
            str(informational["id"]), ApprovalDecision(reason="colleague re-approving", purpose="marketing"), ctx=bctx))
        out["edit after approval"] = await status_of(ca.edit_command(str(informational["id"]), CommandEdit(
            target=dict(informational["target"]) if isinstance(informational["target"], dict)
            else json.loads(informational["target"]),
            draft={"body": "Changed after approval."}), ctx=actx))
        out["purpose after those attempts"] = (await stored(informational["id"]))[0]["purpose"]

        # 4. Undeclared is marketing.
        default = await stage("default", "oral")
        await ca.approve_command(str(default["id"]), ApprovalDecision(reason="approved, no purpose"), ctx=actx)
        record, approval_id, _ = await stored(default["id"])
        out["default stored"] = summary(record, approval_id)

        ebr = await stage("ebr", "ebr")
        await ca.approve_command(str(ebr["id"]), ApprovalDecision(reason="deadline reminder", purpose="informational"),
                                 ctx=actx)

        # 5. A speed-to-lead text stays marketing.
        stl = await stage("stl", "oral", context={"source": "speed-to-lead", "contact_id": None})
        out["speed-to-lead declared informational"] = await status_of(ca.approve_command(
            str(stl["id"]), ApprovalDecision(reason="first response", purpose="informational"), ctx=actx))
        out["speed-to-lead approval after the refusal"] = (await stored(stl["id"]))[2]
        await ca.approve_command(str(stl["id"]), ApprovalDecision(reason="first response", purpose="marketing"),
                                 ctx=actx)
        out["speed-to-lead approved as marketing"] = (await stored(stl["id"]))[0]["purpose"]

        # 5b. Each campaign pipeline's text, edited with no context, still cannot
        #     be declared informational: the edit keeps the server-owned context
        #     and the idempotency key / created_by mark the origin anyway.
        target = {"phone": phones["oral"], "client_id": clients["oral"], "state_code": "DE",
                  "timezone": "America/New_York"}
        mission_id = str(uuid.uuid4())
        campaigns = {
            "speed-to-lead": await stage("c-stl", "oral", {"source": "speed-to-lead", "intake_event_id": "i-1"},
                                         idem=f"speed-to-lead:{s}"),
            "smart-plan": await stage("c-sp", "oral", {"source": "smart-plan", "step_run_id": "r-1"},
                                      idem=f"smart-plan:{s}"),
            "sales-agent": await stage("c-sa", "oral", {"source": "our-ai-sales-agent"},
                                       idem=f"sales-agent:follow_up:{s}"),
        }
        staged = await ca.stage_command(
            actx, command_type=CommandType.SMS, target=target,
            draft={"body": "Your showing at 12 Elm St is confirmed for 3 pm."},
            context={"mission_id": mission_id, "objective": "book showings", "required_disclosures": []},
            idempotency_key=f"mission:{mission_id}:action:{s}", created_by=f"mission:{mission_id}")
        campaigns["mission"] = await ca._get_command(actx, staged["command"]["id"])
        kept, refused, after = {}, {}, {}
        for name, command in campaigns.items():
            await ca.edit_command(str(command["id"]), CommandEdit(
                target=target, draft={"body": "Edited: your showing is at 4 pm."}), ctx=actx)
            context = json.loads(await admin.fetchval(
                "SELECT draft->'context' FROM command_executions WHERE id=$1", uuid.UUID(str(command["id"]))))
            kept[name] = "mission_id kept" if context.get("mission_id") == mission_id else context.get("source")
            refused[name] = await status_of(ca.approve_command(
                str(command["id"]), ApprovalDecision(reason="edited, now informational", purpose="informational"),
                ctx=actx))
            after[name] = (await stored(command["id"]))[2]
        out["edited campaign context kept"] = kept
        out["edited campaign declared informational"] = refused
        out["edited campaign approvals after the refusal"] = after

        # A campaign text whose context was emptied before this fix, carrying
        # a stale informational declaration for its current approval.
        laundered = campaigns["speed-to-lead"]
        await ca.approve_command(str(laundered["id"]), ApprovalDecision(reason="first response, marketing"),
                                 ctx=actx)
        await admin.execute(
            "UPDATE command_executions SET draft = jsonb_set(jsonb_set(draft, '{context}', '{}'::jsonb), "
            "'{declared_purpose}', jsonb_build_object('purpose', 'informational', 'declared', true, "
            "'declared_by', $2::text, 'approval_id', approval_id::text)) WHERE id=$1",
            uuid.UUID(str(laundered["id"])), agent_a)

        # 6. The worker reads the declaration and the real gate applies it.
        seen: list = []
        real_guard = outreach_compliance.guard_outreach

        async def gate(ctx, **kwargs):
            decision = await real_guard(ctx, **{**kwargs, "now_utc": IN_WINDOW, "log": False})
            seen.append((getattr(kwargs["purpose"], "value", kwargs["purpose"]), decision.allowed))
            return SimpleNamespace(allowed=False, blockers=[STOP])

        async def nothing(*_a, **_k):
            return None

        monkeypatch.setattr(billing, "entitlement_refusal", nothing)
        monkeypatch.setattr(ca, "guard_outreach", gate)
        for label, command in (("gate: oral consent, informational", informational),
                               ("gate: oral consent, default (marketing)", default),
                               ("gate: prior business only, informational", ebr),
                               ("gate: speed-to-lead", stl),
                               ("gate: campaign text, context emptied, stale informational declaration", laundered)):
            row = await admin.fetchrow("SELECT * FROM command_executions WHERE id=$1", uuid.UUID(str(command["id"])))
            target = json.loads(row["target"]) if isinstance(row["target"], str) else row["target"]

            class Reporter:
                job = {"tenant_id": T, "approval_id": str(row["approval_id"]), "created_by": agent_a}

                async def progress(self, *_a, **_k):
                    return None

            with pytest.raises(Exception) as stopped:
                await ca._execute_command_job(
                    {"command_type": "SMS", "command_id": str(row["id"]), "target": target,
                     "draft": {"body": "Your showing at 12 Elm St is confirmed for 3 pm."}}, Reporter())
            assert STOP in str(stopped.value), stopped.value
            out[label] = seen[-1]
        return out
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in await admin.fetch(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
