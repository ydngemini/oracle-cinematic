"""Invitation emails are queued with the invitations and sent by a job, not
inside the request (external audit 21.5), against real PostgreSQL.

Before: a bulk invite sent each email in turn before answering (up to 40 s
each); the owner pressed Send again on a timeout and that re-invite revoked
the links already on their way.

Skipped unless ORACLE_LIVE_DB_ADMIN_DSN is set (see test_privacy_lifecycle_live).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_invitations_are_queued_and_sent_by_a_job(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import brokerage_onboarding as bo
    import config
    import smtp_mailer
    from db import connection
    from tenancy import Role, TenantContext

    if not os.environ.get("ORACLE_ENCRYPTION_MASTER_KEY"):
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "live-test-master-key-" + secrets.token_hex(16))
    monkeypatch.setattr(config, "IS_DEV", False)            # no dev capture: real queueing
    sent = []
    behaviour = {"fail": False}

    def slow_send(*, recipient, subject, text, html):
        time.sleep(1.0)                                     # a slow mail server
        if behaviour["fail"]:
            raise ConnectionRefusedError("smtp down")
        sent.append((recipient, text))

    monkeypatch.setattr(smtp_mailer, "send", slow_send)
    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    ctx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    emails = [f"agent{i}-{s}@live.test" for i in range(5)]

    async def invites():
        return await admin.fetch(
            "SELECT id, email, delivery_status, pending_link_ciphertext IS NOT NULL AS sealed, revoked_at, "
            "send_count FROM brokerage_invitations WHERE tenant_id=$1 ORDER BY email, created_at", T)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"iq-{s}", f"IQ {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)

        # Five invitations answer at once, whatever the mail server is doing.
        started = time.monotonic()
        out = await bo.post_invitations(bo.InviteCreate(emails=emails, role="agent"), ctx)
        assert time.monotonic() - started < 1.0 and sent == []
        assert sorted(out["queued"]) == sorted(emails) and len(out["created"]) == 5
        assert all(c["delivery_status"] == "queued" for c in out["created"])
        rows = await invites()
        assert all(r["sealed"] and r["delivery_status"] == "queued" for r in rows)
        jobs = await admin.fetch("SELECT payload FROM automation_jobs WHERE tenant_id=$1 AND job_type=$2",
                                 uuid.UUID(T), bo.INVITATION_JOB_TYPE)
        assert len(jobs) == 5                                # one durable job per invitation

        # The browser retries the slow request: nothing is revoked or reissued.
        again = await bo.post_invitations(bo.InviteCreate(emails=emails, role="agent"), ctx)
        assert not again["created"] and {k["reason"] for k in again["skipped"]} == {"already_invited"}
        assert [r["revoked_at"] for r in await invites()] == [None] * 5

        # The job sends the invitation with its link, records it, clears the seal.
        first = rows[0]
        assert (await bo.deliver_invitation_email(T, str(first["id"])))["status"] == "sent"
        import re

        assert sent and sent[0][0] == first["email"]
        token = re.search(r"accept-invite\?token=([A-Za-z0-9_\-]+)", sent[0][1]).group(1)
        # the emailed link is the invitation's own: its token hashes to the stored digest
        assert await admin.fetchval("SELECT token_hash FROM brokerage_invitations WHERE id=$1",
                                    first["id"]) == bo.hash_invitation_token(token)
        row = [r for r in await invites() if r["id"] == first["id"]][0]
        assert row["delivery_status"] == "sent" and not row["sealed"]
        # running again sends nothing more (the seal is gone)
        assert (await bo.deliver_invitation_email(T, str(first["id"])))["status"] == "skipped"
        assert len(sent) == 1

        # A failing mail server: recorded and raised, so the worker retries.
        behaviour["fail"] = True
        second = rows[1]
        with pytest.raises(ConnectionRefusedError):
            await bo.deliver_invitation_email(T, str(second["id"]))
        row = [r for r in await invites() if r["id"] == second["id"]][0]
        assert row["delivery_status"] == "failed" and row["sealed"]          # still sendable on retry
        behaviour["fail"] = False

        # A revoked invitation is not sent.
        third = rows[2]
        await bo.revoke_invitation(str(third["id"]), ctx)
        assert (await bo.deliver_invitation_email(T, str(third["id"])))["status"] == "skipped"
        assert len(sent) == 1

        # The explicit Resend does reissue (old link retired, new one queued).
        fourth = rows[3]
        resent = await bo.resend_invitation(str(fourth["id"]), ctx)
        assert resent["created"] and resent["queued"] == [fourth["email"]]
        mine = [r for r in await invites() if r["email"] == fourth["email"]]
        assert len(mine) == 2 and mine[0]["revoked_at"] is not None and mine[1]["sealed"]
        assert mine[1]["send_count"] == 2
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
