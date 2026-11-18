"""10DLC brand and campaign status is read back from the provider (external
audit 25.2), against real PostgreSQL. The provider is a fake answering in
Telnyx's own vocabulary.

Before: the rows kept the status their submission answered; nothing read the
provider again, so an approved campaign stayed 'pending' (and since 25.1 its
texts were refused) and a suspended one stayed 'approved'.

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

NUMBER = "+13025550188"


class _NotFound(Exception):
    status_code = 404


class _Unauthorized(Exception):
    status_code = 401


def test_registration_status_follows_the_provider(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import messaging_api
    import messaging_data as md
    import messaging_provider
    from db import connection
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "telnyx")
    s = secrets.token_hex(5)
    T = str(uuid.uuid4())
    owner = f"owner-{s}@live.test"
    ctx = TenantContext(agent_id=owner, tenant_id=T, role=Role.BROKER_OWNER)
    brand_ref, campaign_ref = f"B{s}", f"C{s}"
    answers = {"brand": {"status": "REGISTRATION_PENDING", "identity_status": "UNVERIFIED"},
               "campaign": {"status": "TCR_PENDING"}}
    reads = []

    class _Adapter:
        async def get_brand_status(self, brand_id, *, credentials=None):
            reads.append(("brand", brand_id))
            answer = answers["brand"]
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)

        async def get_number_campaign(self, phone_number, *, credentials=None):
            reads.append(("number", phone_number))
            answer = answers.get("number", {"campaign_id": campaign_ref, "assignment_status": "ASSIGNED"})
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)

        async def get_campaign_status(self, campaign_id, *, credentials=None):
            reads.append(("campaign", campaign_id))
            answer = answers["campaign"]
            if isinstance(answer, Exception):
                raise answer
            if callable(answer):
                return await answer()
            return dict(answer)

    monkeypatch.setattr(messaging_provider, "get_messaging_provider", lambda _name: _Adapter())
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)

    KEY = {"api_key": "k"}
    ACCOUNT = md.account_fingerprint(KEY)
    creds_in_use = {"value": KEY}

    async def refresh(**kw):
        return await md.refresh_registration_status(ctx, provider="telnyx", credentials=creds_in_use["value"], **kw)

    async def campaign_row():
        return await admin.fetchrow(
            "SELECT status, provider_status, failure_reason, status_check_error, approved_at, status_checked_at, status_ok_at "
            "FROM tenant_messaging_campaigns WHERE tenant_id=$1", T)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", T, f"ds-{s}", f"DS {s}")
        await admin.execute("INSERT INTO users (tenant_id, agent_id, role, email, password_hash) "
                            "VALUES ($1,$2,'broker_owner',$2,'scrypt$x$y')", T, owner)
        # registered through this account (recorded at registration)
        brand = await admin.fetchval(
            "INSERT INTO tenant_messaging_brands (tenant_id, provider, provider_brand_id, status, submitted_at, "
            "provider_account_id) VALUES ($1,'telnyx',$2,'pending',now(),$3) RETURNING id", T, brand_ref, ACCOUNT)
        await admin.execute(
            "INSERT INTO tenant_messaging_campaigns (tenant_id, brand_id, provider, provider_campaign_id, status, "
            "submitted_at, provider_account_id) VALUES ($1,$2,'telnyx',$3,'pending',now(),$4)",
            T, brand, campaign_ref, ACCOUNT)

        # Still pending at the provider: nothing moves upward.
        out = await refresh()
        assert out["campaigns"][0]["status"] == "pending" and out["brand"]["status"] == "pending"
        assert (await campaign_row())["provider_status"] == "TCR_PENDING"
        # An accepted-but-not-provisioned campaign is not approved.
        answers["campaign"] = {"status": "MNO_ACCEPTED"}
        await refresh()
        assert (await campaign_row())["status"] == "pending"

        # Pending -> approved (provisioned at the carriers); brand OK/VERIFIED.
        answers["brand"] = {"status": "OK", "identity_status": "VERIFIED"}
        answers["campaign"] = {"status": "MNO_PROVISIONED"}
        assert tid_in(await md.registrations_to_sync(limit=1000), T)
        out = await refresh()
        row = await campaign_row()
        assert row["status"] == "approved" and row["approved_at"] is not None and row["status_check_error"] is None
        assert out["brand"]["status"] == "verified"

        # A polling failure changes no status and is recorded.
        answers["campaign"] = TimeoutError("read timed out")
        await refresh()
        row = await campaign_row()
        assert row["status"] == "approved" and "TimeoutError" in row["status_check_error"]
        # An unrecognised value changes nothing either (never a guess).
        answers["campaign"] = {"status": "SOMETHING_NEW"}
        await refresh()
        row = await campaign_row()
        assert row["status"] == "approved" and "unrecognised" in row["status_check_error"]

        # Two owners racing: the read sent later wins even if it answers first.
        release = asyncio.Event()

        async def slow_old_answer():
            await release.wait()
            return {"status": "MNO_PROVISIONED"}

        answers["campaign"] = slow_old_answer
        stale = asyncio.ensure_future(refresh())
        await asyncio.sleep(0.05)
        answers["campaign"] = {"status": "TCR_SUSPENDED", "failure_reason": "carrier audit"}
        await refresh()
        release.set()
        await stale
        row = await campaign_row()
        assert row["status"] == "suspended" and row["failure_reason"] == "carrier audit"
        assert await admin.fetchval("SELECT count(*) FROM tenant_messaging_campaigns WHERE tenant_id=$1", T) == 1

        # Rejected after an appeal-free review, with its reason.
        answers["campaign"] = {"status": "TELNYX_FAILED", "failure_reason": "opt-in not described"}
        await refresh()
        assert tuple((await campaign_row())[:3]) == ("rejected", "TELNYX_FAILED", "opt-in not described")

        # Provisioned but suspended by T-Mobile: suspended, not approved.
        answers["campaign"] = {"status": "MNO_PROVISIONED", "t_mobile_suspended": True}
        await refresh()
        assert (await campaign_row())["status"] == "suspended"

        # A cancelled account / revoked key answers 401: nothing changes, the
        # error is recorded, and status_ok_at stops advancing (the send gate
        # refuses once the last good read is 14 days old).
        answers["campaign"] = {"status": "MNO_PROVISIONED"}
        await refresh()
        ok_before = (await campaign_row())["status_ok_at"]
        answers["campaign"] = _Unauthorized("401 Unauthorized")
        await refresh()
        row = await campaign_row()
        assert row["status"] == "approved" and "Unauthorized" in row["status_check_error"]
        assert row["status_ok_at"] == ok_before and row["status_checked_at"] > ok_before

        # A "not found" from ANOTHER account (switched or fallback credential)
        # is not evidence the campaign is gone: nothing changes.
        creds_in_use["value"] = {"api_key": "a-different-account"}
        answers["campaign"] = _NotFound("campaign not found")
        await refresh()
        row = await campaign_row()
        assert row["status"] == "approved" and "not the one holding it" in row["status_check_error"]
        # From the account that holds it: expired, never left approved.
        creds_in_use["value"] = KEY
        await refresh()
        row = await campaign_row()
        assert row["status"] == "expired" and row["provider_status"] == "NOT_FOUND"
        # ...and still swept, so a transient account problem recovers.
        assert T in await md.registrations_to_sync(limit=1000)
        answers["campaign"] = {"status": "MNO_PROVISIONED"}
        await refresh()
        assert (await campaign_row())["status"] == "approved"
        answers["campaign"] = _NotFound("campaign not found")
        await refresh()

        # A registration the provider confirmed but that never reached a row is
        # counted for an operator, not attached by the refresh.
        await admin.execute(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref, detail) "
            "VALUES ($1,$2,'telnyx','10dlc_campaign','confirmed','C-orphan','{}'::jsonb)", T, owner)
        out = await refresh()
        assert out["unattached_submissions"] >= 1
        assert await admin.fetchval("SELECT count(*) FROM tenant_messaging_campaigns WHERE tenant_id=$1", T) == 1

        # A provider event only nudges a re-read of the matching brokerage.
        answers["campaign"] = {"status": "MNO_PROVISIONED"}
        reads.clear()

        async def creds(_ctx):
            return {"api_key": "k"}

        monkeypatch.setattr(messaging_api, "_telnyx_credentials", creds)
        # a nudge right after a read is throttled (one read a minute per brokerage)
        await messaging_api._refresh_for_provider_id(campaign_ref)
        assert reads == []
        for table in ("tenant_messaging_campaigns", "tenant_messaging_brands"):
            await admin.execute(f"UPDATE {table} SET status_checked_at = now() - interval '2 minutes' "
                                "WHERE tenant_id=$1", T)
        await messaging_api._refresh_for_provider_id(campaign_ref)
        assert ("campaign", campaign_ref) in reads and (await campaign_row())["status"] == "approved"
        reads.clear()
        await messaging_api._refresh_for_provider_id("C-unknown")
        assert reads == []

        # The periodic sweep reads it too, and one brokerage's broken
        # credential is recorded without stopping the sweep.
        result = await messaging_api.sync_open_registrations(limit=1000)
        assert result["synced"] >= 1

        async def broken(_ctx):
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="Stored Telnyx credential is invalid.")

        monkeypatch.setattr(messaging_api, "_telnyx_credentials", broken)
        result = await messaging_api.sync_open_registrations(limit=1000)
        assert result["failed"] >= 1

        # A committed assignment follows the provider: a read failure changes
        # nothing; the number moved off the campaign releases it (so the send
        # gate refuses it), as does the number no longer being assigned.
        monkeypatch.setattr(messaging_api, "_telnyx_credentials", creds)
        assignment = await admin.fetchval(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref, detail) "
            "VALUES ($1,$2,'telnyx','10dlc_number_assignment','attached','PN1',$3::jsonb) RETURNING id",
            T, owner, json.dumps({"campaign_id": campaign_ref, "phone_number": NUMBER}))

        async def state():
            return await admin.fetchval("SELECT state FROM provider_purchases WHERE id=$1", assignment)

        async def status():
            return await admin.fetchval("SELECT detail->>'assignment_status' FROM provider_purchases "
                                        "WHERE id=$1", assignment)

        async def reattach():
            await admin.execute("UPDATE provider_purchases SET state='attached' WHERE id=$1", assignment)

        answers["number"] = TimeoutError("slow")
        await refresh()
        assert await state() == "attached" and await status() is None      # not yet ASSIGNED: not sendable
        answers["number"] = {"campaign_id": campaign_ref, "assignment_status": "PENDING_ASSIGNMENT"}
        await refresh()
        assert await state() == "attached" and await status() == "PENDING_ASSIGNMENT"
        answers["number"] = {"campaign_id": campaign_ref, "assignment_status": "ASSIGNED"}
        await refresh()
        assert await state() == "attached" and await status() == "ASSIGNED"
        answers["number"] = {"campaign_id": campaign_ref, "assignment_status": "FAILED_ASSIGNMENT"}
        await refresh()
        assert await state() == "released"
        await reattach()
        answers["number"] = {"campaign_id": campaign_ref, "assignment_status": "PENDING_UNASSIGNMENT"}
        await refresh()
        assert await state() == "released"
        await reattach()
        answers["number"] = {"campaign_id": "C-someone-else", "assignment_status": "ASSIGNED"}
        out = await refresh()
        assert await state() == "released" and out["assignments"][0]["released"]
        await reattach()
        answers["number"] = {"campaign_id": None, "assignment_status": ""}
        await refresh()
        assert await state() == "released"
        await reattach()
        # a not-found from another account releases nothing; from the holder it does
        await admin.execute(
            "INSERT INTO messaging_routes (tenant_id, agent_id, provider, provider_account_id, hosted_order_status, "
            "active) VALUES ($1,$2,'telnyx',$3,'active',true)", T, owner, ACCOUNT)
        answers["number"] = _NotFound("no assignment")
        creds_in_use["value"] = {"api_key": "a-different-account"}
        await refresh()
        assert await state() == "attached"
        creds_in_use["value"] = KEY
        await refresh()
        assert await state() == "released"

        # Owner refresh is throttled: a second refresh within the window reads nothing.
        reads.clear()
        await refresh()
        reads.clear()
        throttled = await refresh(min_interval_s=30)
        assert throttled["throttled"] and reads == []

        # Approval read back makes the number assignable (_CAMPAIGN_FOR_ASSIGNMENT_SQL).
        assert await admin.fetchval(md._CAMPAIGN_FOR_ASSIGNMENT_SQL.replace("FOR UPDATE OF c", ""), T) is not None
        assert json.dumps(reads) is not None
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


def tid_in(tenants, tenant):
    return tenant in tenants
