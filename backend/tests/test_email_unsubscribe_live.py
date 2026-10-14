"""An email unsubscribe binds every agent in the brokerage (L17-08) — against
real PostgreSQL with FORCE RLS, as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

End to end, with only the SMTP relay faked: the broker owner sets the postal
address; agent A emails their client through the real composer path
(crm.send_message -> email_outbox.deliver); the sent body carries the address
and an unsubscribe link and the relay is handed the RFC 8058 URL; the person
opens the link (nothing changes) and one-click POSTs it without logging in;
then agent B — who cannot see A's client — is blocked from emailing the same
person: B's email queued BEFORE the unsubscribe is stopped at send time, a new
one is refused at the composer, and the command path's gate says no. Another
brokerage is not bound by it (the opt-out is the brokerage's).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


class _Request:
    def __init__(self, accept: str = "text/html"):
        self.headers = {"accept": accept}


def test_an_unsubscribe_blocks_the_next_send_by_another_agent(monkeypatch):
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.test")
    monkeypatch.delenv("ORACLE_DOMAIN", raising=False)
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import brokerage_onboarding as bo
    import commands_api
    import crm
    import email_outbox
    import email_unsubscribe_api
    from command_providers import ProviderResult
    from db import connection
    from outreach_compliance import Channel, guard_outreach
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "d")}
    agent = {k: f"{k}-{tag}@unsub.test" for k in users}
    person = f"pat-{tag}@client.test"
    clients = {k: uuid.uuid4() for k in ("a", "b", "d")}

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=other_tenant if who == "d" else tenant, role=role)

    sent: list[dict] = []

    async def relay(draft, credentials=None, reply_to=None, list_unsubscribe_url=None):
        sent.append({**draft, "list_unsubscribe_url": list_unsubscribe_url})
        return ProviderResult("smtp", f"<{uuid.uuid4().hex}@unsub.test>", "submitted", {})

    async def no_credential(*a, **k):
        return None

    monkeypatch.setattr(commands_api, "_resolve_email_provider", lambda: (relay, "smtp"))
    monkeypatch.setattr(commands_api, "_load_provider_credential", no_credential)

    async def suppressions() -> list:
        return await admin.fetch(
            "SELECT channel, reason, source_text FROM outreach_suppression "
            "WHERE tenant_id=$1 AND contact=$2 AND lifted_at IS NULL", uuid.UUID(tenant), person)

    async def email(who: str, client: str) -> str:
        out = await crm.send_message(client, crm.MessageCreate(channel="email", subject="Your showing",
                                                               body="Hi Pat, the house is ready."), ctx(who))
        return out["queued_email_id"]

    try:
        for t, slug, name in ((tenant, "unsub", "Unsub Realty"), (other_tenant, "unsub-other", "Other Realty")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", t, f"{slug}-{tag}", name)
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other_tenant if who == "d" else tenant, agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        # The same person is a client of A, of B (written differently), and of
        # an agent at another brokerage.
        for who, cid in clients.items():
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) VALUES ($1,$2,$3,$4,$5)",
                cid, other_tenant if who == "d" else tenant, f"Pat {who}", agent[who],
                person.upper() if who == "b" else person)

        # 1. No address yet: setup required, and a send would carry none.
        async with connection.tenant_tx(ctx("owner")) as conn:
            state = await bo.compute_setup_state(conn, ctx("owner"))
        assert state["commercial_email"]["status"] == "NEEDS_ACTION"
        await bo.update_profile(bo.BrokerageProfileUpdate(postal_address="1 Loockerman St\nDover, DE 19901"),
                                ctx("owner"))
        async with connection.tenant_tx(ctx("a")) as conn:  # any member reads the state
            state = await bo.compute_setup_state(conn, ctx("a"))
        assert state["commercial_email"]["status"] == "READY"

        # 2. B can not see A's client (record rule, 0131) — the block below does
        #    not come from visibility.
        async with connection.tenant_tx(ctx("b")) as conn:
            assert await conn.fetchval("SELECT count(*) FROM clients WHERE id=$1", clients["a"]) == 0

        # 3. B queues an email first; A emails and it goes out with the footer.
        queued_b = await email("b", str(clients["b"]))
        queued_a = await email("a", str(clients["a"]))
        assert await email_outbox.deliver(tenant, queued_a) == {"status": "sent"}
        assert len(sent) == 1
        body, url = sent[0]["body"], sent[0]["list_unsubscribe_url"]
        assert body.startswith("Hi Pat, the house is ready.\n\n--\nUnsub Realty\n1 Loockerman St\nDover, DE 19901\n")
        assert url.startswith("https://app.neoh.test/api/public/email/unsubscribe/u1") and url in body
        assert person not in url and person.split("@")[0] not in url
        token = re.search(r"/unsubscribe/(u1[A-Za-z0-9_-]+)", url).group(1)

        # 4. Opening the link changes nothing; the one-click POST, no login, does.
        page = await email_unsubscribe_api.confirm_unsubscribe(token, _Request())
        assert page.status_code == 200 and b"Unsub Realty" in page.body
        assert await suppressions() == []
        done = await email_unsubscribe_api.one_click_unsubscribe(token, _Request("application/json"))
        assert done.status_code == 200
        again = await email_unsubscribe_api.one_click_unsubscribe(token, _Request("application/json"))
        assert again.status_code == 200
        rows = await suppressions()
        assert [(r["channel"], r["reason"], r["source_text"]) for r in rows] == [
            ("email", "stop_keyword", "email unsubscribe link")], "idempotent: one active row"

        # 5. B's email queued before the unsubscribe is stopped at send time.
        assert await email_outbox.deliver(tenant, queued_b) == {"status": "blocked"}
        assert len(sent) == 1, "nothing more reached the relay"
        status, error = await admin.fetchrow("SELECT status, error FROM email_outbox WHERE id=$1",
                                             uuid.UUID(queued_b))
        assert status == "failed" and error.startswith("blocked: ")

        # 6. A new email from B is refused at the composer, and the approved-
        #    command gate (commands_api._execute_command_job) says no as well.
        with pytest.raises(HTTPException) as refused:
            await email("b", str(clients["b"]))
        assert refused.value.status_code == 451
        decision = await guard_outreach(ctx("b"), channel=Channel.EMAIL, contact=person.upper(),
                                        state_code=None, log=False)
        assert not decision.allowed

        # 7. Another brokerage is not bound by this brokerage's opt-out.
        other = await guard_outreach(ctx("d"), channel=Channel.EMAIL, contact=person, state_code=None, log=False)
        assert other.allowed
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                for t in (tenant, other_tenant):
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
            for t in (tenant, other_tenant):
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(t))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
