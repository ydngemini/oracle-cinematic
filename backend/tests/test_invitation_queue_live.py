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

        # A refusing mail server: recorded by type and raised, so the worker
        # retries; the seal stays for the retry, and goes on the last attempt.
        behaviour["fail"] = True
        second = rows[1]
        with pytest.raises(bo.InvitationDeliveryError) as exc:
            await bo.deliver_invitation_email(T, str(second["id"]))
        assert exc.value.job_terminal is False and str(exc.value) == "ConnectionRefusedError"
        row = [r for r in await invites() if r["id"] == second["id"]][0]
        assert row["delivery_status"] == "failed" and row["sealed"]
        with pytest.raises(bo.InvitationDeliveryError):
            await bo.deliver_invitation_email(T, str(second["id"]), last_attempt=True)
        row = [r for r in await invites() if r["id"] == second["id"]][0]
        assert row["delivery_status"] == "failed" and not row["sealed"]      # the job gave up: link gone
        behaviour["fail"] = False
        # a bulk Send now reissues the failed one (it never arrived)
        again = await bo.post_invitations(bo.InviteCreate(emails=[second["email"]], role="agent"), ctx)
        assert again["queued"] == [second["email"]]

        # A revoked invitation is not sent, and its sealed link is gone at once
        # (the trigger, whatever path revokes it).
        third = rows[2]
        await bo.revoke_invitation(str(third["id"]), ctx)
        assert not [r for r in await invites() if r["id"] == third["id"]][0]["sealed"]
        assert (await bo.deliver_invitation_email(T, str(third["id"])))["status"] == "skipped"
        assert len(sent) == 1
        # accepted: the consume function clears it too
        fifth = rows[4]
        token_hash = await admin.fetchval("SELECT token_hash FROM brokerage_invitations WHERE id=$1", fifth["id"])
        joiner = await admin.fetchval(
            "INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
            "VALUES ($1,$2,'agent',$2,'scrypt$x$y') RETURNING id", T, fifth["email"])
        await admin.fetchrow("SELECT * FROM consume_brokerage_invitation($1, $2)", token_hash, joiner)
        assert not [r for r in await invites() if r["id"] == fifth["id"]][0]["sealed"]
        # expired before it was sent: not sent, the seal cleared
        sixth = (await bo.post_invitations(bo.InviteCreate(emails=[f"late-{s}@live.test"], role="agent"),
                                           ctx))["created"][0]
        await admin.execute("UPDATE brokerage_invitations SET created_at = now() - interval '2 days', "
                            "expires_at = now() - interval '1 minute' "
                            "WHERE id=$1::uuid", sixth["id"])
        assert (await bo.deliver_invitation_email(T, str(sixth["id"])))["status"] == "expired"
        assert await admin.fetchval("SELECT pending_link_ciphertext IS NULL FROM brokerage_invitations "
                                    "WHERE id=$1::uuid", sixth["id"])

        # The explicit Resend does reissue (old link retired, new one queued).
        fourth = rows[3]
        resent = await bo.resend_invitation(str(fourth["id"]), ctx)
        assert resent["created"] and resent["queued"] == [fourth["email"]]
        mine = [r for r in await invites() if r["email"] == fourth["email"]]
        assert len(mine) == 2 and mine[0]["revoked_at"] is not None and mine[1]["sealed"]
        assert not mine[0]["sealed"]                       # the reissued one's link went with it
        assert mine[1]["send_count"] == 2
        jobs = await admin.fetch("SELECT queue_name FROM automation_jobs WHERE tenant_id=$1 AND job_type=$2",
                                 uuid.UUID(T), bo.INVITATION_JOB_TYPE)
        assert {j["queue_name"] for j in jobs} == {"default"}   # not the chat workers

        # A job that died without clearing its link (lease lost on the last
        # attempt): the janitor's sweep clears it; a live job's link is kept.
        await admin.execute("UPDATE automation_jobs SET state='dead_letter' WHERE tenant_id=$1 "
                            "AND idempotency_key = 'invitation-email:' || $2::text", uuid.UUID(T), str(mine[1]["id"]))
        live_one = [r for r in await invites() if r["sealed"] and r["id"] != mine[1]["id"]]
        assert live_one                                     # still has a running job
        assert await bo.sweep_stale_invitation_links() >= 1
        after = {r["id"]: r for r in await invites()}
        assert not after[mine[1]["id"]]["sealed"] and after[mine[1]["id"]]["delivery_status"] == "failed"
        assert all(after[r["id"]]["sealed"] for r in live_one)
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
