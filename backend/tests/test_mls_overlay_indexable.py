"""The MLS overlay must stay matchable to its indexes.

It runs once per lead on every pipeline page, and the pipeline is pushed on
every WebSocket connect. When its WHERE clause drifted from the index
definitions — a COALESCE missing inside the parcel expression, COALESCE'd
partial-index predicates, and a char(2)/text comparison that cast the column —
each lead seq-scanned every listing in its state: 140 s per 51-lead page on the
ACTRIS feed. Matching them exactly: 5 ms (Mission 8). Postgres gives no error
when an index silently stops matching; only these assertions will.
"""

import pathlib
import re

from mls_enrichment import MLS_OVERLAY_SELECT

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"
WHERE = MLS_OVERLAY_SELECT[MLS_OVERLAY_SELECT.index("WHERE"):MLS_OVERLAY_SELECT.index("ORDER BY")]


def _flat(sql: str) -> str:
    return re.sub(r"\s+", "", sql)


def _index_sql() -> str:
    return "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.sql"))
                     if "idx_oml_match_parcel" in p.read_text())


def test_the_parcel_branch_uses_the_indexed_expression():
    assert _flat("regexp_replace(lower(COALESCE(m.features->>'parcel_number','')),'[^a-z0-9]','','g')") in _flat(WHERE)
    assert _flat("lower(COALESCE(features->>'parcel_number',''))") in _flat(_index_sql())


def test_the_address_branch_states_the_partial_index_predicate_verbatim():
    assert "m.address <> ''" in WHERE and "m.zip_code <> ''" in WHERE
    assert "COALESCE(m.address,'') <> ''" not in WHERE
    assert "COALESCE(m.zip_code,'') <> ''" not in WHERE


def test_state_is_compared_without_casting_the_indexed_column():
    assert "m.state_code = leads.state::bpchar" in WHERE
    assert "--" not in MLS_OVERLAY_SELECT  # it is embedded in larger queries


def test_mls_search_orders_by_exactly_the_search_index():
    """NULLS LAST on NOT NULL columns kept idx_oml_search_order from supplying
    the order: every search seq-scanned and sorted the state's feed (2.2 s p95
    at 25 agents, Mission 8). The count is capped for the same reason."""
    import inspect
    import mls_portal
    src = inspect.getsource(mls_portal)
    assert '"ORDER BY last_updated DESC, list_price DESC, "' in src
    assert "NULLS LAST, list_price" not in src
    assert "LIMIT {_SEARCH_COUNT_CAP + 1}) capped" in src
    idx = (MIGRATIONS / "0116_mls_search_order_index.sql").read_text()
    assert "(state_code, last_updated DESC, list_price DESC, mls_id, mls_number)" in idx
