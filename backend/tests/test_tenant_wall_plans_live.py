"""The client page and the deal pipeline plan on a tenant index with no
restated tenant predicate — against real PostgreSQL with FORCE RLS, as the
request login (skipped without ORACLE_LIVE_DB_ADMIN_DSN).

L16-03 (audit A10). 0113 had these two queries restate the RLS policy as
`tenant_id = ANY (app_visible_tenants())`, because the old OR policy was not
indexable (the Work list scanned every brokerage's clients; the pipeline walked
the platform-wide ranking index, 1.2 s for a 250-lead brokerage). Since 0141
the tenant wall is itself an index condition, so the restatement is gone. This
test captures the SQL the real handlers send (crm.list_clients,
server.push_deal_pipeline) and EXPLAINs it — custom and generic plans, as
asyncpg prepares them — for a small brokerage beside a large one. A plan whose
target table is not reached through a tenant index, or is sequentially
scanned, fails.

It also checks that dropping the predicate changed nothing anyone sees: the
client set with and without it is identical in every session context except
the platform admin, who may additionally see rows whose tenant row is gone.
Everything runs in one transaction that is rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

SMALL = "a1440000-0000-4000-8000-00000000000a"
BIG = "b1440000-0000-4000-8000-00000000000b"
WALL = "tenant_id >= app_tenant_floor()"

# label, login, tenant, role, agent — the request-login contexts whose plans
# must use the tenant index (the small brokerage, where a platform-wide walk
# is the failure), plus the platform admin and the other brokerage for the
# visibility check.
PLAN_CONTEXTS = [
    ("agent of the small brokerage", "oracle_app_login", SMALL, "agent", "l1603-small-agent"),
    ("owner of the small brokerage", "oracle_app_login", SMALL, "broker_owner", "l1603-small-owner"),
    ("forged admin GUC, request login", "oracle_app_login", SMALL, "platform_admin", "forged"),
]
ALL_CONTEXTS = PLAN_CONTEXTS + [
    ("owner of the big brokerage", "oracle_app_login", BIG, "broker_owner", "l1603-big-owner"),
    ("platform admin, platform login", "oracle_platform_login",
     "00000000-0000-0000-0000-000000000000", "platform_admin", "ops"),
]


class _Recorder:
    """Stands in for the tenant connection; keeps every statement sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 0

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"


class _Socket:
    async def send_text(self, _text):
        return None

    async def send_json(self, _data):
        return None


async def _captured_queries(monkeypatch) -> list[tuple[str, str, str, tuple]]:
    """(label, target table, sql, args) for each hot query, from the handlers."""
    import crm
    import server
    from db import connection
    from tenancy import Role, TenantContext

    rec = _Recorder()

    @contextlib.asynccontextmanager
    async def fake_tx(*_a, **_k):
        yield rec

    monkeypatch.setattr(crm, "tenant_tx", fake_tx)
    monkeypatch.setattr(connection, "tenant_tx", fake_tx)
    ctx = TenantContext(agent_id="l1603-small-agent", tenant_id=SMALL, role=Role.AGENT)
    out = []
    for sort in ("recent", "score"):
        rec.calls.clear()
        await crm.list_clients(type="all", stage=None, tag=None, q=None, score_min=None,
                               assignee=None, sort=sort, ctx=ctx)
        sql, args = next(c for c in rec.calls if "WITH page AS MATERIALIZED" in c[0])
        out.append((f"crm.list_clients sort={sort}", "clients", sql, args))
    for request, label in (({}, "page"), ({"priority": "hot"}, "page, hot filter")):
        rec.calls.clear()
        await server.push_deal_pipeline(_Socket(), ctx, request)
        page = [c for c in rec.calls if "FROM leads" in c[0] and "ORDER BY motivation_score" in c[0]]
        assert len(page) == 1, rec.calls
        out.append((f"server.push_deal_pipeline {label}", "leads", *page[0]))
        if request:
            count = [c for c in rec.calls if "FROM (SELECT 1 FROM leads" in c[0]]
            assert len(count) == 1, rec.calls
            out.append(("server.push_deal_pipeline capped count, hot filter", "leads", *count[0]))
    # (The MLS overlay's entitlement subquery keeps its own
    # `e.tenant_id = app_current_tenant()`: a per-brokerage licence, not a
    # restated wall — MLS is gated off, D-2026-10-12-01.)
    for label, _rel, sql, _args in out:
        assert "app_visible_tenants" not in sql, label
    return out


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _nodes(plan):
    if isinstance(plan, dict):
        yield plan
        for child in plan.get("Plans", []):
            yield from _nodes(child)


def _subtree(plan, name):
    for node in _nodes(plan):
        if node.get("Subplan Name") == name:
            return node
    return plan


async def _set_context(conn, login, tenant, role, agent):
    await conn.execute(f'SET LOCAL SESSION AUTHORIZATION "{login}"')  # nosec B608 — fixed test roles
    await conn.execute(
        "SELECT set_config('app.current_tenant', $1, true), set_config('app.current_role', $2, true),"
        "       set_config('app.current_agent', $3, true), set_config('app.record_scope', 'actor', true)",
        tenant, role, agent)


