"""A lapsed subscription stops new outbound contact, and nothing else (T18.05,
AC18.07, F18.07) — against real PostgreSQL with FORCE RLS, as the application
role (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

The subscription moves only through SIGNED synthetic Stripe events posted to
the real webhook route (never live Stripe, nothing is charged), and the only
fake is the SMTP relay. With ORACLE_BILLING_ENFORCED on:

* past_due (Stripe's retry window) is still entitled: the composer emails;
* canceled: the CRM composer refuses an outbound email (402) and queues
  nothing — it was the one way to email a client that skipped the gate —
  while inbound logging, internal notes and reading the thread keep working;
* an email queued before the cancellation is not sent: blocked at send time,
  and final (a later reactivation does not fire it unseen);
* an approved command queued before the cancellation is not executed: the
  command fails "Not sent" and the job is not retried;
* a staged command cannot be released afterwards by any path — the mission
  executor's self-release calls the same release_command — so it stays
  awaiting approval and nothing is queued;
* a platform admin acting in the brokerage gets the same refusal up front,
  not a "queued" email the send worker is certain to block;
* /billing/status says whether the brokerage was ever entitled, so a paid
  brokerage whose new checkout is still unpaid (a bank debit settling, or
  failed) is told apart from one that never paid, and keeps its records.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
SECRET = "whsec_" + "d" * 32


def _signed(event: dict) -> tuple[bytes, dict]:
    body = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


@pytest.fixture
def relay(monkeypatch):
    """The SMTP relay, faked; every other step is the real code."""
    import commands_api
    from command_providers import ProviderResult

    sent: list[dict] = []

    async def send(draft, credentials=None, reply_to=None, list_unsubscribe_url=None):
        sent.append(dict(draft))
        return ProviderResult("smtp", f"<{uuid.uuid4().hex}@lapsed.test>", "submitted", {})

    async def no_credential(*a, **k):
        return None

    monkeypatch.setattr(commands_api, "_resolve_email_provider", lambda: (send, "smtp"))
    monkeypatch.setattr(commands_api, "_load_provider_credential", no_credential)
    monkeypatch.setenv("ORACLE_BILLING_ENFORCED", "1")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.test")
    # 0154 (A01): email subjects and bodies are sealed at write.
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-lapsed-billing-master-key-0123456789")
    monkeypatch.delenv("ORACLE_DOMAIN", raising=False)
    import billing

    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", SECRET)
    return sent


class _World:
    """One brokerage: an owner, an agent, the agent's client, a postal address
    (so the composer's own setup check passes) and a paid subscription."""

    def __init__(self, admin, client):
        from tenancy import Role, TenantContext

        self.admin, self.http = admin, client
        self.tenant = str(uuid.uuid4())
        self.tag = self.tenant[:8]
        self.sub = f"sub_lapse_{uuid.uuid4().hex[:10]}"
        self.client_id = str(uuid.uuid4())
        self.agent = TenantContext(agent_id=f"agent-{self.tag}@lapsed.test", tenant_id=self.tenant, role=Role.AGENT)
        self.event_ids: list[str] = []
        self.clock = 100

    async def build(self, payment_status="paid"):
        import billing

        self.path = next(r.path for r in billing.router.routes if "webhook" in r.path)
        await self.admin.execute(
            "INSERT INTO tenants (id, slug, name, postal_address) VALUES ($1,$2,'Lapsed Realty',"
            "'1 Loockerman St, Dover, DE 19901')", uuid.UUID(self.tenant), f"lapsed-{self.tag}")
        await self.admin.execute(
            "INSERT INTO users (tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
            "VALUES ($1,$2,'agent',$2,'scrypt$x$y','Agent Lapsed',true)", uuid.UUID(self.tenant), self.agent.agent_id)
        await self.admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) VALUES ($1,$2,'Pat Lapsed',$3,$4)",
            uuid.UUID(self.client_id), uuid.UUID(self.tenant), self.agent.agent_id, f"pat-{self.tag}@client.test")
        await self.stripe("checkout.session.completed", {
            "metadata": {"tenant_id": self.tenant}, "subscription": self.sub,
            "customer": f"cus_lapse_{self.tag}", "payment_status": payment_status})
        assert await self.status() == ("active" if payment_status == "paid" else "incomplete")
        return self

    async def stripe(self, kind: str, obj: dict):
        self.clock += 100
        event_id = f"evt_Lapse{uuid.uuid4().hex[:12]}"
        self.event_ids.append(event_id)
        body, headers = _signed({"id": event_id, "object": "event", "type": kind, "created": self.clock,
                                 "data": {"object": obj}})
        response = await self.http.post(self.path, content=body, headers=headers)
        assert response.status_code == 200, response.text

    async def status(self):
        return await self.admin.fetchval("SELECT status FROM subscriptions WHERE tenant_id=$1",
                                         uuid.UUID(self.tenant))

    async def past_due(self):
        await self.stripe("customer.subscription.updated", {"id": self.sub, "status": "past_due"})
        assert await self.status() == "past_due"

    async def cancel(self):
        await self.stripe("customer.subscription.deleted", {"id": self.sub})
        assert await self.status() == "canceled"

    async def seen(self):
        """What the front-end gate reads: GET /billing/status, as the agent."""
        import billing

        return await billing.subscription_status(self.tenant, ctx=self.agent)

    async def compose(self, channel="email", direction="outbound"):
        import crm

        return await crm.send_message(
            self.client_id, crm.MessageCreate(channel=channel, direction=direction, subject="Your showing",
                                              body="Hi Pat, the house is ready."), self.agent)

    async def stage(self, key: str) -> str:
        import commands_api
        from commands_api import CommandCreate, CommandType

        staged = await commands_api.create_command(CommandCreate(
            command_type=CommandType.EMAIL, target={"email": f"pat-{self.tag}@client.test",
                                                    "client_id": self.client_id},
            draft={"subject": "Hello", "body": "Following up."}, idempotency_key=f"lapse-{self.tag}-{key}"),
            ctx=self.agent)
        return str(staged["command"]["id"])

    async def count(self, table: str) -> int:
        return await self.admin.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id=$1", uuid.UUID(self.tenant))

    async def cleanup(self):
        await self.admin.execute("DELETE FROM stripe_webhook_events WHERE event_id = ANY($1::text[])", self.event_ids)
        await self.admin.execute("SET session_replication_role = replica")
        try:
            tables = await self.admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                await self.admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", self.tenant)
            await self.admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(self.tenant))
        finally:
            await self.admin.execute("SET session_replication_role = origin")


