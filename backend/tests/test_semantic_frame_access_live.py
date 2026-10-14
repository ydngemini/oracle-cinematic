"""A SemanticFrame's candidates are authorised as the person asking — against
real PostgreSQL with FORCE RLS, as the application role (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Prompt 05, AC05.03 (identity is authorised, never invented: F05.02, F05.07);
ledger L05-02. Owner decision D-2026-10-13-01: an agent sees their own
records, the unassigned pool and what is shared with them.

Actors: agent A, agent B, agent C (B's client and contact shared with C to
VIEW), the broker owner, a platform admin, and agent D of another brokerage.
Records per owner (A, B, the pool, and D in the other brokerage): a client, a
contact, a deal on the client, and a lead that is the client's house; plus a
harvested lead with no client, and an id that exists nowhere.

One frame names every one of them as candidates. Whatever survives
`authorize_frame` for an actor must be exactly what that actor may open.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

OWNERS = ("a", "b", "pool", "d")


def test_frame_candidates_follow_record_access():
    asyncio.run(_scenario())


async def _scenario():  # noqa: C901 — one scenario, read top to bottom
    import asyncpg

    import semantic_frame as sf
    from db import connection
    from tenancy import Role, TenantContext, brokerage_scope

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    tag = uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c", "d")}
    agent = {k: f"{k}-{tag}@frame.test" for k in users}
    tenant_of = {k: (other if k == "d" else tenant) for k in (*users, "pool")}
    assignee = {"a": agent["a"], "b": agent["b"], "pool": None, "d": agent["d"]}
    rec = {k: {t: uuid.uuid4() for t in ("client", "contact", "deal", "lead")} for k in OWNERS}
    harvest, ghost = uuid.uuid4(), uuid.uuid4()

    def ctx(who: str) -> TenantContext:
        role = Role.BROKER_OWNER if who == "owner" else Role.AGENT
        return TenantContext(agent_id=agent[who], tenant_id=tenant_of[who], role=role)

    actors = {k: ctx(k) for k in ("a", "b", "c", "owner", "d")}
    actors["admin"] = TenantContext(agent_id=f"frame-admin-{tag}", tenant_id=tenant, role=Role.PLATFORM_ADMIN)

    def frame() -> "sf.SemanticFrame":
        def cands(record_type: str, ids) -> list[dict]:
            return [{"record_type": record_type, "id": str(i)} for i in ids]

        ev = [{"source": "utterance", "quote": "Sarah"}]
        people = (cands("client", [rec[k]["client"] for k in OWNERS] + [ghost])[:3]
                  + cands("contact", [rec[k]["contact"] for k in OWNERS])[:2])
        # Mentions hold at most MAX_CANDIDATES each; spread the records over
        # several mentions so every one is asked about.
        mentions = [
            {"text": "Sarah", "kind": "person", "candidates": people, "evidence": ev},
            {"text": "Sarah", "kind": "person", "evidence": ev, "candidates": (
                cands("client", [rec["d"]["client"], ghost])
                + cands("contact", [rec[k]["contact"] for k in ("pool", "d")]))},
            {"text": "her house", "kind": "property", "evidence": ev, "candidates": (
                cands("lead", [rec[k]["lead"] for k in OWNERS] + [harvest]))},
            {"text": "the deal", "kind": "deal", "evidence": ev, "candidates": (
                cands("deal", [rec[k]["deal"] for k in OWNERS] + [ghost]))},
        ]
        return sf.validate_frame({
            "schema_version": 1,
            "goals": [{"label": "show_person", "evidence": ev}],
            "mentions": mentions, "effect": "read",
        })

    try:
        for t, slug in ((tenant, "frame"), (other, "frame-other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Frame matrix')",
                                t, f"{slug}-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, tenant_of[who], agent[who],
                "broker_owner" if who == "owner" else "agent", f"Agent {who.upper()}")
        for k in OWNERS:
            r, t = rec[k], tenant_of[k]
            await admin.execute(
                "INSERT INTO agent_contacts (id, tenant_id, assigned_agent_id, source, data_state) "
                "VALUES ($1,$2,$3,'matrix','pending_encryption')", r["contact"], t, assignee[k])
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, contact_id) VALUES ($1,$2,$3,$4,$5)",
                r["client"], t, f"Frame {k}", assignee[k], r["contact"])
            await admin.execute(
                "INSERT INTO transactions (id, tenant_id, client_id, client_party_role, state_code) "
                "VALUES ($1,$2,$3,'seller','DE')", r["deal"], t, r["client"])
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, address) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5)", r["lead"], t, f"FRAME-{tag}-{k}", r["client"],
                f"{tag} {k} Street, Dover, DE")
        await admin.execute(
            "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, address) "
            "VALUES ($1,$2,$3,'DE',50,$4)", harvest, tenant, f"FRAME-{tag}-harvest", f"{tag} Harvest Rd")
        for kind in ("client", "contact"):
            await admin.execute(
                "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
                "granted_by) VALUES ($1,$2,$3,$4,'view',$5)", tenant, kind, rec["b"][kind], users["c"], agent["b"])

        def every(*owners: str, lead_extra: bool = True) -> set:
            out = {rec[o][t] for o in owners for t in ("client", "contact", "deal", "lead")}
            return out | ({harvest} if lead_extra else set())

        expected = {
            "a": every("a", "pool"),
            "b": every("b", "pool"),
            "c": every("b", "pool"),          # B's client shared to C, view only
            "owner": every("a", "b", "pool"),
            "admin": every("a", "b", "pool"),
            "d": every("d", lead_extra=False),  # the other brokerage sees only its own
        }
        in_tenant = every("a", "b", "pool")
        for who, want in expected.items():
            out = await sf.authorize_frame(actors[who], frame())
            assert out.authorized
            everything = {c.id for m in out.mentions for c in m.candidates}
            got = everything
            if who == "admin":
                got = everything & in_tenant  # a platform admin may also read other brokerages
            assert got == want, (who, sorted(map(str, got - want)), sorted(map(str, want - got)))
            assert ghost not in got
            proposed = sum(len(m.candidates) for m in frame().mentions)
            assert out.dropped_candidates == proposed - len(
                [c for m in out.mentions for c in m.candidates]), who
            # Cross-brokerage denial, both ways (C3.5).
            if who != "admin":
                foreign = every("d", lead_extra=False) if who != "d" else in_tenant
                assert not (got & foreign), who
            # Nothing the caller cannot open reaches a planner, and a mention
            # left with no candidate becomes a question.
            for i, m in enumerate(out.mentions):
                asked = [a for a in out.ambiguities if a.mention == i]
                if not m.candidates or len(m.candidates) > 1:
                    assert len(asked) == 1, (who, i)
                    assert {c.id for c in asked[0].choices} <= everything

        # The access level is the database's own decision (record_access).
        out = await sf.authorize_frame(actors["c"], frame())
        levels = {c.id: c.access for m in out.mentions for c in m.candidates}
        assert levels[rec["b"]["client"]] == "view" and levels[rec["b"]["contact"]] == "view"
        assert levels[rec["pool"]["client"]] == "edit"
        assert levels[rec["b"]["deal"]] == "visible"
        out = await sf.authorize_frame(actors["a"], frame())
        levels = {c.id: c.access for m in out.mentions for c in m.candidates}
        assert levels[rec["a"]["client"]] == "edit" and levels[rec["a"]["lead"]] == "edit"

        # A pipeline context is refused: a person's frame is never authorised
        # against the whole brokerage.
        with pytest.raises(sf.FrameRejected):
            await sf.authorize_frame(brokerage_scope(actors["a"]), frame())

        # Revoking the share between drafting and execution drops B's records
        # for C on the next authorisation (C3.4).
        stored = (await sf.authorize_frame(actors["c"], frame())).model_dump(mode="json")
        await admin.execute(
            "UPDATE record_shares SET revoked_at = now() WHERE tenant_id=$1 AND grantee_user_id=$2",
            tenant, users["c"])
        again = await sf.authorize_frame(actors["c"], sf.validate_frame(stored))
        got = {c.id for m in again.mentions for c in m.candidates}
        assert not ({rec["b"]["client"], rec["b"]["contact"], rec["b"]["deal"], rec["b"]["lead"]} & got)
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
                for t in (tenant, other):
                    await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", t)
            for t in (tenant, other):
                await admin.execute("DELETE FROM tenants WHERE id=$1", uuid.UUID(t))
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
