"""The property-data health components read real tables (L19-04), against
real PostgreSQL as the platform role (skipped without ORACLE_LIVE_DB_ADMIN_DSN)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_property_components_read_harvest_sources_and_market_stats(monkeypatch):
    monkeypatch.delenv("ORACLE_RECOVERY_MODE", raising=False)
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    import component_health as ch
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant = uuid.uuid4()
    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Health')", tenant, f"h-{tenant.hex[:8]}")
        await admin.execute(
            """INSERT INTO harvest_sources (tenant_id, source_key, display_name, jurisdiction, adapter,
                   schedule_seconds, enabled, circuit_state, last_succeeded_at)
               VALUES ($1,'health-open','Open circuit','DE','arcgis',86400,true,'open', now()),
                      ($1,'health-ok','Fresh','DE','arcgis',86400,true,'closed', now())""", tenant)
        import provider_health as ph

        os.environ["RENTCAST_API_KEY"] = "live-test-key"
        await admin.execute("DELETE FROM provider_health WHERE provider='rentcast'")
        await ph.record("rentcast", "credential_rejected")
        await ph.record("rentcast", "credential_rejected")
        row = await admin.fetchrow("SELECT * FROM provider_health WHERE provider='rentcast'")
        assert row["consecutive_failures"] == 2 and row["last_failure_kind"] == "credential_rejected"
        out = await ch._db_components()
        assert out["valuation_providers"]["state"] == ch.AUTH_FAILED, out["valuation_providers"]
        await ph.record("rentcast", ph.OK)
        row = await admin.fetchrow("SELECT * FROM provider_health WHERE provider='rentcast'")
        assert row["consecutive_failures"] == 0 and row["last_ok_at"] is not None
        out = await ch._db_components()
        assert out["valuation_providers"]["state"] == ch.HEALTHY
        harvest = out["property_harvesters"]
        assert harvest["state"] == ch.DEGRADED and harvest["detail"]["open_circuits"] >= 1, harvest
        assert out["market_data"]["state"] in (ch.NOT_CONFIGURED, ch.HEALTHY, ch.STALE), out["market_data"]
        assert set(ch._DB_KEYS) <= set(out)
    finally:
        os.environ.pop("RENTCAST_API_KEY", None)
        await admin.execute("DELETE FROM provider_health WHERE provider='rentcast'")
        await admin.execute("DELETE FROM harvest_sources WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()


def test_the_platform_twilio_account_records_texts_and_calls_apart():
    """0165 (L10-12): the platform Twilio outcomes land and read back through
    the app login, texts and calls on separate rows."""
    asyncio.run(_twilio_scenario())


async def _twilio_scenario():
    import asyncpg

    import provider_health as ph
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    names = [ph.PLATFORM_TWILIO_SMS, ph.PLATFORM_TWILIO_VOICE]
    try:
        await admin.execute("DELETE FROM provider_health WHERE provider = ANY($1::text[])", names)
        await ph.record(ph.PLATFORM_TWILIO_SMS, ph.OK)
        await ph.record(ph.PLATFORM_TWILIO_VOICE, "credential_rejected")
        sms = await ph.last_outcome(ph.PLATFORM_TWILIO_SMS)
        voice = await ph.last_outcome(ph.PLATFORM_TWILIO_VOICE)
        assert ph.state(True, sms) == "verified_by_send", sms
        assert ph.state(True, voice) == "failing", voice
        assert voice["last_failure_kind"] == "credential_rejected"
    finally:
        await admin.execute("DELETE FROM provider_health WHERE provider = ANY($1::text[])", names)
        await admin.close()
        await connection.close_pool()