def _run(scenario, payment_status="paid"):
    async def main():
        import asyncpg
        import httpx
        from fastapi import FastAPI

        import billing
        from db import connection

        app = FastAPI()
        app.include_router(billing.router)
        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=4)
        world = None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                world = _World(admin, client)
                await world.build(payment_status)
                await scenario(world)
        finally:
            if world is not None:
                await world.cleanup()
            await admin.close()
            await connection.close_pool()

    asyncio.run(main())


def test_the_composer_refuses_outbound_email_once_canceled_and_keeps_the_record_open(relay):
    async def scenario(w: _World):
        from fastapi import HTTPException

        import crm

        # Stripe's retry window is still entitled (billing._ENTITLED_STATUSES).
        await w.past_due()
        assert (await w.compose())["delivery_status"] == "queued"

        await w.cancel()
        outbox, logs = await w.count("email_outbox"), await w.count("interaction_logs")
        with pytest.raises(HTTPException) as refused:
            await w.compose()
        assert refused.value.status_code == 402, refused.value.detail
        assert await w.count("email_outbox") == outbox, "nothing queued"
        assert await w.count("interaction_logs") == logs, "nothing logged as contact"

        # The record stays open: logging what the client sent, an internal
        # note, and reading the thread back.
        assert (await w.compose(direction="inbound"))["interaction"]["direction"] == "inbound"
        assert (await w.compose(channel="note"))["delivery_status"] == "internal"
        thread = await crm.client_interactions(w.client_id, limit=50, ctx=w.agent)
        assert len(thread["interactions"]) == logs + 2
        assert relay == []

    _run(scenario)


def test_an_email_queued_before_the_cancellation_is_not_sent(relay):
    async def scenario(w: _World):
        import email_outbox

        queued = (await w.compose())["queued_email_id"]
        await w.cancel()
        assert await email_outbox.deliver(w.tenant, queued) == {"status": "blocked"}
        assert relay == [], "nothing reached the relay"
        status, error = await w.admin.fetchrow("SELECT status, error FROM email_outbox WHERE id=$1",
                                               uuid.UUID(queued))
        assert status == "failed" and error.startswith("blocked: "), (status, error)
        assert await w.admin.fetchval("SELECT last_contacted_at FROM clients WHERE id=$1",
                                      uuid.UUID(w.client_id)) is None

    _run(scenario)


