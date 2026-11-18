"""The SMS send-readiness gate against real PostgreSQL, and the command worker
honouring it (external audit 25.1).

Before: an active hosted order and a verified voice number were enough for
the Telnyx send branch to submit an approved text, whatever the brokerage's
10DLC campaign or the number's assignment said.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from types import SimpleNamespace

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

NUMBER = "+13025550177"


def test_a_text_is_sent_only_from_a_registered_sender(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import billing
    import commands_api as ca
    import messaging_data as md
    import messaging_provider
    from db import connection
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "telnyx")
    s = secrets.token_hex(5)
    T, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    ctx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)

    ACCOUNT = md.account_fingerprint({"api_key": "KEY-A"})

    async def readiness(account=ACCOUNT):
        return await md.messaging_send_readiness(ctx, provider="telnyx", account=account)

    async def blockers():
        return (await readiness())["blockers"]

    try:
        for tenant in (T, OTHER):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                tenant, f"sr-{tenant[:8]}", f"SR {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        await admin.execute(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "voice_caller_id_e164, voice_caller_id_verified, outbound_verification_status, active, "
            "forward_on_request, forward_when_ai_unavailable) "
            "VALUES ($1,$2,$3,'plivo','MAsrlive',$3,true,'verified',true,false,false)", T, owner, NUMBER)
        route = await admin.fetchval(
            "INSERT INTO messaging_routes (tenant_id, agent_id, provider, provider_account_id, "
            "hosted_order_status, active) VALUES ($1,$2,'telnyx',$3,'active',true) RETURNING id",
            T, owner, ACCOUNT)

        # Active hosting, verified number, no campaign: the old gate sent this.
        assert await blockers() == ["no_campaign"]

        brand = await admin.fetchval("INSERT INTO tenant_messaging_brands (tenant_id, provider, provider_brand_id, "
                                     "status) VALUES ($1,'telnyx','B1','verified') RETURNING id", T)
        campaign = await admin.fetchval(
            "INSERT INTO tenant_messaging_campaigns (tenant_id, brand_id, provider, provider_campaign_id, status) "
            "VALUES ($1,$2,'telnyx',$3,'pending') RETURNING id", T, brand, f"TC{s}")
        await admin.execute("UPDATE messaging_routes SET campaign_id=$2 WHERE id=$1", route, campaign)
        assert set(await blockers()) == {"campaign_not_approved", "number_not_assigned"}
        await admin.execute("UPDATE tenant_messaging_campaigns SET status='rejected' WHERE id=$1", campaign)
        assert "campaign_not_approved" in await blockers()

        # Approved (as read back from the provider), but this number is not
        # assigned (nothing, or another number).
        await admin.execute("UPDATE tenant_messaging_campaigns SET status='approved', status_ok_at=now() "
                            "WHERE id=$1", campaign)
        assert await blockers() == ["number_not_assigned"]
        await admin.execute(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref, detail) "
            "VALUES ($1,$2,'telnyx','10dlc_number_assignment','attached','PN1',$3::jsonb)",
            T, owner, json.dumps({"campaign_id": f"TC{s}", "phone_number": "+13025550999",
                                  "assignment_status": "ASSIGNED"}))
        assert await blockers() == ["number_not_assigned"]
        # an assignment still unconfirmed (unknown) is not committed
        pending_assignment = await admin.fetchval(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, detail) "
            "VALUES ($1,$2,'telnyx','10dlc_number_assignment','unknown',$3::jsonb) RETURNING id",
            T, owner, json.dumps({"campaign_id": f"TC{s}", "phone_number": NUMBER}))
        assert await blockers() == ["number_not_assigned"]
        # attached, but the provider has not finished assigning it
        await admin.execute("UPDATE provider_purchases SET state='attached', provider_ref='PN2', "
                            "detail = detail || '{\"assignment_status\": \"PENDING_ASSIGNMENT\"}' WHERE id=$1",
                            pending_assignment)
        assert await blockers() == ["number_not_assigned"]
        await admin.execute("UPDATE provider_purchases SET detail = detail || '{\"assignment_status\": \"ASSIGNED\"}' "
                            "WHERE id=$1", pending_assignment)

        # The valid case.
        assert await readiness() == {"ready": True, "blockers": [], "sender": NUMBER}

        # Approval not confirmed by a successful read for 14 days (reads
        # failing, e.g. a cancelled account answering 401): not sendable.
        await admin.execute("UPDATE tenant_messaging_campaigns SET status_ok_at = now() - interval '15 days' "
                            "WHERE id=$1", campaign)
        assert await blockers() == ["registration_unverified"]
        await admin.execute("UPDATE tenant_messaging_campaigns SET status_ok_at = now() WHERE id=$1", campaign)
        # Without a credential the route must carry a recorded account.
        assert (await readiness(None))["ready"]
        await admin.execute("UPDATE messaging_routes SET provider_account_id='plivo' WHERE id=$1", route)
        assert (await readiness(None))["blockers"] == ["wrong_account"]
        await admin.execute("UPDATE messaging_routes SET provider_account_id=$2 WHERE id=$1", route, ACCOUNT)

        # The hosting must belong to the account the send uses.
        other_account = md.account_fingerprint({"api_key": "KEY-B"})
        assert (await readiness(other_account))["blockers"] == ["wrong_account"]
        # The voice route switched off, or a toll-free sender: not sendable.
        await admin.execute("UPDATE telephony_routes SET active=false WHERE tenant_id=$1", T)
        assert "no_verified_number" in await blockers()
        await admin.execute("UPDATE telephony_routes SET active=true WHERE tenant_id=$1", T)
        await admin.execute("UPDATE telephony_routes SET voice_caller_id_e164='+18885550177' WHERE tenant_id=$1", T)
        assert "toll_free_not_supported" in await blockers()
        await admin.execute("UPDATE telephony_routes SET voice_caller_id_e164=$2 WHERE tenant_id=$1", T, NUMBER)
        assert (await readiness())["ready"]

        # Another brokerage's campaign cannot be put on this route at all.
        other_brand = await admin.fetchval("INSERT INTO tenant_messaging_brands (tenant_id, provider, status) "
                                           "VALUES ($1,'telnyx','verified') RETURNING id", OTHER)
        other_campaign = await admin.fetchval(
            "INSERT INTO tenant_messaging_campaigns (tenant_id, brand_id, provider, provider_campaign_id, status) "
            "VALUES ($1,$2,'telnyx','TCX','approved') RETURNING id", OTHER, other_brand)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await admin.execute("UPDATE messaging_routes SET campaign_id=$2 WHERE id=$1", route, other_campaign)

        # The worker: a revoked registration refuses before the provider is
        # asked; the valid one sends from the proven sender.
        sent = []

        class _Adapter:
            async def send_message(self, *, to, from_, text, credentials=None):
                sent.append(from_)
                import command_providers

                return command_providers.ProviderResult("telnyx", f"msg-{s}-{len(sent)}", "queued", {})

        async def allowed(*_a, **_k):
            return SimpleNamespace(allowed=True, blockers=[])

        async def nothing(*_a, **_k):
            return None

        async def yes(*_a, **_k):
            return True

        monkeypatch.setattr(billing, "entitlement_refusal", nothing)
        monkeypatch.setattr(ca, "_requester_may_contact", yes)
        monkeypatch.setattr(ca, "guard_outreach", allowed)
        async def telnyx_key(_ctx, provider, **_k):
            return json.dumps({"api_key": "KEY-A"}) if provider == "telnyx" else None

        monkeypatch.setattr(ca, "_load_provider_credential", telnyx_key)
        monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda _name: _Adapter())

        class Reporter:
            job = {"tenant_id": T, "created_by": owner}

            async def progress(self, *_a, **_k):
                return None

        async def text(key):
            command = str(await admin.fetchval(
                "INSERT INTO command_executions (tenant_id, command_type, classification, risk_class, target, "
                "draft, state, idempotency_key, created_by) "
                "VALUES ($1,'SMS','external','outreach',$2::jsonb,'{}'::jsonb,'queued',$3,$4) RETURNING id",
                T, json.dumps({"phone": "+13025550142"}), f"sms-{s}-{key}", owner))
            try:
                await ca._execute_command_job(
                    {"command_type": "SMS", "command_id": command, "target": {"phone": "+13025550142"},
                     "draft": {"body": "Following up on your inquiry."}}, Reporter())
            except Exception as exc:  # noqa: BLE001 — the refusal surfaces as the job's error
                return command, exc
            return command, None

        await admin.execute("UPDATE tenant_messaging_campaigns SET status='rejected' WHERE id=$1", campaign)
        command, error = await text("revoked")
        assert sent == [] and error is not None and "texting campaign" in str(error)
        assert await admin.fetchval("SELECT state FROM command_executions WHERE id=$1::uuid", command) == "failed"

        await admin.execute("UPDATE tenant_messaging_campaigns SET status='approved', status_ok_at=now() "
                            "WHERE id=$1", campaign)
        command, error = await text("valid")
        assert error is None and sent == [NUMBER]
        assert await admin.fetchval("SELECT state FROM command_executions WHERE id=$1::uuid", command) == "succeeded"
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for tenant in (T, OTHER):
                for table in await admin.fetch(
                        "SELECT c.table_name FROM information_schema.columns c "
                        "JOIN information_schema.tables t USING (table_schema, table_name) "
                        "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                        "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                    await admin.execute(f'DELETE FROM "{table[0]}" WHERE tenant_id::text = $1', tenant)
                await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()
