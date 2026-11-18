"""Hosted-SMS hardening against real PostgreSQL (external audits 25.6, 25.7,
25.8): non-final hosted orders are swept; the ownership code has an atomic
attempt limit and lockout; contradicting delivery receipts are journaled and
held for reconciliation.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

NUMBER = "+13025550166"


@pytest.mark.parametrize("part", ["otp_lockout", "hosted_order_sweep", "receipt_contradiction"])
def test_hosted_sms_hardening(monkeypatch, part):
    asyncio.run(_scenario(monkeypatch, part))


async def _scenario(monkeypatch, part):
    import asyncpg

    import component_health as ch
    import messaging_api
    import messaging_data as md
    import messaging_provider
    import outreach_lifecycle
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    ctx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
    calls = {"verify": 0, "order": 0}
    behaviour = {"verify": False, "order": "pending", "verify_raises": False}

    class _Adapter:
        async def complete_ownership_verification(self, order, number, code, *, credentials=None):
            calls["verify"] += 1
            await asyncio.sleep(0.05)      # in flight together
            if behaviour["verify_raises"]:
                raise TimeoutError("provider timed out")
            return behaviour["verify"]

        async def get_hosted_order_status(self, order, *, credentials=None):
            calls["order"] += 1
            return {"status": behaviour["order"]}

    async def creds(_ctx):
        return {"api_key": "k"}

    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda _name: _Adapter())
    monkeypatch.setattr(messaging_api, "_telnyx_credentials", creds)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=6)

    async def route():
        return await admin.fetchrow("SELECT hosted_order_status, verification_attempt_count, "
                                    "verification_locked_until, hosted_order_checked_at, hosted_order_status_at "
                                    "FROM messaging_routes WHERE tenant_id=$1", T)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"mh-{s}", f"MH {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "voice_caller_id_e164, voice_caller_id_verified, outbound_verification_status, active, "
            "forward_on_request, forward_when_ai_unavailable) "
            "VALUES ($1,$2,$3,'plivo','MAhardening',$3,true,'verified',true,false,false)", T, owner, NUMBER)
        await admin.execute(
            "INSERT INTO messaging_routes (tenant_id, agent_id, provider, hosted_order_id, hosted_order_status, "
            "active) VALUES ($1,$2,'telnyx','order-1','pending_verification',true)", T, owner)

        if part == "otp_lockout":
            # ── 25.7: ten wrong codes at once — exactly the limit reaches the provider ──
            results = await asyncio.gather(*(md.complete_messaging_verification(ctx, "000000", credentials={})
                                             for _ in range(10)), return_exceptions=True)
            assert calls["verify"] == md._MAX_VERIFICATION_ATTEMPTS
            assert all(isinstance(r, md.MessagingDataError) for r in results)
            assert sum(md.VERIFICATION_LOCKED in str(r) for r in results) >= 10 - md._MAX_VERIFICATION_ATTEMPTS
            row = await route()
            assert row["verification_locked_until"] is not None
            # even the right code is refused while locked, without asking the provider
            behaviour["verify"] = True
            with pytest.raises(md.MessagingDataError, match="Too many incorrect codes"):
                await md.complete_messaging_verification(ctx, "123456", credentials={})
            assert calls["verify"] == md._MAX_VERIFICATION_ATTEMPTS
            # the lock passes: a provider failure gives the attempt back; then the right code works
            await admin.execute("UPDATE messaging_routes SET verification_locked_until = now() - interval '1 second' "
                                "WHERE tenant_id=$1", T)
            behaviour["verify_raises"] = True
            with pytest.raises(TimeoutError):
                await md.complete_messaging_verification(ctx, "123456", credentials={})
            assert (await route())["verification_attempt_count"] == 0
            behaviour["verify_raises"] = False
            await md.complete_messaging_verification(ctx, "123456", credentials={})
            row = await route()
            assert row["hosted_order_status"] == "processing" and row["verification_attempt_count"] == 0
            assert row["verification_locked_until"] is None

        if part == "hosted_order_sweep":
            await admin.execute("UPDATE messaging_routes SET hosted_order_status='processing' WHERE tenant_id=$1", T)
            row = await route()
            # ── 25.6: the sweep reads non-final hosted orders; the status change is stamped ──
            before = row["hosted_order_status_at"]
            assert (T, owner) in await md.hosted_orders_to_sync(limit=1000)
            await messaging_api.sync_hosted_orders(limit=1000)
            row = await route()
            assert row["hosted_order_status"] == "processing" and row["hosted_order_checked_at"] is not None
            await admin.execute("UPDATE messaging_routes SET hosted_order_status_at = now() - interval '8 days' "
                                "WHERE tenant_id=$1", T)
            assert (await ch._db_components())["messaging_registration"]["detail"].get("hosting_stalled", 0) >= 1
            behaviour["order"] = "successful"
            await messaging_api.sync_hosted_orders(limit=1000)
            row = await route()
            assert row["hosted_order_status"] == "active" and row["hosted_order_status_at"] > before
            assert (T, owner) not in await md.hosted_orders_to_sync(limit=1000)

        if part == "receipt_contradiction":
            # ── 25.8: a contradicting final receipt is journaled and held ──
            delivered, failed = [], []

            async def on_delivered(conn, **kw):
                delivered.append(kw["provider_message_id"])

            async def on_failed(conn, **kw):
                failed.append(kw["provider_message_id"])

            monkeypatch.setattr(outreach_lifecycle, "record_sms_delivered", on_delivered)
            monkeypatch.setattr(outreach_lifecycle, "record_sms_failed", on_failed)
            msg = f"msg-{s}"
            await admin.execute(
                "INSERT INTO sms_messages (tenant_id, agent_id, direction, provider, provider_message_id, from_e164, "
                "to_e164, status) VALUES ($1,$2,'outbound','telnyx',$3,$4,'+13025550142','queued')", T, owner, msg, NUMBER)
            assert await md.record_delivery_update(provider="telnyx", provider_message_id=msg, status="delivered",
                                                   error_reason=None)
            assert await md.record_delivery_update(provider="telnyx", provider_message_id=msg, status="undelivered",
                                                   error_reason="carrier 30003")
            assert await admin.fetchval("SELECT status FROM sms_messages WHERE provider_message_id=$1", msg) == "delivered"
            journal = await admin.fetch("SELECT id, status, contradicts FROM sms_delivery_receipts "
                                        "WHERE tenant_id=$1 ORDER BY id", T)
            assert [(r["status"], r["contradicts"]) for r in journal] == [("delivered", None), ("undelivered", "delivered")]
            assert delivered == [msg] and failed == []                 # the contradiction ran no side effect
            assert (await ch._db_components())["messaging_registration"]["detail"].get("receipt_contradictions", 0) >= 1
            # a duplicate of the stored outcome is journaled but contradicts nothing
            await md.record_delivery_update(provider="telnyx", provider_message_id=msg, status="delivered",
                                            error_reason=None)
            assert await admin.fetchval("SELECT count(*) FROM sms_delivery_receipts WHERE tenant_id=$1 "
                                        "AND contradicts IS NOT NULL", T) == 1
            # an operator checks the provider's record and settles it
            platform = TenantContext(agent_id="ops@neoh.test", role=Role.PLATFORM_ADMIN,
                                     tenant_id="00000000-0000-0000-0000-000000000000")
            out = await md.reconcile_delivery_receipt(platform, receipt_id=journal[1]["id"], final_status="failed")
            assert out["status_changed"] and out["receipts_reconciled"] == 1 and failed == [msg]
            assert await admin.fetchval("SELECT status FROM sms_messages WHERE provider_message_id=$1", msg) == "failed"
            with pytest.raises(md.ReceiptNotFound):
                await md.reconcile_delivery_receipt(platform, receipt_id=journal[1]["id"], final_status="delivered")
            # pruning keeps nothing old except open contradictions
            await admin.execute("UPDATE sms_delivery_receipts SET received_at = now() - interval '100 days' "
                                "WHERE tenant_id=$1", T)
            await md.prune_delivery_receipts()
            assert await admin.fetchval("SELECT count(*) FROM sms_delivery_receipts WHERE tenant_id=$1", T) == 0
            # an unknown message is still 'not yet written' (the webhook retries), journaling nothing
            assert not await md.record_delivery_update(provider="telnyx", provider_message_id="nope",
                                                       status="delivered", error_reason=None)
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
