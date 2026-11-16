"""A person deleted by request stays deleted after a backup restore (external
audit 29.4, second half), against real PostgreSQL.

A single-person deletion wrote no restore directive, so a restore from a
backup taken before it brought the person (and their texts) back, and nothing
deleted them again. Now the directive (row ids and keyed hashes, no customer
data) is saved and read back BEFORE anything is deleted, a request whose
directive cannot be saved deletes nothing, and reapply-erasures deletes a
restored person again.

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


def test_a_restored_person_is_deleted_again(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "c" * 64)
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "local")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)
    asyncio.run(_scenario(monkeypatch, tmp_path))


async def _scenario(monkeypatch, media_root):
    import asyncpg

    import object_storage
    import privacy_lifecycle as pl
    import privacy_requests
    from db import connection
    from tenancy import Role, TenantContext

    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    x, y, sms = (str(uuid.uuid4()) for _ in range(3))
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    try:
        async with admin.transaction():
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
                                T, f"restore-{s}", f"Brokerage {s}")
            await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                                "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, email, phone) VALUES "
                "($1,$3,$4,$5,'302-555-0171'), ($2,$3,$6,$7,'302-555-0172')",
                x, y, T, f"X {s}", f"x-{s}@client.test", f"Y {s}", f"y-{s}@client.test")
            await admin.execute(
                "INSERT INTO sms_messages (id, tenant_id, agent_id, provider, provider_message_id, direction, "
                "from_e164, to_e164, body, client_id) VALUES ($1,$2,$3,'telnyx',$4,'inbound','+13025550171','+13025550199',$5,$6)",
                sms, T, owner, f"msg-{s}", f"x-text {s}", x)
        octx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)

        async def delete(email):
            return await privacy_requests.handle_subject_request(
                octx, kind="dsr_delete", email=email, phone=None, reason="asked", preview=False)

        # Storage down: the request deletes nothing and says so.
        with monkeypatch.context() as m:
            m.setattr(object_storage, "is_configured", lambda: False)
            with pytest.raises(pl.LifecycleError, match="Nothing was deleted") as err:
                await delete(f"y-{s}@client.test")
        assert err.value.status_code == 503
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", y) == 1
        assert await admin.fetchval(
            "SELECT error FROM privacy_operations WHERE tenant_id=$1 AND kind='dsr_delete'", T) \
            == "restore_directive_unwritten"

        # Storage up: the directive is written first, then X is deleted.
        done = await delete(f"x-{s}@client.test")
        key = done["restore_directive"]
        assert key.startswith("privacy/subject-erasure-directives/")
        directive = json.loads((media_root / key).read_bytes())
        assert directive["ids"]["clients"] == [x] and directive["ids"]["sms_messages"] == [sms]
        assert directive["state"] == "committed" and done["restore_directive_state"] == "committed"
        assert f"x-{s}" not in json.dumps(directive) and "5550171" not in json.dumps(directive)  # no PII
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", x) == 0

        # Nothing to do before a restore.
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=False) if e.get("tenant_id") == T]
        assert [e["status"] for e in mine] == ["still_erased"]

        # A restore from an older backup brings X, their text and no tombstone back...
        await admin.execute("SET session_replication_role = replica")
        await admin.execute("INSERT INTO clients (id, tenant_id, full_name, email, phone) "
                            "VALUES ($1,$2,$3,$4,'302-555-0171')", x, T, f"X {s}", f"x-{s}@client.test")
        await admin.execute(
            "INSERT INTO sms_messages (id, tenant_id, agent_id, provider, provider_message_id, direction, "
            "from_e164, to_e164, body, client_id) VALUES ($1,$2,$3,'telnyx',$4,'inbound','+13025550171','+13025550199',$5,$6)",
            sms, T, owner, f"msg-{s}", f"x-text {s}", x)
        await admin.execute("DELETE FROM suppression_tombstones WHERE tenant_id=$1", T)
        await admin.execute("SET session_replication_role = origin")

        # A PENDING directive (a deletion that may never have committed) and an
        # expired one, both naming Y, who was never deleted, are never applied.
        from datetime import datetime, timedelta, timezone

        for name, extra in (("pending", {"state": "pending"}),
                            ("expired", {"state": "committed",
                                         "created_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()})):
            fake = {**directive, **extra, "operation_id": str(uuid.uuid4()), "ids": {"clients": [y]},
                    "tombstones": []}
            object_storage.put_bytes(f"{privacy_requests.SUBJECT_DIRECTIVES}{name}-{s}.json",
                                     json.dumps(fake).encode(), "application/json")

        # A legal hold on X blocks the re-deletion until it is released.
        await admin.execute("INSERT INTO legal_holds (tenant_id, scope, subject_ref, reason, placed_by) "
                            "VALUES ($1,'contact',$2,'litigation','counsel')", T, directive["subject_ref"])
        by = {e["directive"].rsplit("/", 1)[1]: e for e in
              await privacy_requests.reapply_subject_erasures(apply=True) if e.get("tenant_id") == T}
        assert by[f"pending-{s}.json"]["status"] == "needs_review"
        assert by[f"expired-{s}.json"]["status"] == "expired"
        assert by[key.rsplit("/", 1)[1]]["status"] == "skipped_legal_hold"
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id = ANY($1::uuid[])", [x, y]) == 2
        await admin.execute("UPDATE legal_holds SET released_at=now(), released_by='counsel' WHERE tenant_id=$1", T)

        # ...the brokerage-level pass does not touch it (it is not a brokerage erasure)...
        assert not [e for e in await pl.reapply_erasures(apply=False) if e.get("tenant_id") == T]
        # ...and the subject pass finds and deletes it again.
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=False) if e.get("tenant_id") == T]
        assert mine[0]["status"] == "resurrected" and mine[0]["rows_back"] == {"clients": 1, "sms_messages": 1}
        assert mine[0]["tombstones_missing"] >= 1
        mine = [e for e in await privacy_requests.reapply_subject_erasures(apply=True) if e.get("tenant_id") == T]
        assert mine[0]["status"] == "reapplied"
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", x) == 0
        assert await admin.fetchval("SELECT count(*) FROM sms_messages WHERE id=$1", sms) == 0
        assert await admin.fetchval("SELECT count(*) FROM suppression_tombstones WHERE tenant_id=$1", T) >= 1
        assert await admin.fetchval("SELECT count(*) FROM clients WHERE id=$1", y) == 1   # Y untouched
        assert await admin.fetchval(
            "SELECT state FROM privacy_operations WHERE id=$1::uuid", mine[0]["reapply_operation_id"]) == "succeeded"

        # Directives older than any usable backup are deleted; fresh ones kept.
        assert await privacy_requests.expire_subject_directives() >= 1
        left = object_storage.list_prefix(privacy_requests.SUBJECT_DIRECTIVES)
        assert f"{privacy_requests.SUBJECT_DIRECTIVES}expired-{s}.json" not in left and key in left
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
