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
