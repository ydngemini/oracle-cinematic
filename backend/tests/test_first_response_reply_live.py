"""A lead's reply is recorded on its first response (0158, L04-03), against
real PostgreSQL with FORCE RLS, as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Pins: an inbound text from the client stamps replied_at and the message as
evidence on their newest accepted/delivered first response; the disposition
stays delivered and lead_engaged is not metered again; the stamp is written
once; an opt-out, a reply to a failed text, a reply outside the window and a
text from a stranger stamp nothing; routing metrics count replies on their
own.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_a_reply_is_recorded_on_the_first_response(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-reply-master-key-0123456789abcdef")
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import commands_api
    import contact_truth
    import lead_routing_api
    import messaging_data
    import outreach_lifecycle as ol
    import speed_to_lead
    from command_providers import ProviderResult
    from db import connection
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    worker = TenantContext(agent_id="command-worker", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    owner = TenantContext(agent_id="owner-r", tenant_id=tenant, role=Role.BROKER_OWNER)
    created = datetime.now(timezone.utc) - timedelta(minutes=5)
    phones = {"sarah": "+13025550121", "luis": "+13025550122", "ana": "+13025550123",
              "ben": "+13025550124"}
    contact = {k: uuid.uuid4() for k in phones}
    client = {k: uuid.uuid4() for k in phones}

    async def first_response(who: str, final: str) -> str:
        """A speed-to-lead text to `who`, staged then accepted, then `final`
        (delivered / failed / accepted) — the real writers throughout."""
        cid = str(uuid.uuid4())
        ref = f"tx-{uuid.uuid4()}"
        await admin.execute(
            """INSERT INTO command_executions (id, tenant_id, command_type, classification,
                   risk_class, target, draft, state, idempotency_key, created_by)
               VALUES ($1,$2,'SMS','outreach','external_send','{}','{}','queued',$3,'agent-r')""",
            uuid.UUID(cid), uuid.UUID(tenant), f"reply-{cid}")
        await speed_to_lead._record_response_event(
            worker, intake_event_id=str(uuid.uuid4()), contact_id=str(contact[who]),
            client_id=str(client[who]), lead_id=None, channel="sms", disposition="staged",
            command_id=cid, lead_created_at=created)
        await admin.execute(
            "UPDATE command_executions SET state='succeeded', provider='telnyx', provider_reference=$2, "
            "provider_submitted_at=now() WHERE id=$1", uuid.UUID(cid), ref)
        await messaging_data.record_outbound_message(
            tenant_id=tenant, agent_id="agent-r", contact_id=str(contact[who]), client_id=str(client[who]),
            provider="telnyx", provider_message_id=ref, from_e164="+13025550100", to_e164=phones[who],
            body="Thanks for reaching out")
        await commands_api._record_send_bookkeeping(
            worker, command_id=cid, command_type=commands_api.CommandType.SMS, target={},
            draft={"body": "hi"}, provider_result=ProviderResult("telnyx", ref, "queued", {}))
        if final != "accepted":
            await messaging_data.record_delivery_update(
                provider="telnyx", provider_message_id=ref, status=final, error_reason=None)
        return cid

    async def inbound(phone: str, text: str = "Yes, interested", opted_out: bool = False) -> str:
        return await messaging_data.record_inbound_message(
            tenant_id=tenant, agent_id="agent-r", provider="telnyx",
            provider_message_id=f"in-{uuid.uuid4()}", from_e164=phone, to_e164="+13025550100",
            text=text, media=[], opted_out=opted_out)

    async def row(cid: str):
        return await admin.fetchrow(
            "SELECT disposition, replied_at, reply_sms_message_id FROM lead_response_events "
            "WHERE command_id=$1", uuid.UUID(cid))

    async def engaged() -> int:
        return await admin.fetchval(
            "SELECT count(*) FROM billing_usage_events WHERE tenant_id=$1 AND metric='lead_engaged'",
            uuid.UUID(tenant))

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Reply test')",
                            uuid.UUID(tenant), f"reply-{tenant[:8]}")
        for who, phone in phones.items():
            digest = contact_truth.lookup_hash(tenant, "phone", contact_truth.normalize_phone(phone))
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, source, data_state, phone_lookup_hash) "
                "VALUES ($1,$2,'reply-test','pending_encryption',$3)", contact[who], uuid.UUID(tenant), digest)
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, contact_id) VALUES ($1,$2,$3,$4)",
                client[who], uuid.UUID(tenant), f"Reply {who}", contact[who])

        sarah = await first_response("sarah", "delivered")
        assert (await row(sarah))["disposition"] == "delivered"
        metered = await engaged()

        # ── the lead answers: stamped, evidence kept, nothing else moves ────
        message = await inbound(phones["sarah"])
        r = await row(sarah)
        assert r["replied_at"] is not None and str(r["reply_sms_message_id"]) == message
        assert r["disposition"] == "delivered", "a reply is not a disposition"
        assert await engaged() == metered, "a reply never meters lead_engaged again"
        first_stamp = r["replied_at"]
        await inbound(phones["sarah"], "Also, can we talk tomorrow?")
        assert (await row(sarah))["replied_at"] == first_stamp, "written once"

        # ── not replies ───────────────────────────────────────────────────────
        luis = await first_response("luis", "delivered")
        await inbound(phones["luis"], "STOP", opted_out=True)
        assert (await row(luis))["replied_at"] is None, "an opt-out is not a reply"

        ana = await first_response("ana", "failed")
        assert (await row(ana))["disposition"] == "failed"
        await inbound(phones["ana"])
        assert (await row(ana))["replied_at"] is None, "a failed text cannot be answered"

        ben = await first_response("ben", "accepted")
        await admin.execute(
            "UPDATE lead_response_events SET accepted_at = now() - make_interval(days => $2) "
            "WHERE command_id=$1", uuid.UUID(ben), ol.REPLY_WINDOW_DAYS + 1)
        await inbound(phones["ben"])
        assert (await row(ben))["replied_at"] is None, "outside the reply window"

        await inbound("+13025550199")  # nobody's number: stored, linked to nothing

        # ── metrics count replies on their own ───────────────────────────────
        metrics = await lead_routing_api.routing_metrics(days=30, ctx=owner)
        first = metrics["first_response"]
        assert first["replied"] == 1 and first["failed"] == 1
        assert first["delivered"] == 2 and "responded" not in first
        assert "within 7 days" in first["replied_measured_as"]
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table in ("billing_usage_events", "lead_response_events", "client_activities", "sms_messages",
                          "command_executions", "clients", "agent_contacts"):
                await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1::uuid", tenant)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
