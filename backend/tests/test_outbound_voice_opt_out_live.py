"""A spoken outbound opt-out is a real suppression the outreach gate reads
(voice_opt_out, L09-05), against real PostgreSQL as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_an_outbound_opt_out_blocks_the_next_contact():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import voice_opt_out
    from db import connection
    from outreach_compliance import Channel, ConsentLedger, normalize_contact
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    callee = "(302) 555-0177"
    agent = TenantContext(agent_id="agent@optout.test", tenant_id=str(tenant), role=Role.AGENT)
    stranger = TenantContext(agent_id="agent@other.test", tenant_id=str(other), role=Role.AGENT)
    contact = normalize_contact(callee, Channel.VOICE)
    try:
        for t in (tenant, other):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Opt-out')", t, f"oo-{t.hex[:8]}")
        state = {"direction": "outbound", "tenant_id": str(tenant), "callee": callee}
        await voice_opt_out.record_outbound_opt_out(state)
        await voice_opt_out.record_outbound_opt_out(state)  # idempotent
        rows = await admin.fetch(
            "SELECT channel, reason, source_text FROM outreach_suppression WHERE tenant_id=$1", tenant)
        assert [dict(r) for r in rows] == [{"channel": "*", "reason": "stop_keyword",
                                            "source_text": voice_opt_out.SOURCE_TEXT}]
        for channel in (Channel.VOICE, Channel.SMS, Channel.EMAIL):
            async with connection.tenant_tx(agent) as conn:
                facts = await ConsentLedger.gather_state(conn, contact, channel)
            assert facts["suppressed"] is True, channel
        async with connection.tenant_tx(stranger) as conn:
            assert (await ConsentLedger.gather_state(conn, contact, Channel.VOICE))["suppressed"] is False
        with pytest.raises(voice_opt_out.OptOutNotRecorded):
            await voice_opt_out.record_outbound_opt_out({"direction": "outbound", "tenant_id": str(tenant)})
    finally:
        for t in (tenant, other):
            await admin.execute("DELETE FROM outreach_suppression WHERE tenant_id=$1", t)
            await admin.execute("DELETE FROM tenants WHERE id=$1", t)
        await admin.close()
        await connection.close_pool()
