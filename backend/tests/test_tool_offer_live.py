"""The per-turn tool offer reads the brokerage's real setup (T07.04, AC07.01,
L07-03), against real PostgreSQL with FORCE RLS as the application login
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

A fresh brokerage with nothing connected: an agent's turn withholds texting,
email, Neoh calls and scheduling, each with the setup it needs, plus the
broker-owner reads; the owner's turn keeps those reads. A registered text
number and a Twilio account validated for texts bring draft_sms back while
call_contact stays withheld; a connected Google Calendar brings scheduling
back. The executor refuses a withheld tool before any command is staged.
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
                 "ORACLE_SMTP_FROM_EMAIL", "ORACLE_SMTP_USERNAME"):
        monkeypatch.delenv(name, raising=False)
    asyncio.run(_scenario())


async def _scenario():
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
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Offer')", uuid.UUID(tenant), f"offer-{tag}")
        for c, role in ((agent, "agent"), (owner, "broker_owner")):
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,$3,$2)",
                                uuid.UUID(tenant), c.agent_id, role)

        # Nothing connected: every channel needs setup, and the agent is not
        # offered the owner's reads.
        offer = await tool_offer.for_turn(agent, "client")
        assert {"draft_sms", "draft_email", "call_contact", "schedule_event"} <= set(offer.withheld)
        assert "text number" in offer.withheld["draft_sms"]
        assert "mail server" in offer.withheld["draft_email"]
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
        assert receipt["ok"] is False and "text number" in receipt["error"]
        assert await admin.fetchval("SELECT count(*) FROM command_executions WHERE tenant_id=$1", uuid.UUID(tenant)) == 0

        # A registered text number for this agent and a Twilio account validated
        # for texts: texting is back; Neoh calls still need voice.
        await admin.execute(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "forward_on_request, forward_when_ai_unavailable, sms_sender_e164, sms_sender_type, active) "
            "VALUES ($1,$2,$3,'twilio','ACoffer',false,false,'+13025550181','twilio_registered',true)",
            uuid.UUID(tenant), agent.agent_id, f"+1302555{uuid.uuid4().int % 10000:04d}")
        await admin.execute(
            "INSERT INTO provider_credentials (tenant_id, provider, account_label, token_ciphertext, created_by, "
            "validation_status, validated_capabilities) VALUES ($1,'twilio','default','\\x00'::bytea,$2,'valid',"
            "'{\"sms\": true, \"voice\": false}'::jsonb)", uuid.UUID(tenant), owner.agent_id)
        offer = await tool_offer.for_turn(agent, "client")
        assert "draft_sms" in offer.offered
        assert "call_contact" in offer.withheld and "voice" in offer.withheld["call_contact"]

        # A connected calendar brings scheduling back.
        await admin.execute(
            "INSERT INTO provider_credentials (tenant_id, provider, account_label, token_ciphertext, created_by, "
            "validation_status) VALUES ($1,'google','default','\\x00'::bytea,$2,'valid')",
            uuid.UUID(tenant), owner.agent_id)
        offer = await tool_offer.for_turn(agent, "client")
        assert "schedule_event" in offer.offered and "draft_email" in offer.withheld
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("command_executions", "provider_credentials", "telephony_routes", "agent_routing_state",
                          "users"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
