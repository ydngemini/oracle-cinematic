"""The Work list must choose its page BEFORE decorating it.

With the per-client LATERAL subqueries in the same SELECT as ORDER BY/LIMIT,
Postgres ran all five for every client in the tenant and discarded all but
500: measured 888 ms p50 for a 20,000-client brokerage at zero load, 2.4 s
under EXPLAIN ANALYZE. Selecting the page in a MATERIALIZED CTE first cut it
to ~100 ms (performance/ baseline, Mission 7).
"""

import inspect
import re

import crm


def _sql() -> str:
    return inspect.getsource(crm.list_clients)


def test_page_is_limited_before_any_lateral_runs():
    src = _sql()
    page = src.index("WITH page AS MATERIALIZED")
    limit = src.index("LIMIT 500", page)
    first_lateral = src.index("LEFT JOIN LATERAL")
    assert page < limit < first_lateral


def test_laterals_decorate_the_page_not_the_whole_table():
    src = _sql()
    outer = src[src.index("SELECT c.id, c.full_name"):]
    assert re.search(r"FROM page c\b", outer)
    assert "FROM clients c\n" not in outer


def test_every_filter_and_sort_key_is_a_clients_column():
    # The restructure is only correct while nothing the page depends on is
    # computed by a lateral. Sort keys must stay on c.* columns.
    for expr in crm._SORT_SQL.values():
        for term in expr.split(","):
            assert term.strip().startswith("c."), expr


def _restated_tenant_predicates(src: str) -> list[str]:
    """Tenant predicates in the code and SQL, ignoring comments and docstrings."""
    if src.lstrip().startswith(("async def", "def")) and '"""' in src:
        head, _, rest = src.partition('"""')
        src = head + rest.partition('"""')[2]
    code = "\n".join(line.split("#")[0].split("--")[0] for line in src.splitlines())
    return [m for m in ("app_visible_tenants", "app_current_tenant()", "tenant_id =", "tenant_id=")
            if m in code]


def test_page_selection_does_not_restate_the_tenant_wall():
    # RLS scopes the rows; since 0141 the wall is itself an index condition.
    # The 0113 restatement `tenant_id = ANY (app_visible_tenants())` was
    # redundant, cost ~2-3 ms and hid orphan-tenant rows from a platform admin
    # (L16-03). The plan proof is tests/test_tenant_wall_plans_live.py.
    assert _restated_tenant_predicates(_sql()) == []


def test_visible_tenants_never_narrows_a_platform_admin():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0113_app_visible_tenants.sql").read_text()
    assert "WHEN app_is_platform_admin() THEN ARRAY(SELECT id FROM tenants)" in sql
    assert "GRANT EXECUTE ON FUNCTION app_visible_tenants() TO oracle_app" in sql


def test_deal_pipeline_does_not_restate_the_tenant_wall():
    # Runs on every WebSocket connect. Under the old OR policy it needed the
    # 0113 restatement (1.2 s -> 1.7 ms, Mission 8); under 0141 it does not
    # (L16-03). The leads page query is the `where` block; the rollup count
    # and the coverage lookup below it are not part of this check.
    import server
    src = inspect.getsource(server.push_deal_pipeline)
    where = src[src.index('where = """'):src.index('cursor = options["cursor"]')]
    assert "AND ($1::text IS NULL OR state=$1)" not in where  # first filter leads now
    assert "WHERE ($1::text IS NULL OR state=$1)" in where
    assert _restated_tenant_predicates(where) == []


def test_the_plan_proof_exercises_both_queries():
    import pathlib
    live = (pathlib.Path(__file__).resolve().parent / "test_tenant_wall_plans_live.py").read_text()
    assert "crm.list_clients" in live and "server.push_deal_pipeline" in live
