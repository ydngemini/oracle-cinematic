"""lead_engaged is metered at delivery, once — against real PostgreSQL with
FORCE RLS and the full migration chain (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Audit A05 / prompt 18 AC18.04 / prompt 04 AC04.05. Drives the real write paths:
speed_to_lead's staged ledger row, the command worker's acceptance bookkeeping,
the Telnyx delivery-receipt handler and the carrier call-status handler, then
reads billing_usage_events back. Proves:

- staged, approved and accepted leave no usage row;
- a carrier delivery receipt meters exactly one billable row that names its
  command, and a duplicate receipt adds nothing;
- a receipt that arrives before the acceptance is recorded is applied at
  acceptance, not lost;
- an answered call meters once ('answered' then 'completed');
- a failed or cancelled send never meters;
- a row the pre-0133 code wrote at the staged step is non-billable: never
  drained, never counted as pending — and promoted in place on delivery;
- the routing metrics count staged, accepted and delivered apart, and measure
  latency to acceptance, under the broker owner's RLS;
- 0133's CHECK refuses a billable lead_engaged row that names no command;
- the reconciliation query finds nothing to fix for correct rows and flags a
  staged-step row that had already been reported to Stripe.

The brokerage is a fresh throwaway tenant, removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_lead_engagement_is_metered_at_delivery_exactly_once():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import billing_usage
    import commands_api
    import messaging_data
    import outreach_lifecycle as ol
    import speed_to_lead
    from command_providers import ProviderResult
    from db import connection
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = str(uuid.uuid4())
    worker = TenantContext(agent_id="command-worker", tenant_id=tenant, role=Role.PLATFORM_ADMIN)
    agent = TenantContext(agent_id="agent-a", tenant_id=tenant, role=Role.AGENT)
    owner = TenantContext(agent_id="owner-a", tenant_id=tenant, role=Role.BROKER_OWNER)
    created = datetime.now(timezone.utc) - timedelta(seconds=45)

    async def command(state: str, command_type: str = "SMS") -> str:
        cid = str(uuid.uuid4())
        await admin.execute(
            """INSERT INTO command_executions (id, tenant_id, command_type, classification,
                   risk_class, target, draft, state, idempotency_key, created_by)
               VALUES ($1,$2,$3,'outreach','external_send','{}','{}',$4,$5,'agent-a')""",
            uuid.UUID(cid), uuid.UUID(tenant), command_type, state, f"live-{cid}")
        return cid

    async def stage(cid: str, intake: str) -> None:
        # The real staging write speed_to_lead makes.
        await speed_to_lead._record_response_event(
            worker, intake_event_id=intake, contact_id=None, client_id=None,
            lead_id=None, channel="sms", disposition="staged", command_id=cid,
            lead_created_at=created,
        )

    async def accept(cid: str, reference: str, provider: str = "telnyx") -> None:
        await admin.execute(
            """UPDATE command_executions SET state='succeeded', provider=$2,
                      provider_reference=$3, provider_submitted_at=now() WHERE id=$1""",
            uuid.UUID(cid), provider, reference)
        await commands_api._record_send_bookkeeping(
            worker, command_id=cid, command_type=commands_api.CommandType.SMS,
            target={}, draft={"body": "hi"},
            provider_result=ProviderResult(provider, reference, "queued", {}),
        )

    async def outbound_sms(reference: str) -> None:
        await messaging_data.record_outbound_message(
            tenant_id=tenant, agent_id="agent-a", contact_id=None, client_id=None,
            provider="telnyx", provider_message_id=reference,
            from_e164="+13025550100", to_e164="+13025550111", body="hi")

    async def usage() -> list:
        return await admin.fetch(
            """SELECT idempotency_key, lifecycle_stage, source_table, source_id::text,
                      reported_at FROM billing_usage_events
                WHERE tenant_id=$1 AND metric='lead_engaged' ORDER BY idempotency_key""",
            uuid.UUID(tenant))

    async def disposition(cid: str):
        return await admin.fetchrow(
            "SELECT disposition, approved_at, accepted_at, delivered_at"
            "  FROM lead_response_events WHERE command_id=$1", uuid.UUID(cid))

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'A05 live test')",
                            uuid.UUID(tenant), f"a05-{tenant[:8]}")

        # ── 1. Text: staged → approved → accepted → delivered ─────────────────
        sms = await command("awaiting_approval")
        await stage(sms, str(uuid.uuid4()))
        assert (await disposition(sms))["disposition"] == "staged"
        assert await usage() == [], "a staged draft engaged nobody"

        await admin.execute("UPDATE command_executions SET state='queued' WHERE id=$1",
                            uuid.UUID(sms))
        async with tenant_tx(agent) as conn:  # the approving agent, as the app role
            assert await ol.advance_first_response(
                conn, tenant_id=tenant, command_id=sms, stage=ol.Stage.APPROVED) is True
        row = await disposition(sms)
        assert row["disposition"] == "approved" and row["approved_at"] is not None
        assert await usage() == []

        ref = f"tx-{uuid.uuid4()}"
        await outbound_sms(ref)
        await accept(sms, ref)
        row = await disposition(sms)
        assert row["disposition"] == "accepted" and row["accepted_at"] is not None
        assert await usage() == [], "provider acceptance is not delivery"

        assert await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=ref, status="delivered", error_reason=None)
        row = await disposition(sms)
        assert row["disposition"] == "delivered" and row["delivered_at"] is not None
        rows = await usage()
        assert len(rows) == 1
        assert rows[0]["lifecycle_stage"] == "delivered"
        assert rows[0]["source_table"] == "command_executions" and rows[0]["source_id"] == sms

        # A duplicate receipt and a late 'sent' change nothing.
        await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=ref, status="delivered", error_reason=None)
        await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=ref, status="sent", error_reason=None)
        assert len(await usage()) == 1

        # ── 2. The receipt beat the acceptance ────────────────────────────────
        early = await command("executing")
        await stage(early, str(uuid.uuid4()))
        early_ref = f"tx-{uuid.uuid4()}"
        await outbound_sms(early_ref)
        await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=early_ref, status="delivered", error_reason=None)
        assert (await disposition(early))["disposition"] == "staged"
        assert len(await usage()) == 1, "not yet accepted, so not yet billable"
        await accept(early, early_ref)
        assert (await disposition(early))["disposition"] == "delivered"
        assert len(await usage()) == 2

        # ── 3. Failed and cancelled sends never meter ─────────────────────────
        failed = await command("queued")
        await stage(failed, str(uuid.uuid4()))
        failed_ref = f"tx-{uuid.uuid4()}"
        await outbound_sms(failed_ref)
        await accept(failed, failed_ref)
        await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=failed_ref, status="failed",
            error_reason="carrier rejected")
        assert (await disposition(failed))["disposition"] == "accepted"

        never_sent = await command("failed")
        await stage(never_sent, str(uuid.uuid4()))
        async with tenant_tx(worker) as conn:
            await ol.advance_first_response(
                conn, tenant_id=tenant, command_id=never_sent, stage=ol.Stage.DELIVERED)
        # A stray "delivered" for a command no provider took moves nothing.
        assert (await disposition(never_sent))["disposition"] == "staged"

        cancelled = await command("cancelled")
        await stage(cancelled, str(uuid.uuid4()))
        async with tenant_tx(agent) as conn:
            await ol.advance_first_response(
                conn, tenant_id=tenant, command_id=cancelled, stage=ol.Stage.CANCELLED)
        assert (await disposition(cancelled))["disposition"] == "cancelled"
        async with tenant_tx(worker) as conn:
            await ol.advance_first_response(
                conn, tenant_id=tenant, command_id=cancelled, stage=ol.Stage.DELIVERED)
        assert (await disposition(cancelled))["disposition"] == "cancelled", "terminal"
        assert len(await usage()) == 2

        # ── 4. An answered call meters once ───────────────────────────────────
        call = await command("succeeded", "CALL")
        await stage(call, str(uuid.uuid4()))
        call_ref, client = f"call-{uuid.uuid4()}", uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'Pat')",
                            client, uuid.UUID(tenant))
        await admin.execute(
            """INSERT INTO live_call_sessions (tenant_id, command_id, client_id, consent_recorded,
                   consent_basis, transcript_status, provider_call_id, created_by)
               VALUES ($1,$2,$3,false,'awaiting_explicit_live_transcription_consent','pending',$4,'agent-a')""",
            uuid.UUID(tenant), uuid.UUID(call), client, call_ref)
        await commands_api.record_call_outcome(call_ref, "no-answer")
        assert len(await usage()) == 2, "an unanswered call reached nobody"
        await commands_api.record_call_outcome(call_ref, "answered")
        await commands_api.record_call_outcome(call_ref, "completed", duration_seconds=61)
        assert (await disposition(call))["disposition"] == "delivered"
        assert len(await usage()) == 3

        # ── 5. A staged-step row from the pre-0133 code ───────────────────────
        legacy_intake, legacy_reported = str(uuid.uuid4()), str(uuid.uuid4())
        await admin.execute(
            """INSERT INTO billing_usage_events (tenant_id, metric, quantity, idempotency_key)
               VALUES ($1,'lead_engaged',1,$2)""", uuid.UUID(tenant), f"lead-engaged:{legacy_intake}")
        await admin.execute(
            """INSERT INTO billing_usage_events (tenant_id, metric, quantity, idempotency_key,
                   reported_at, stripe_event_id)
               VALUES ($1,'lead_engaged',1,$2, now(), 'evt_test')""",
            uuid.UUID(tenant), f"lead-engaged:{legacy_reported}")

        summary = await billing_usage.usage_summary(days=30, ctx=owner)
        engaged = next(m for m in summary["metrics"] if m["metric"] == "lead_engaged")
        assert engaged["events"] == 3 and engaged["not_billable_events"] == 2

        problems = await billing_usage.reconcile_lead_engagements(owner)
        assert [p["problem"] for p in problems] == ["reported_before_delivery"]
        assert problems[0]["idempotency_key"] == f"lead-engaged:{legacy_reported}"

        # The drain reports billable rows only, each once.
        reported: list[str] = []

        class _Meter:
            @staticmethod
            def create(**kwargs):
                reported.append(kwargs["identifier"])
                return type("E", (), {"identifier": kwargs["identifier"]})()

        real_price, real_meter = billing_usage.STRIPE_METERED_PRICE_ID, billing_usage.stripe.billing.MeterEvent
        real_customer = billing_usage._stripe_customer_for

        async def _customer(ctx):
            return "cus_live_test"

        billing_usage.STRIPE_METERED_PRICE_ID = "price_live_test"
        billing_usage.stripe.billing.MeterEvent = _Meter
        billing_usage._stripe_customer_for = _customer
        try:
            first = await billing_usage.drain_usage_to_stripe(worker)
            second = await billing_usage.drain_usage_to_stripe(worker)
        finally:
            billing_usage.STRIPE_METERED_PRICE_ID = real_price
            billing_usage.stripe.billing.MeterEvent = real_meter
            billing_usage._stripe_customer_for = real_customer
        assert first["reported"] == 3 and second["reported"] == 0
        assert f"lead-engaged:{legacy_intake}" not in reported

        # Delivery promotes the unreported staged-step row instead of adding one.
        legacy_cmd = await command("awaiting_approval")
        await stage(legacy_cmd, legacy_intake)
        legacy_ref = f"tx-{uuid.uuid4()}"
        await outbound_sms(legacy_ref)
        await accept(legacy_cmd, legacy_ref)
        await messaging_data.record_delivery_update(
            provider="telnyx", provider_message_id=legacy_ref, status="delivered", error_reason=None)
        promoted = [r for r in await usage() if r["idempotency_key"] == f"lead-engaged:{legacy_intake}"]
        assert len(promoted) == 1 and promoted[0]["source_id"] == legacy_cmd
        assert len(await usage()) == 5  # 4 billable + the reported staged-step row

        # ── 6. Routing metrics keep the stages apart (AC04.05) ────────────────
        import lead_routing_api

        metrics = await lead_routing_api.routing_metrics(days=30, ctx=owner)
        first = metrics["first_response"]
        assert first["attempts"] == 7
        assert first["staged"] == 1, "the never-sent draft is staged, not a response"
        assert first["accepted"] == 5 and first["delivered"] == 4 and first["cancelled"] == 1
        # The call went straight from staged to answered: accepted, untimed.
        assert first["accepted_untimed"] == 1
        assert first["latency_measured_to"] == "accepted"
        assert first["p50_seconds"] is not None and first["p50_seconds"] >= 44
        assert "responded" not in first

        # ── 7. The schema refuses a billable row with no delivered source ─────
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                """INSERT INTO billing_usage_events (tenant_id, metric, quantity, idempotency_key,
                       lifecycle_stage) VALUES ($1,'lead_engaged',1,'bad-key','delivered')""",
                uuid.UUID(tenant))
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                """INSERT INTO billing_usage_events (tenant_id, metric, quantity, idempotency_key,
                       lifecycle_stage, source_table, source_id)
                   VALUES ($1,'lead_engaged',1,'bad-key-2','accepted','command_executions',$2)""",
                uuid.UUID(tenant), uuid.UUID(sms))

        # Every billable row reconciles to a succeeded, delivered command.
        problems = await billing_usage.reconcile_lead_engagements(owner)
        assert [p["problem"] for p in problems] == ["reported_before_delivery"]
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
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", tenant)
            await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(tenant))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
