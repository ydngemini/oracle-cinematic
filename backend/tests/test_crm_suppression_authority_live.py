"""The outreach ledger is the one suppression authority (external audits
26.1, 26.9), against real PostgreSQL as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Before: a contact's CRM "do not contact" flags lived only in its JSON, which
the send gates never read; an inbound STOP in the ledger never reached the
nurture check, which read only the JSON; and a changed phone or email kept
the consent given for the old one.

Nothing is sent: a nurture reservation only queues a job.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import secrets
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_crm_flags_and_the_ledger_agree(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


def _daytime_zone() -> tuple[str, datetime]:
    """A zone where it is daytime now (nurture keeps quiet hours 20-08)."""
    now = datetime.now(timezone.utc)
    for name in ("UTC", "America/New_York", "America/Los_Angeles", "Asia/Tokyo", "Asia/Kolkata",
                 "Europe/Berlin", "Australia/Sydney", "Pacific/Honolulu"):
        local = now.astimezone(ZoneInfo(name))
        if 9 <= local.hour <= 18:
            return name, local
    raise AssertionError("no daytime zone found")


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import contacts_api
    from contacts_api import ContactCreate, ContactPatch, NurtureJobRequest
    from db import connection
    from outreach_compliance import Channel, ConsentLedger
    from tenancy import Role, TenantContext

    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    owner = f"owner-{tag}@crm.test"
    ctx = TenantContext(agent_id=owner, tenant_id=tenant, role=Role.BROKER_OWNER)
    zone, local = _daytime_zone()
    captured = datetime.now(timezone.utc).isoformat()
    sms_ok = {"sms": {"granted": True, "captured_at": captured, "source": "form"},
              "voice": {"granted": True, "captured_at": captured, "source": "form"}}

    def person(name: str, phone: str, **extra) -> ContactCreate:
        return ContactCreate(full_name=name, phone=phone, email=f"{name.lower()}-{tag}@crm.test", timezone=zone,
                             birthday_month=local.month, birthday_day=local.day, consent=sms_ok, **extra)

    async def ledger(contact: str) -> set[str]:
        return {r["channel"] for r in await admin.fetch(
            "SELECT channel FROM outreach_suppression WHERE tenant_id=$1 AND contact=$2 AND lifted_at IS NULL",
            uuid.UUID(tenant), contact)}

    async def gate_blocks(contact: str, channel: Channel) -> bool:
        async with connection.tenant_tx(ctx) as conn:
            state = await ConsentLedger.gather_state(conn, contact, channel, tenant_id=tenant)
        return bool(state["suppressed"])

    async def nurture(contact_id: str) -> str:
        out = await contacts_api.reserve_nurture_job(contact_id, NurtureJobRequest(event_type="birthday",
                                                                                   channel="sms"), ctx=ctx)
        return "created" if out.get("created") else out["decision"]["reason"]

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'CRM ledger')", tenant, f"crm-{tag}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash, is_active) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y',true)", tenant, owner)

        # ── a CRM do-not-contact reaches the send gates at once ────────────
        x = (await contacts_api.create_contact(person("Xan", "+13025550300"), ctx=ctx))["contact"]
        assert await ledger("+13025550300") == set() and not await gate_blocks("+13025550300", Channel.SMS)
        await contacts_api.update_contact(x["id"], ContactPatch(suppression={"dnc": True}), ctx=ctx)
        assert await ledger("+13025550300") == {"*"}
        assert await gate_blocks("+13025550300", Channel.SMS) and await gate_blocks("+13025550300", Channel.VOICE)
        # clearing the CRM flag does not lift the opt-out (the stricter decision stands)
        await contacts_api.update_contact(x["id"], ContactPatch(suppression={}), ctx=ctx)
        assert await gate_blocks("+13025550300", Channel.SMS)
        # flags set at creation reach the ledger too, per channel
        e = (await contacts_api.create_contact(person("Eve", "+13025550310", suppression={"email": True}), ctx=ctx))
        assert await ledger(f"eve-{tag}@crm.test") == {"email"} and await ledger("+13025550310") == set()

        # ── an inbound STOP in the ledger makes nurture ineligible ──────────
        z = (await contacts_api.create_contact(person("Zed", "+13025550301"), ctx=ctx))["contact"]
        w = (await contacts_api.create_contact(person("Wes", "(302) 555-0301"), ctx=ctx))["contact"]  # same phone
        v = (await contacts_api.create_contact(person("Val", "+13025550302"), ctx=ctx))["contact"]
        assert await nurture(v["id"]) == "created"                     # the control: eligible
        await ConsentLedger.suppress(ctx, contact="+13025550301", channel="sms", reason="stop_keyword")
        assert await nurture(z["id"]) == "suppressed"                  # before 26.1: created
        assert await nurture(w["id"]) == "suppressed"                  # one phone, one suppression
        assert e["contact"]["id"]

        # ── a changed destination does not carry the old consent (26.9) ────
        moved = (await contacts_api.update_contact(v["id"], ContactPatch(phone="+13025550399"), ctx=ctx))["contact"]
        assert moved["consent"]["sms"]["granted"] is False and moved["consent"]["voice"]["granted"] is False
        assert moved["consent"]["sms"]["source"] == "reset: destination changed"
        same = (await contacts_api.update_contact(x["id"], ContactPatch(phone="302.555.0300"), ctx=ctx))["contact"]
        assert same["consent"]["sms"]["granted"] is True               # the same number, reformatted
        mailed = (await contacts_api.update_contact(x["id"], ContactPatch(email=f"new-{tag}@crm.test"),
                                                     ctx=ctx))["contact"]
        assert mailed["consent"]["email"]["granted"] is False and mailed["consent"]["sms"]["granted"] is True
        # consent newly recorded for the new number in the same edit is kept
        fresh_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        fresh = {ch: {"granted": True, "captured_at": fresh_at, "source": "new form"} for ch in ("sms", "voice")}
        kept = (await contacts_api.update_contact(
            z["id"], ContactPatch(phone="+13025550398", consent=fresh), ctx=ctx))["contact"]
        assert kept["consent"]["sms"]["granted"] is True
        # a new number of a do-not-contact person is suppressed too
        await contacts_api.update_contact(x["id"], ContactPatch(suppression={"sms": True}), ctx=ctx)
        await contacts_api.update_contact(x["id"], ContactPatch(phone="+13025550397"), ctx=ctx)
        assert "sms" in await ledger("+13025550397")

        # ── review round 1 ────────────────────────────────────────────────
        # Concurrent edits (a new phone, a new do-not-contact) resolve to the
        # stricter outcome: the number finally stored is suppressed.
        for i in range(3):
            c = (await contacts_api.create_contact(person(f"Race{i}", f"+1302555060{i}"), ctx=ctx))["contact"]
            new_phone = f"+1302555070{i}"
            await asyncio.gather(
                contacts_api.update_contact(c["id"], ContactPatch(phone=new_phone), ctx=ctx),
                contacts_api.update_contact(c["id"], ContactPatch(suppression={"sms": True}), ctx=ctx))
            assert await gate_blocks(new_phone, Channel.SMS), i
        # The old consent object sent back with a new number does not carry over.
        r = (await contacts_api.create_contact(person("Ray", "+13025550310"), ctx=ctx))["contact"]
        back = (await contacts_api.update_contact(
            r["id"], ContactPatch(phone="+13025550311", consent=r["consent"]), ctx=ctx))["contact"]
        assert back["consent"]["sms"]["granted"] is False
        # A legacy (unsealed) contact: its client's phone is its destination,
        # and a STOP there makes its nurture ineligible.
        legacy_client, legacy_contact = uuid.uuid4(), uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, phone) "
                            "VALUES ($1,$2,'Old Client','+13025550320')", legacy_client, tenant)
        await admin.execute(
            "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state, legacy_client_id, "
            "consent, timezone, birthday_month, birthday_day, nurture_enabled) "
            "VALUES ($1,$2,$3,'legacy','pending_encryption',$4,$5::jsonb,$6,$7,$8,true)",
            legacy_contact, tenant, owner, legacy_client, _json.dumps(sms_ok), zone, local.month, local.day)
        await ConsentLedger.suppress(ctx, contact="+13025550320", channel="*", reason="stop_keyword")
        assert await nurture(str(legacy_contact)) == "suppressed"
        # A phone edited on the CLIENT record: the contact's flags follow, its
        # consent does not.
        import crm

        linked = uuid.uuid4()
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name) VALUES ($1,$2,'Linked')", linked, tenant)
        k = (await contacts_api.create_contact(person("Kai", "+13025550330", client_id=str(linked),
                                                      suppression={"dnc": True}), ctx=ctx))["contact"]
        await crm.update_client(str(linked), crm.ClientPatch(phone="+13025550331"), ctx=ctx)
        assert await gate_blocks("+13025550331", Channel.SMS)
        stored = await admin.fetchval("SELECT consent FROM agent_contacts WHERE id=$1", uuid.UUID(k["id"]))
        stored = _json.loads(stored) if isinstance(stored, str) else stored
        assert stored["sms"]["granted"] is False and stored["voice"]["granted"] is False
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
