"""A broker owner's decision that a public record is a linked client's house
(T11.03, F11.04, 0168), against real PostgreSQL: FORCE RLS, the application
login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Actors: the broker owner, agent A (the client's assignee), agent B (cannot see
A's client), agent C (A's client shared with C to VIEW), the broker owner of
another brokerage and a platform admin working from that other brokerage.
One house (a lead on parcel P, linked to A's client) and two public records on
P; a second house for the lead cascade.

The table is append-only: no UPDATE or DELETE for the application (and an
UPDATE is refused for everyone by trigger), one first decision per pair, one
successor per decision, a successor only within its own pair. Who decided, in
what role and when is stamped by trigger from the session, whatever the INSERT
says. Reading follows the lead (0143), and writing needs the lead's client to
be editable. The reason is at rest only sealed, under the lead tenant's key.
Through the routes: the owner confirms and the Houses detail then reads
verified; a re-link to another client makes it out of date; a platform admin's
decision on another tenant's house is sealed and audited in that tenant.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

REASON = "Deed of record and tax bill both name the client for this parcel"
OTHER_REASON = "Assessor parcel belongs to a different owner of record entirely"
MASTER = "live-test-record-match-master-key-0123456789abcdef"
SEALED = "pgp_sym_encrypt('direct insert reason', $1::text, 's2k-mode=1')"


def test_a_broker_owner_decides_a_linked_houses_record(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


def test_the_table_carries_only_the_wall_and_the_lead_rule():
    asyncio.run(_policies())


async def _policies():
    """One permissive tenant wall, a restrictive lead-follow read and a
    restrictive client-edit insert; no UPDATE or DELETE policy, and the
    application holds neither privilege."""
    import asyncpg

    admin = await asyncpg.connect(DSN)
    try:
        rows = await admin.fetch(
            "SELECT policyname, permissive, cmd FROM pg_policies "
            "WHERE schemaname='public' AND tablename='property_match_decisions'")
        flags = await admin.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='property_match_decisions'::regclass")
        privileges = await admin.fetchrow(
            "SELECT has_table_privilege('oracle_app','property_match_decisions','SELECT') AS s, "
            "has_table_privilege('oracle_app','property_match_decisions','INSERT') AS i, "
            "has_table_privilege('oracle_app','property_match_decisions','UPDATE') AS u, "
            "has_table_privilege('oracle_app','property_match_decisions','DELETE') AS d, "
            "has_table_privilege('oracle_app','property_match_decisions','TRUNCATE') AS t")
    finally:
        await admin.close()
    assert {(r["policyname"], r["permissive"], r["cmd"]) for r in rows} == {
        ("property_match_decisions_tenant_isolation", "PERMISSIVE", "ALL"),
        ("property_match_decisions_lead_read", "RESTRICTIVE", "SELECT"),
        ("property_match_decisions_lead_insert", "RESTRICTIVE", "INSERT"),
    }
    assert tuple(flags) == (True, True)
    assert tuple(privileges) == (True, True, False, False, False)


async def _scenario(monkeypatch):  # noqa: C901 — one scenario, read top to bottom
    import asyncpg
    from fastapi import HTTPException

    import mls_portal
    import property_match_api
    import sealed_text
    from db import connection
    from tenancy import Role, TenantContext

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other = uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@match.test" for k in users}
    client_a, client_x = uuid.uuid4(), uuid.uuid4()
    contact_a, contact_x = uuid.uuid4(), uuid.uuid4()
    lead, lead_two = uuid.uuid4(), uuid.uuid4()
    record, record_two, record_far = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    parcel, parcel_two = f"PMD-{tag}", f"PMD2-{tag}"
    refreshed = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who in ("owner", "d") else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=str(other if who == "d" else tenant), role=role)

    # An operator whose context tenant is `other`, deciding `tenant`'s house.
    operator = TenantContext(agent_id=f"ops-{tag}@match.test", tenant_id=str(other), role=Role.PLATFORM_ADMIN)

    def body(decision, *, record_id=record, supersedes=None, evidence=("parcel_number_checked", "deed_or_title"),
             reason=REASON):
        return property_match_api.RecordMatchDecision(
            public_record_id=str(record_id), decision=decision, evidence=list(evidence), reason=reason,
            supersedes_id=str(supersedes) if supersedes else None)

    async def detail(who, record_id=record):
        c = operator if who == "operator" else ctx(who)
        return await mls_portal.mls_pipeline_listing(record_id=str(record_id), ctx=c)

    async def seen(who) -> set:
        async with connection.tenant_tx(ctx(who)) as conn:
            rows = await conn.fetch("SELECT id FROM property_match_decisions WHERE lead_id = $1", lead)
        return {r["id"] for r in rows}

    async def insert(conn, *, lead_id=lead, record_id=record, client=client_a, decision="withdrawn",
                     supersedes=None, extra_cols="", extra_vals=""):
        return await conn.fetchval(
            "INSERT INTO property_match_decisions (tenant_id, lead_id, public_record_id, client_id, decision, "
            f"supersedes_id, reason_ciphertext{extra_cols}) VALUES ($2,$3,$4,$5,$6,$7,{SEALED}{extra_vals}) "
            "RETURNING id",
            sealed_text.key_for(tenant), tenant, lead_id, record_id, client, decision, supersedes)

    async def opened(decision_id, tid):
        return await admin.fetchval(
            "SELECT pgp_sym_decrypt(reason_ciphertext, $2) FROM property_match_decisions WHERE id=$1",
            decision_id, sealed_text.key_for(tid))

    try:
        # ── fixtures (superuser, outside RLS) ──────────────────────────────
        for tid, slug in ((tenant, "match"), (other, "match-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Record match')",
                                tid, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, other if who == "d" else tenant, agent[who],
                "broker_owner" if who in ("owner", "d") else "agent", f"Agent {who.upper()}")
        for client, contact, name in ((client_a, contact_a, "Match A"), (client_x, contact_x, "Match X")):
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption')", contact, tenant, agent["a"])
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, contact_id) VALUES ($1,$2,$3,$4,$5)",
                client, tenant, name, agent["a"], contact)
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, client_a, users["c"], agent["a"])
        for rid, pid, source_record in ((record, parcel, "1"), (record_two, parcel, "2"),
                                        (record_far, parcel_two, "3")):
            await admin.execute(
                "INSERT INTO public_property_records (id, source_key, source_record_id, parcel_id, state, "
                "address, source_name, coverage_scope, record_refreshed_at) "
                "VALUES ($1,$2,$3,$4,'DE',$5,'Live test parcels','county:test',$6)",
                rid, f"pmd-live-{tag}", source_record, pid, f"{tag} Match Street", refreshed)
        for lid, pid in ((lead, parcel), (lead_two, parcel_two)):
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", lid, tenant, pid, client_a, f"{tag} Match Street")

        # The application login is held by RLS: it is no superuser and cannot bypass it.
        async with connection.tenant_tx(ctx("owner")) as conn:
            assert await conn.fetchval(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user") is False

        # ── the route: an agent is refused, the owner confirms ────────────
        with pytest.raises(HTTPException) as refused:
            await property_match_api.decide_record_match(lead_id=lead, body=body("confirmed"), ctx=ctx("a"))
        assert refused.value.status_code == 403
        assert await admin.fetchval("SELECT count(*) FROM property_match_decisions WHERE lead_id=$1", lead) == 0

        out = await property_match_api.decide_record_match(lead_id=lead, body=body("confirmed"), ctx=ctx("owner"))
        first = uuid.UUID(out["decision_id"])
        assert out["identity"]["verified"] is True
        row = await admin.fetchrow("SELECT * FROM property_match_decisions WHERE id=$1", first)
        assert (row["tenant_id"], row["lead_id"], row["public_record_id"], row["client_id"]) == (
            tenant, lead, record, client_a)
        assert (row["decision"], row["supersedes_id"], row["pairing"]) == ("confirmed", None, "parcel_key")
        assert row["evidence"] == ["parcel_number_checked", "deed_or_title"]
        assert row["record_refreshed_at"] == refreshed
        assert (row["decided_by"], row["decided_role"]) == (agent["owner"], "broker_owner")
        # The reason is at rest only sealed, under this tenant's key, and no other.
        assert await opened(first, tenant) == REASON
        with pytest.raises(asyncpg.PostgresError):
            await opened(first, other)
        assert not await admin.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='property_match_decisions' AND data_type IN ('text','character varying') "
            "AND column_name LIKE '%reason%')")

        # The Houses detail now reads verified, the search claim untouched.
        owner_view = await detail("owner")
        assert owner_view["lead_id"] == str(lead)
        assert owner_view["identity"]["status"] == "not_applicable"
        assert owner_view["identity"]["verified"] is True
        assert owner_view["identity"]["confirmation"]["state"] == "confirmed"
        assert owner_view["identity"]["confirmable"] is True
        agent_view = await detail("a")
        assert (agent_view["identity"]["verified"], agent_view["identity"]["confirmable"]) == (True, False)
        # The other record on the same parcel is not decided by this one.
        sibling = await detail("owner", record_two)
        assert sibling["identity"]["verified"] is False
        assert sibling["identity"]["confirmation"]["state"] == "none"

        # ── who reads the decisions: they follow the lead (0143) ──────────
        assert await seen("owner") == {first}
        assert await seen("a") == {first}
        assert await seen("c") == {first}          # a view share sees the house
        assert await seen("b") == set()            # cannot see A's client, so not its house
        assert await seen("d") == set()            # another brokerage's owner
        unseen = await detail("b")
        assert unseen["lead_id"] is None and "confirmation" not in unseen["identity"]

        # ── the database floor ────────────────────────────────────────────
        # A view-only share cannot record a decision, whatever the route allows.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with connection.tenant_tx(ctx("c")) as conn:
                await insert(conn, record_id=record_two, decision="rejected")
        # Append-only: no UPDATE or DELETE for the application ...
        for statement in ("UPDATE property_match_decisions SET decision='rejected' WHERE id=$1",
                          "DELETE FROM property_match_decisions WHERE id=$1"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with connection.tenant_tx(ctx("owner")) as conn:
                    await conn.execute(statement, first)
        # ... and an UPDATE is refused for everyone, by trigger.
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await admin.execute("UPDATE property_match_decisions SET decision='rejected' WHERE id=$1", first)
        assert await admin.fetchval("SELECT decision FROM property_match_decisions WHERE id=$1", first) == "confirmed"

        # The actor, role and time come from the session, never the INSERT.
        async with connection.tenant_tx(ctx("owner")) as conn:
            withdrawn = await insert(
                conn, supersedes=first, extra_cols=", decided_by, decided_role, decided_at",
                extra_vals=", 'forged@elsewhere.test', 'platform_admin', '2001-01-01'")
        stamped = await admin.fetchrow(
            "SELECT decided_by, decided_role, decided_at FROM property_match_decisions WHERE id=$1", withdrawn)
        assert (stamped["decided_by"], stamped["decided_role"]) == (agent["owner"], "broker_owner")
        assert stamped["decided_at"].year >= 2026
        # One successor per decision, one first decision per pair.
        with pytest.raises(asyncpg.UniqueViolationError, match="uq_pmd_successor"):
            async with connection.tenant_tx(ctx("owner")) as conn:
                await insert(conn, supersedes=first, decision="rejected")
        with pytest.raises(asyncpg.UniqueViolationError, match="uq_pmd_first"):
            async with connection.tenant_tx(ctx("owner")) as conn:
                await insert(conn, decision="rejected")
        # A successor names a decision of its own pair, never another's.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.tenant_tx(ctx("owner")) as conn:
                await insert(conn, record_id=record_two, supersedes=withdrawn, decision="rejected")
        # Without a session actor nothing is recorded.
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="app.current_agent"):
            async with connection.tenant_tx(ctx("owner")) as conn:
                await conn.execute("SELECT set_config('app.current_agent', '', true)")
                await insert(conn, record_id=record_two, decision="rejected")

        # ── reversal through the route, then a client re-link ─────────────
        with pytest.raises(HTTPException) as stale:
            await property_match_api.decide_record_match(
                lead_id=lead, body=body("confirmed", supersedes=first), ctx=ctx("owner"))
        assert stale.value.status_code == 409
        reconfirmed = await property_match_api.decide_record_match(
            lead_id=lead, body=body("confirmed", supersedes=withdrawn), ctx=ctx("owner"))
        assert (await detail("owner"))["identity"]["verified"] is True

        await admin.execute("UPDATE leads SET seller_client_id=$2 WHERE id=$1", lead, client_x)
        drifted = (await detail("owner"))["identity"]
        assert drifted["verified"] is False
        assert drifted["confirmation"] == {
            "state": "out_of_date", "head_id": reconfirmed["decision_id"],
            "decided_at": drifted["confirmation"]["decided_at"]}
        current = await property_match_api.decide_record_match(
            lead_id=lead, body=body("confirmed", supersedes=reconfirmed["decision_id"]), ctx=ctx("owner"))
        assert await admin.fetchval(
            "SELECT client_id FROM property_match_decisions WHERE id=$1", uuid.UUID(current["decision_id"])) == client_x
        assert (await detail("owner"))["identity"]["verified"] is True

        # ── a platform admin decides another tenant's house ───────────────
        foreign = await property_match_api.decide_record_match(
            lead_id=lead, body=body("rejected", record_id=record_two, evidence=(), reason=OTHER_REASON),
            ctx=operator)
        foreign_id = uuid.UUID(foreign["decision_id"])
        assert await admin.fetchval("SELECT tenant_id FROM property_match_decisions WHERE id=$1", foreign_id) == tenant
        assert await opened(foreign_id, tenant) == OTHER_REASON
        assert (await detail("owner", record_two))["identity"]["confirmation"]["state"] == "rejected"

        # The audit rows: ids and the decision, in the lead's tenant; never the reason.
        audits = await admin.fetch(
            "SELECT action, user_id, target_id, metadata FROM audit_ledger "
            "WHERE tenant_id=$1 AND action LIKE 'property_match_%' ORDER BY created_at", tenant)
        assert [(a["action"], a["user_id"], str(a["target_id"])) for a in audits] == [
            ("property_match_confirmed", agent["owner"], str(first)),
            ("property_match_confirmed", agent["owner"], reconfirmed["decision_id"]),
            ("property_match_confirmed", agent["owner"], current["decision_id"]),
            ("property_match_rejected", operator.agent_id, str(foreign_id)),
        ]
        for audit in audits:
            metadata = json.loads(audit["metadata"]) if isinstance(audit["metadata"], str) else audit["metadata"]
            assert set(metadata) == {"decision_id", "lead_id", "public_record_id", "client_id", "decision",
                                     "supersedes_id"}
            assert REASON not in json.dumps(metadata) and OTHER_REASON not in json.dumps(metadata)
        assert await admin.fetchval(
            "SELECT count(*) FROM audit_ledger WHERE tenant_id=$1 AND action LIKE 'property_match_%'", other) == 0

        # ── rows leave with their house or their client ───────────────────
        async with connection.tenant_tx(ctx("owner")) as conn:
            on_two = await insert(conn, lead_id=lead_two, record_id=record_far, decision="rejected")
        await admin.execute("DELETE FROM leads WHERE id=$1", lead_two)
        assert await admin.fetchval("SELECT count(*) FROM property_match_decisions WHERE id=$1", on_two) == 0
        await admin.execute("DELETE FROM clients WHERE id=$1", client_a)
        # A's decisions go, and with them every later decision in their chain.
        left = await admin.fetch("SELECT id FROM property_match_decisions WHERE lead_id=$1", lead)
        assert {r["id"] for r in left} == {foreign_id}
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            for tid in (tenant, other):
                for table in ("property_match_decisions", "record_shares", "leads", "clients", "agent_contacts",
                              "users"):
                    await admin.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tid)
                await admin.execute("DELETE FROM tenants WHERE id = $1", tid)
            await admin.execute("DELETE FROM public_property_records WHERE id = ANY($1::uuid[])",
                                [record, record_two, record_far])
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
