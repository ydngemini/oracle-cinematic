"""The per-turn tool offer reads the brokerage's real setup, through the same
rows the approved command would use (T07.04, AC07.01, L07-03), against real
PostgreSQL with FORCE RLS as the application login (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

A fresh brokerage with nothing connected: an agent's turn withholds texting,
email, Neoh calls and scheduling, each with the setup it needs, plus the
broker-owner reads; the owner's turn keeps those reads. Then, one rail at a
time: a voice-verified business number with an active Telnyx hosted order
brings texting back; the number is on Plivo, so calls need the platform's
Plivo account (provider_credentials admits no plivo row: it is env-only);
a saved (not yet validated) mail server closes the email gap; a Google row
under the owner's own label — where the OAuth flow stores it — closes the
owner's calendar gap, not the agent's. The executor refuses a withheld tool
before any command is staged.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_offer_follows_what_the_brokerage_has_set_up(monkeypatch):
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY", "TWILIO_API_SECRET",
                 "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "ORACLE_SMTP_FROM_EMAIL", "ORACLE_SMTP_USERNAME",
                 "ORACLE_CUSTOM_CALL_API_URL", "ORACLE_MESSAGING_PROVIDER", "ORACLE_EMAIL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import ai_chat_store
    import tool_offer
    from ai_tools_read import OPS_ROLE_TOOLS
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = str(uuid.uuid4())
    tag = tenant[:8]
    agent = TenantContext(agent_id=f"agent-{tag}@offer.test", tenant_id=tenant, role=Role.AGENT)
    owner = TenantContext(agent_id=f"owner-{tag}@offer.test", tenant_id=tenant, role=Role.BROKER_OWNER)

    async def credential(provider, label, **extra):
        cols = {"tenant_id": uuid.UUID(tenant), "provider": provider, "account_label": label,
                "created_by": owner.agent_id, **extra}
        names = ",".join(cols)
        marks = ",".join(f"${i + 1}" for i in range(len(cols)))
        await admin.execute(
            f"INSERT INTO provider_credentials ({names}, token_ciphertext) VALUES ({marks}, '\\x00'::bytea)",
            *cols.values())

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Offer')", uuid.UUID(tenant), f"offer-{tag}")
        for c, role in ((agent, "agent"), (owner, "broker_owner")):
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,$3,$2)",
                                uuid.UUID(tenant), c.agent_id, role)

        # ── nothing connected ───────────────────────────────────────────────
        offer = await tool_offer.for_turn(agent, "client")
        assert {"draft_sms", "draft_email", "call_contact", "schedule_event"} <= set(offer.withheld)
        assert "business number" in offer.withheld["draft_sms"]
        assert "mail server" in offer.withheld["draft_email"]
        assert "verify your business number" in offer.withheld["call_contact"]
        assert "Google Calendar" in offer.withheld["schedule_event"]
        assert set(OPS_ROLE_TOOLS) <= set(offer.withheld)
        assert {"add_client_note", "search_clients", "update_client"} <= offer.offered
        owner_offer = await tool_offer.for_turn(owner, "client")
        assert set(OPS_ROLE_TOOLS).isdisjoint(owner_offer.withheld)
        assert "draft_sms" in owner_offer.withheld

        # The executor refuses a withheld tool with the reason, staging nothing.
        turn = str(uuid.uuid4())
        tool_offer.remember(agent, turn, offer)
        receipt = await ai_chat_store.execute_safe_tool(
            agent, agent.agent_id, turn, "draft_sms", {"client_id": str(uuid.uuid4()), "body": "hi"},
            "client", str(uuid.uuid4()), 0)
        assert receipt["ok"] is False and "business number" in receipt["error"]
        assert await admin.fetchval("SELECT count(*) FROM command_executions WHERE tenant_id=$1", uuid.UUID(tenant)) == 0

        # ── texting: a voice-verified business number on Plivo, hosted order active ──
        await admin.execute(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "forward_on_request, forward_when_ai_unavailable, voice_caller_id_e164, voice_caller_id_verified, "
            "outbound_verification_status, active) VALUES ($1,$2,$3,'plivo','MAoffer',false,false,"
            "'+13025550181',true,'verified',true)",
            uuid.UUID(tenant), agent.agent_id, f"+1302555{uuid.uuid4().int % 10000:04d}")
        await admin.execute(
            "INSERT INTO messaging_routes (tenant_id, agent_id, provider, hosted_order_status) "
            "VALUES ($1,$2,'telnyx','processing')", uuid.UUID(tenant), agent.agent_id)
        offer = await tool_offer.for_turn(agent, "client")
        assert "still being set up (processing)" in offer.withheld["draft_sms"]
        await admin.execute("UPDATE messaging_routes SET hosted_order_status='active' WHERE tenant_id=$1",
                            uuid.UUID(tenant))
        offer = await tool_offer.for_turn(agent, "client")
        assert "draft_sms" in offer.offered
        # Calls: the number is verified, but its rail is Plivo and no Plivo account exists.
        assert "Plivo" in offer.withheld["call_contact"]
        monkeypatch.setenv("PLIVO_AUTH_ID", "MA" + "0" * 18)
        monkeypatch.setenv("PLIVO_AUTH_TOKEN", "p" * 32)
        offer = await tool_offer.for_turn(agent, "client")
        assert "call_contact" in offer.offered

        # ── email: a saved mail server, not yet validated, is still a sender ──
        await credential("smtp", "default", validation_status="unverified")
        offer = await tool_offer.for_turn(agent, "client")
        assert "draft_email" in offer.offered

        # ── calendar: the OAuth flow labels the row with the connecting user ──
        await credential("google", owner.agent_id, validation_status="valid")
        assert "schedule_event" in (await tool_offer.for_turn(owner, "client")).offered
        assert "schedule_event" in (await tool_offer.for_turn(agent, "client")).withheld
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("command_executions", "provider_credentials", "messaging_routes", "telephony_routes",
                          "agent_routing_state", "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
