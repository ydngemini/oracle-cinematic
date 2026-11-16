"""A data-subject request reads or deletes one person, completely (external
audit 29.3, 29.8), against real PostgreSQL.

29.3: discovery ORed the identifiers, so A's email with B's phone returned, or
deleted, both people's records. Now any two identifiers that reach different
records are refused (409 ambiguous_subject) before anything is read or deleted.

29.8: the access bundle capped each table, swallowed a failed query, skipped
encrypted records without the key and never returned the dependent tables it
had counted (intake sessions, attempt logs, ...). Now it returns every table
it found, sealed fields opened, or fails the request.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

MASTER = "e" * 64


def test_one_person_completely_or_nothing(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import privacy_lifecycle as pl
    import privacy_requests
    from crypto import derive_tenant_key
    from db import connection
    from tenancy import Role, TenantContext

    s = "DSAR-" + secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test".lower()
    x, y, kx = (str(uuid.uuid4()) for _ in range(3))
    key = derive_tenant_key(T, MASTER)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"dsar-{s}".lower(), f"Brokerage {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, email, phone) VALUES "
                "($1,$3,$4,$5,'302-555-0161'), ($2,$3,$6,$7,'302-555-0162')",
                x, y, T, f"X {s}", f"x-{s}@client.test", f"Y {s}", f"y-{s}@client.test")
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, legacy_client_id, pii_ciphertext) "
                "VALUES ($1,$2,$3,$4, pgp_sym_encrypt($5,$6))",
                kx, T, owner, x, json.dumps({"full_name": f"kx-{s}"}), key)
            await admin.execute("UPDATE clients SET contact_id=$1 WHERE id=$2", kx, x)
            sealed = "pgp_sym_encrypt($4, $5)"
            await admin.execute(
                "INSERT INTO contact_intake_sessions (tenant_id, contact_id, client_id, persona, "
                "raw_answers_ciphertext, normalized_fields_ciphertext, transcript_ciphertext, created_by) "
                f"VALUES ($1,$2,$3,'buyer',{sealed},{sealed},{sealed},'x')",
                T, kx, x, json.dumps({"budget": f"answer-{s}"}), key)
            await admin.execute("INSERT INTO outreach_attempt_log (tenant_id, contact, channel, allowed) "
                                "VALUES ($1,'+13025550161','sms',true)", T)

        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        async def ask(kind="dsr_access", preview=False, **ids):
            args = {"email": None, "phone": None, **ids}
            return await privacy_requests.handle_subject_request(
                octx, kind=kind, reason="subject asked", preview=preview, **args)

        async def ops():
            return await admin.fetchval("SELECT count(*) FROM privacy_operations WHERE tenant_id=$1", T)

        # ── 29.3: X's email with Y's phone (or Y's record id) is refused ───
        for mixed in ({"email": f"x-{s}@client.test", "phone": "(302) 555-0162"},
                      {"email": f"x-{s}@client.test", "client_id": y}):
            for kind, preview in (("dsr_access", True), ("dsr_access", False), ("dsr_delete", False)):
                with pytest.raises(pl.LifecycleError, match="ambiguous_subject") as err:
                    await ask(kind, preview, **mixed)
                assert err.value.status_code == 409
        assert await ops() == 0                                     # nothing was started
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id = ANY($1::uuid[])", [x, y]) == 2

        # identifiers of the same person still work together
        found = await ask(email=f"x-{s}@client.test", phone="302.555.0161", contact_id=kx)
        blob = json.dumps(found["records"])
        assert f"y-{s}" not in blob and f"Y {s}" not in blob
        # ── 29.8: every table found is returned, sealed fields opened ─────
        assert set(found["matches"]) <= set(found["records"])
        session = found["records"]["contact_intake_sessions"][0]
        assert f"answer-{s}" in session["raw_answers"]
        assert "raw_answers_ciphertext" not in session
        assert found["records"]["outreach_attempt_log"][0]["contact"] == "+13025550161"
        assert f"kx-{s}" in blob

        # a table that cannot be read fails the request, and its operation is
        # not left running or marked succeeded
        async def unreadable(*_a, **_k):
            raise RuntimeError("column renamed")

        monkeypatch.setattr(privacy_requests, "_generic_rows", unreadable)
        with pytest.raises(RuntimeError):
            await ask(email=f"x-{s}@client.test")
        assert await admin.fetchval(
            "SELECT state FROM privacy_operations WHERE tenant_id=$1 AND kind='dsr_access' "
            "ORDER BY requested_at DESC LIMIT 1", T) == "failed"

        # without the key nothing is read and nothing is left running
        monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY")
        with pytest.raises(Exception):
            await ask(email=f"x-{s}@client.test")
        assert await admin.fetchval("SELECT count(*) FROM privacy_operations WHERE tenant_id=$1 "
                                    "AND state='running'", T) == 0
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for table, ttype in await admin.fetch(
                    "SELECT c.table_name, c.udt_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema='public' AND c.column_name='tenant_id' "
                    "AND t.table_type='BASE TABLE' AND c.table_name <> 'tenants'"):
                await admin.execute(f'DELETE FROM "{table}" WHERE tenant_id::text = $1', T)
            await admin.execute("DELETE FROM tenants WHERE id=$1", T)
        finally:
            await admin.execute("SET session_replication_role = origin")
        await admin.close()
        await connection.close_pool()

