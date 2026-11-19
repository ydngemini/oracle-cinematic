"""An inbound call its bridge never finalized gets its callback task from the
reconciliation sweep (audit 34.6), on real PostgreSQL: once, the right task
(do-not-contact when an opt-out was recorded mid-call), status no longer
'active'. Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_unfinalized_inbound_calls_are_recovered_once():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import contact_truth
    import inbound_voice
    from crypto import derive_tenant_key
    from db import connection

    tenant = str(uuid.uuid4())
    agent = f"agent-{tenant[:8]}@live.test"
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Recovery live')", tenant, f"rec-{tenant[:8]}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email) VALUES ($1,$2,'agent',$2)", tenant, agent)
        route = await admin.fetchval(
            "INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider, provider_account_id, "
            "forward_on_request, forward_when_ai_unavailable) VALUES ($1,$2,$3,'plivo','MArec',false,false) RETURNING id",
            tenant, agent, f"+1302555{uuid.uuid4().int % 10000:04d}")
        key = derive_tenant_key(tenant, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"])

        async def call(phone, *, ended_minutes_ago, opted_out=False):
            sid = f"CA{uuid.uuid4().hex}"
            await admin.execute(
                "INSERT INTO inbound_voice_calls (tenant_id, route_id, provider_call_sid, provider_status, direction, "
                "caller_phone_lookup_hash, intake_mode, caller_phone_ciphertext, disclosure_version, disclosed_at, "
                "transcript_status, opt_out_requested, started_at, ended_at) "
                "VALUES ($1,$2,$3,'completed','inbound',$4,'auto',pgp_sym_encrypt($5,$6),'v1',now(),'active',$7, "
                "now() - make_interval(mins => $8 + 5), now() - make_interval(mins => $8))",
                tenant, route, sid, contact_truth.lookup_hash(tenant, "phone", phone), phone, key, opted_out,
                ended_minutes_ago)
            return sid

        lost = await call("+13025550171", ended_minutes_ago=30)
        stopped = await call("+13025550172", ended_minutes_ago=30, opted_out=True)
        live = await call("+13025550173", ended_minutes_ago=2)  # just ended: its bridge may still finalize it

        # Through the 15-minute reconciliation sweep itself.
        import reconciliation

        first = (await reconciliation.run_sweep(only_tenant=tenant))["inbound_calls"]
        assert first == {"recovered": 2, "failed": 0}

        async def outcome(sid):
            return await admin.fetchrow(
                "SELECT c.transcript_status, c.handoff_status, t.title FROM inbound_voice_calls c "
                "LEFT JOIN client_tasks t ON t.id = c.callback_task_id WHERE c.provider_call_sid=$1", sid)

        lost_row, stopped_row, live_row = await outcome(lost), await outcome(stopped), await outcome(live)
        assert lost_row["transcript_status"] == "failed" and "without a saved transcript" in lost_row["title"]
        assert stopped_row["handoff_status"] == "do_not_contact" and "do-not-contact" in stopped_row["title"]
        assert live_row["transcript_status"] == "active" and live_row["title"] is None

        again = await inbound_voice.recover_unfinalized_calls(only_tenant=tenant)
        assert again == {"recovered": 0, "failed": 0}
        assert await admin.fetchval("SELECT count(*) FROM client_tasks WHERE tenant_id=$1", tenant) == 2

        # The bridge finalizes a call after the sweep picked it: the recovery
        # pass must not overwrite the saved transcript (34.6 review).
        raced = await call("+13025550174", ended_minutes_ago=30)
        state = {"direction": "inbound", "tenant_id": tenant, "agent_id": agent}
        await inbound_voice.finalize_inbound_voice_call(
            raced, [{"role": "caller", "text": "I would like a showing on Saturday"}], state)
        saved = await admin.fetchval("SELECT transcript_ciphertext FROM inbound_voice_calls WHERE provider_call_sid=$1", raced)
        await inbound_voice.finalize_inbound_voice_call(raced, [], state, recovered=True)
        row = await admin.fetchrow("SELECT transcript_status, transcript_ciphertext FROM inbound_voice_calls "
                                   "WHERE provider_call_sid=$1", raced)
        assert row["transcript_status"] == "complete" and row["transcript_ciphertext"] == saved is not None

        # No end recorded and started 3 hours ago: possibly still live, not recovered.
        silent = await call("+13025550175", ended_minutes_ago=0)
        await admin.execute("UPDATE inbound_voice_calls SET ended_at=NULL, started_at=now() - interval '3 hours' "
                            "WHERE provider_call_sid=$1", silent)
        assert (await inbound_voice.recover_unfinalized_calls(only_tenant=tenant))["recovered"] == 0
    finally:
        for sql in ("DELETE FROM inbound_voice_calls WHERE tenant_id=$1", "DELETE FROM client_tasks WHERE tenant_id=$1",
                    "DELETE FROM outreach_suppression WHERE tenant_id=$1", "DELETE FROM telephony_routes WHERE tenant_id=$1",
                    "DELETE FROM users WHERE tenant_id=$1", "DELETE FROM tenants WHERE id=$1"):
            try:
                await admin.execute(sql, tenant)
            except Exception:  # noqa: BLE001 - best-effort cleanup of a tenant this test made
                pass
        await admin.close()
        await connection.close_pool()