def test_an_approved_command_queued_before_the_cancellation_is_not_executed(relay):
    async def scenario(w: _World):
        import commands_api
        from commands_api import ApprovalDecision

        command_id = await w.stage("approved")
        await commands_api.approve_command(command_id, ApprovalDecision(reason="checked, send it"), ctx=w.agent)
        queued = await w.admin.fetchrow("SELECT payload, approval_id, created_by FROM automation_jobs "
                                     "WHERE tenant_id=$1 AND job_type='command:execute'", uuid.UUID(w.tenant))
        await w.cancel()

        class _Reporter:
            job = {"tenant_id": w.tenant, "created_by": queued["created_by"], "approval_id": queued["approval_id"]}

            async def progress(self, *_a, **_k):
                return None

        with pytest.raises(Exception) as refused:
            await commands_api._execute_command_job(json.loads(queued["payload"]), _Reporter())
        assert relay == [], "nothing reached the relay"
        # Not retried: a retry would send the moment the subscription returns.
        assert getattr(refused.value, "job_terminal", False), repr(refused.value)
        state, error = await w.admin.fetchrow("SELECT state, last_error FROM command_executions WHERE id=$1",
                                              uuid.UUID(command_id))
        assert state == "failed" and error.startswith("Not sent"), (state, error)

    _run(scenario)


def test_a_staged_command_cannot_be_released_after_the_cancellation(relay, monkeypatch):
    async def scenario(w: _World):
        from fastapi import HTTPException

        import commands_api
        from missions import executor

        person, mission = await w.stage("person"), await w.stage("mission")
        await w.cancel()
        with pytest.raises(HTTPException) as refused:
            await commands_api.release_command(w.agent, await commands_api._get_command(w.agent, person),
                                               reason="checked, send it")
        assert refused.value.status_code == 402
        # A mission's self-release goes through the same function.
        assert await executor._release(w.agent, {"id": mission}) is False
        for command_id in (person, mission):
            assert await w.admin.fetchval("SELECT state FROM command_executions WHERE id=$1",
                                          uuid.UUID(command_id)) == "awaiting_approval"
        assert await w.admin.fetchval("SELECT count(*) FROM automation_jobs WHERE tenant_id=$1 "
                                      "AND job_type='command:execute'", uuid.UUID(w.tenant)) == 0

    _run(scenario)


def test_a_platform_admin_is_refused_up_front_not_queued_to_be_blocked(relay):
    """The request gate lets a platform admin through; the send worker does
    not. So the composer asks the worker's reading, and an admin acting in a
    lapsed brokerage gets the same 402 instead of a 'queued' that never goes."""
    async def scenario(w: _World):
        from fastapi import HTTPException

        import crm
        from tenancy import Role, TenantContext

        await w.cancel()
        operator = TenantContext(agent_id=f"ops-{w.tag}@lapsed.test", tenant_id=w.tenant,
                                 role=Role.PLATFORM_ADMIN)
        outbox = await w.count("email_outbox")
        with pytest.raises(HTTPException) as refused:
            await crm.send_message(w.client_id, crm.MessageCreate(
                channel="email", direction="outbound", subject="Your showing", body="Hi Pat."), operator)
        assert refused.value.status_code == 402, refused.value.detail
        assert await w.count("email_outbox") == outbox, "nothing queued"
        assert relay == []

    _run(scenario)


def test_a_paid_brokerage_checking_out_again_unpaid_is_lapsed_not_never_paid(relay):
    """Cancel, then a new checkout by bank debit: Stripe completes the session
    'unpaid' and a NEW subscription row starts 'incomplete' (and becomes
    'incomplete_expired' if the debit fails). The newest status alone reads
    like a brokerage that never paid; the history says it has records."""
    async def scenario(w: _World):
        assert (await w.seen()).ever_entitled is True
        await w.cancel()
        again = f"sub_again_{uuid.uuid4().hex[:10]}"
        await w.stripe("checkout.session.completed", {
            "metadata": {"tenant_id": w.tenant}, "subscription": again,
            "customer": f"cus_lapse_{w.tag}", "payment_status": "unpaid"})
        seen = await w.seen()
        assert (seen.active, seen.status, seen.ever_entitled) == (False, "incomplete", True)
        await w.stripe("customer.subscription.updated", {"id": again, "status": "incomplete_expired"})
        seen = await w.seen()
        assert (seen.active, seen.status, seen.ever_entitled) == (False, "incomplete_expired", True)

    _run(scenario)


def test_a_brokerage_that_never_paid_is_not_counted_as_ever_entitled(relay):
    async def scenario(w: _World):
        seen = await w.seen()
        assert (seen.active, seen.status, seen.ever_entitled) == (False, "incomplete", False)
        await w.stripe("customer.subscription.updated", {"id": w.sub, "status": "incomplete_expired"})
        seen = await w.seen()
        assert (seen.active, seen.status, seen.ever_entitled) == (False, "incomplete_expired", False)

    _run(scenario, payment_status="unpaid")
