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


def test_page_selection_has_an_indexable_tenant_condition():
    # The RLS policy's OR (admin OR tenant) is not indexable; without this the
    # Work list scanned every tenant's clients (migration 0113).
    assert "c.tenant_id = ANY (app_visible_tenants())" in _sql()


def test_visible_tenants_never_narrows_a_platform_admin():
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0113_app_visible_tenants.sql").read_text()
    assert "WHEN app_is_platform_admin() THEN ARRAY(SELECT id FROM tenants)" in sql
    assert "GRANT EXECUTE ON FUNCTION app_visible_tenants() TO oracle_app" in sql


def test_deal_pipeline_has_an_indexable_tenant_condition():
    # Runs on every WebSocket connect. Without the condition the RLS OR forced a
    # walk of every tenant's leads (1.2 s → 1.7 ms measured, Mission 8).
    import inspect
    import server
    src = inspect.getsource(server.push_deal_pipeline)
    assert "WHERE tenant_id = ANY (app_visible_tenants())" in src
