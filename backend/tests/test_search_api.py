"""One search across everything — and it says when part of it fell over.

The failure mode that matters: a leg raises, the other three answer, and the
agent concludes the deal does not exist. `degraded` exists so that cannot
happen silently.
"""

from __future__ import annotations

import asyncio

import search_api as sa
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="a@t.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)


def _run(coro):
    return asyncio.run(coro)


def test_scoring_is_deterministic_and_kind_neutral():
    """The same rule for every kind, so a person and a property with equal
    match quality tie rather than one kind always winning by construction."""
    assert sa.score_match("sarah", "Sarah") == 1.0
    assert sa.score_match("sar", "Sarah Chen") == 0.85
    assert sa.score_match("chen", "Sarah Chen") == 0.7
    assert sa.score_match("ara", "Sarah Chen") == 0.5
    assert sa.score_match("zzz", "Sarah Chen") == 0.0
    assert sa.score_match("", "Sarah") == 0.0
    assert sa.score_match("x", None, "", "x") == 1.0


def test_a_failed_leg_is_named_not_swallowed(monkeypatch):
    async def ok(ctx, q, limit):
        return [sa._hit("people", "1", "Sarah Chen", None, "/p/1", 1.0)]

    async def boom(ctx, q, limit):
        raise RuntimeError("deals table on fire")

    monkeypatch.setitem(sa.LEGS, "people", ok)
    monkeypatch.setitem(sa.LEGS, "deals", boom)
    out = _run(sa.search(CTX, "sarah", ["people", "deals"], 10))
    assert out["degraded"] == ["deals"]
    assert out["counts"] == {"people": 1, "deals": 0}
    assert [h["label"] for h in out["results"]] == ["Sarah Chen"]


def test_results_are_ordered_by_score_then_kind_then_label(monkeypatch):
    async def people(ctx, q, limit):
        return [sa._hit("people", "p", "Main Street Realty", None, "/p/p", 0.5)]

    async def deals(ctx, q, limit):
        return [
            sa._hit("deals", "d2", "155 Main St", None, "/deal/d2", 0.85),
            sa._hit("deals", "d1", "12 Main Ave", None, "/deal/d1", 0.85),
        ]

    monkeypatch.setitem(sa.LEGS, "people", people)
    monkeypatch.setitem(sa.LEGS, "deals", deals)
    out = _run(sa.search(CTX, "main", ["people", "deals"], 10))
    assert [h["id"] for h in out["results"]] == ["d1", "d2", "p"]


def test_short_queries_return_nothing_and_run_no_leg(monkeypatch):
    """One character matches everything and answers nothing."""
    calls = []

    async def spy(ctx, q, limit):
        calls.append(q)
        return []

    for kind in sa.KINDS:
        monkeypatch.setitem(sa.LEGS, kind, spy)
    out = _run(sa.search(CTX, "s", [], 10))
    assert out["results"] == [] and calls == []
    assert out["degraded"] == []


def test_unknown_kinds_are_dropped_and_empty_means_the_defaults(monkeypatch):
    seen = []

    async def spy(ctx, q, limit):
        seen.append(limit)
        return []

    for kind in sa.KINDS:
        monkeypatch.setitem(sa.LEGS, kind, spy)
    out = _run(sa.search(CTX, "sarah", ["nonsense"], 500))
    assert set(out["counts"]) == set(sa.DEFAULT_KINDS)
    assert "records" not in out["counts"], "a name must not be searched across 8.5M parcels"
    assert all(l == sa.MAX_PER_KIND for l in seen), "the per-leg ceiling must hold"


def test_an_address_shaped_query_reaches_public_records(monkeypatch):
    """"412 Delaware Ave" is a question the public corpus can answer; "sarah"
    is not. The gate is crude on purpose — a false positive costs one
    budgeted query, a false negative still has the tenant's own leads."""
    assert sa.looks_like_address("412 Delaware Ave")
    assert sa.looks_like_address("12 main")
    assert not sa.looks_like_address("sarah chen")
    assert not sa.looks_like_address("19901")
    assert not sa.looks_like_address("")

    async def spy(ctx, q, limit):
        return []

    for kind in sa.KINDS:
        monkeypatch.setitem(sa.LEGS, kind, spy)
    assert "records" in _run(sa.search(CTX, "412 Delaware Ave", [], 10))["counts"]
    assert "records" in _run(sa.search(CTX, "anything", ["records"], 10))["counts"], \
        "asking for it by name always works"


def test_every_hit_has_the_one_shape():
    hit = sa._hit("properties", 7, "155 Main St", "Wilmington, DE", "/property/7", 0.85)
    assert set(hit) == {"kind", "id", "label", "sublabel", "href", "score"}
    assert hit["id"] == "7", "ids are strings so the renderer never branches on type"


def test_no_leg_duplicates_the_rls_predicate():
    """RLS scopes every leg; repeating half the policy hides rows from an
    admin — the defect fixed in belief_store this week. One documented
    exception: _properties binds an explicit tenant constant because `leads`
    is the 8.5M-row corpus and the planner needs a constant to index on. The
    exception must stay commented, and must never be app_current_tenant()."""
    import inspect

    # The predicate FORM, not the function name — the exception's comment is
    # allowed to name the function while explaining why it is not used.
    for fn in (sa._people, sa._deals, sa._records, sa._conversations):
        source = inspect.getsource(fn)
        assert "= app_current_tenant()" not in source, fn.__name__
        assert "tenant_id = $" not in source, fn.__name__

    props = inspect.getsource(sa._properties)
    assert "= app_current_tenant()" not in props
    assert "business-scope" in props, "the exception has to say why it exists"
    assert "ctx.tenant_id" in props


def test_every_sql_leg_sets_a_statement_budget():
    """asyncpg's command_timeout is thirty seconds. The first live probe of
    this endpoint spent all of it in one leg — a public-records ILIKE on a
    term every Delaware row contains — and reported nothing for two minutes.
    A leg that cannot answer in two seconds must degrade, not stall the box."""
    import inspect

    for fn in (sa._people, sa._properties, sa._deals, sa._records):
        assert "await _budget(conn)" in inspect.getsource(fn), fn.__name__
    assert 0 < sa.LEG_TIMEOUT_MS <= 5000
    assert "SET LOCAL statement_timeout" in inspect.getsource(sa._budget), \
        "SET LOCAL, so the cap cannot leak into the next borrower of the pooled connection"


def test_the_big_corpus_is_not_sorted_before_limit():
    """LIMIT lets an index scan stop early; a sort has to see every match."""
    import inspect

    source = inspect.getsource(sa._records)
    records_query = source.split("FROM public_property_records")[1].split('"""')[0]
    assert "ORDER BY" not in records_query