async def _scenario(queries):
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "INSERT INTO tenants (id, slug, name) VALUES ($1, 'l1603-small', 'L16-03 small'),"
                " ($2, 'l1603-big', 'L16-03 big') ON CONFLICT (id) DO NOTHING", SMALL, BIG)
            await conn.execute(
                "INSERT INTO clients (tenant_id, full_name, lead_score) SELECT $1::uuid, 'S ' || g, g % 100"
                "  FROM generate_series(1, 250) g", SMALL)
            await conn.execute(
                "INSERT INTO clients (tenant_id, full_name, lead_score) SELECT $1::uuid, 'B ' || g, g % 100"
                "  FROM generate_series(1, 20000) g", BIG)
            await conn.execute(
                "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score)"
                " SELECT $1::uuid, 'L1603-S-' || g, 'DE', g % 100 FROM generate_series(1, 50) g", SMALL)
            await conn.execute(
                "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score)"
                " SELECT $1::uuid, 'L1603-B-' || g, 'DE', g % 100 FROM generate_series(1, 20000) g", BIG)
            await conn.execute("ANALYZE clients")
            await conn.execute("ANALYZE leads")
            target_indexes = {
                rel: {r["i"] for r in await conn.fetch(
                    "SELECT indexrelid::regclass::text AS i FROM pg_index WHERE indrelid = $1::regclass", rel)}
                for rel in ("clients", "leads")
            }

            problems = []
            checked = 0
            for label, login, tenant, role, agent in PLAN_CONTEXTS:
                for qlabel, rel, sql, args in queries:
                    for mode in ("auto", "force_generic_plan"):
                        await conn.execute("SAVEPOINT plan_check")
                        await _set_context(conn, login, tenant, role, agent)
                        await conn.execute("SELECT set_config('plan_cache_mode', $1, true)", mode)
                        await conn.execute("PREPARE l1603_q AS " + sql)  # nosec B608 — SQL captured from the app
                        execute = "EXECUTE l1603_q"
                        if args:
                            execute += "(" + ", ".join(_literal(a) for a in args) + ")"
                        raw = await conn.fetchval("EXPLAIN (FORMAT JSON) " + execute)  # nosec B608
                        await conn.execute("DEALLOCATE l1603_q")
                        await conn.execute("ROLLBACK TO SAVEPOINT plan_check")
                        plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
                        scope = _subtree(plan, "CTE page") if rel == "clients" else plan
                        nodes = list(_nodes(scope))
                        seq = [n for n in nodes if n.get("Node Type") == "Seq Scan" and n.get("Relation Name") == rel]
                        walled = [n for n in nodes if n.get("Index Name") in target_indexes[rel]
                                  and WALL in (n.get("Index Cond") or "")]
                        unwalled = [n.get("Index Name") for n in nodes if n.get("Index Name") in target_indexes[rel]
                                    and WALL not in (n.get("Index Cond") or "")]
                        if seq or not walled or unwalled:
                            problems.append(f"[{label}] {qlabel} ({mode}): seq={bool(seq)} "
                                            f"tenant-index={[n['Index Name'] for n in walled]} other={unwalled}")
                        checked += 1

            # Dropping the predicate changed no one's client set.
            differs = []
            for label, login, tenant, role, agent in ALL_CONTEXTS:
                await conn.execute("SAVEPOINT rows_check")
                await _set_context(conn, login, tenant, role, agent)
                base = "SELECT count(*), COALESCE(sum(hashtext(c.id::text)), 0) FROM clients c WHERE c.archived_at IS NULL"
                without = tuple(await conn.fetchrow(base))
                with_pred = tuple(await conn.fetchrow(base + " AND c.tenant_id = ANY (app_visible_tenants())"))
                await conn.execute("ROLLBACK TO SAVEPOINT rows_check")
                # A platform admin may now also see clients whose tenant row
                # is gone (the predicate listed `tenants`); everyone else must
                # see exactly the same set.
                if role == "platform_admin" and login == "oracle_platform_login":
                    if without[0] < with_pred[0]:
                        differs.append((label, without, with_pred))
                elif without != with_pred:
                    differs.append((label, without, with_pred))
                if tenant == SMALL and role != "platform_admin" and without[0] < 1:
                    differs.append((label, "sees nothing", without))
        finally:
            await tx.rollback()
    finally:
        await conn.close()
    assert checked == len(PLAN_CONTEXTS) * len(queries) * 2
    assert not problems, "\n".join(problems)
    assert not differs, differs


def test_hot_pages_plan_on_the_tenant_index_without_a_restated_predicate(monkeypatch):
    queries = asyncio.run(_captured_queries(monkeypatch))
    assert {q[0] for q in queries} >= {
        "crm.list_clients sort=recent", "server.push_deal_pipeline page",
        "server.push_deal_pipeline capped count, hot filter",
    }
    asyncio.run(_scenario(queries))


def test_capture_sees_the_real_handlers(monkeypatch):
    # Guards the guard: if the handlers stop matching, the plan test above
    # must fail loudly rather than check nothing.
    queries = asyncio.run(_captured_queries(monkeypatch))
    assert all(re.search(r"\bFROM (clients|leads)\b", q[2]) for q in queries)
