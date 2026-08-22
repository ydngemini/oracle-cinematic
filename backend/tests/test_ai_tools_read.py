"""What the fifteen read-only tools say when the data cannot support the answer.

The handlers themselves are mostly SQL. What is worth pinning is the part that
is a judgement: whether an empty result is reported as an empty market, whether
a state median is allowed to stand in for a zip, whether a number that was never
calibrated is allowed to be called a confidence.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

import ai_tools_read as tools
from tenancy import Role, TenantContext


CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class _Conn:
    """A connection that answers by matching a fragment of the SQL.

    Routes are tried in order; the first whose fragment appears in the
    normalised query wins. Anything unrouted answers empty, which is what makes
    "the table has nothing for you" the default case under test.
    """

    def __init__(self, *routes):
        self.routes = list(routes)
        self.queries: list[str] = []

    def _answer(self, query, default):
        normalised = " ".join(query.split())
        self.queries.append(normalised)
        for fragment, value in self.routes:
            if fragment in normalised:
                return value
        return default

    async def fetch(self, query, *args):
        return self._answer(query, [])

    async def fetchrow(self, query, *args):
        return self._answer(query, None)

    async def fetchval(self, query, *args):
        return self._answer(query, None)


def _run(tool_name, conn, **tool_input):
    return asyncio.run(tools.execute(conn, CTX, tool_name, tool_input))


SUBJECT = {
    "id": "rec-1", "address": "15 Main St", "city": "Dover", "county": "Kent",
    "state": "DE", "zip_code": "19901", "latitude": 39.158, "longitude": -75.524,
    "building_area_sqft": 1800, "lot_area_sqft": 7000, "bedrooms": 3,
    "bathrooms": 2, "year_built": 1972, "zoning_district": "R-1",
    "land_use": "single_family", "last_sale_price": 240000,
    "reported_record_date": date(2025, 6, 1), "source_name": "DE FDOR",
    "source_key": "de_fdor", "record_refreshed_at": None,
}


def _comp(record_id, price, sqft, distance=0.2):
    return {**SUBJECT, "id": record_id, "last_sale_price": price,
            "building_area_sqft": sqft, "distance_miles": distance}


# ---------------------------------------------------------------------------
# Comparables
# ---------------------------------------------------------------------------

def test_an_unresolvable_address_refuses_rather_than_widening_the_search():
    """The alternative is returning same-city rows under a heading that says
    "within 0.5 miles" — a confident answer to a question that failed."""
    conn = _Conn(("SELECT 1 FROM public_property_records LIMIT 1", 1))
    result = _run("list_comparable_sales", conn, address="404 Nowhere Rd, Dover DE")

    assert result["ok"] is False
    assert "radius search needs a resolved coordinate" in result["error"]
    assert "comparables" not in result


def test_a_subject_without_coordinates_falls_back_to_its_zip_and_says_so():
    """Only 41,292 of the 797,235 records with a sale price also carry a
    coordinate. Refusing without one made the tool blind to 610,655 more that
    have a sale price and a ZIP — so it widens, and never calls the result a
    distance."""
    comp = {**SUBJECT, "id": "c1", "last_sale_price": 300000,
            "building_area_sqft": 1500, "distance_miles": None}
    conn = _Conn(
        ("FROM public_property_records WHERE search_document ILIKE",
         {**SUBJECT, "latitude": None, "longitude": None}),
        ("WHERE zip_code = $1", [comp]),
    )
    result = _run("list_comparable_sales", conn, address="15 Main St")

    assert result["ok"] is True
    assert result["scope"]["basis"] == "same_zip"
    assert result["scope"]["radius_miles"] is None
    assert result["scope"]["subject_has_coordinate"] is False
    assert "not a measured distance" in result["scope"]["basis_note"]
    assert result["comparables"][0]["distance_miles"] is None


def test_a_subject_with_neither_a_coordinate_nor_a_zip_is_refused():
    conn = _Conn(("FROM public_property_records WHERE search_document ILIKE",
                  {**SUBJECT, "latitude": None, "longitude": None, "zip_code": None}))
    result = _run("list_comparable_sales", conn, address="15 Main St")

    assert result["ok"] is False
    assert "no neighbourhood to compare within" in result["error"]


def test_the_zip_tier_is_a_fallback_not_a_substitute():
    """Widening from a radius to a ZIP loses real precision, so it must only
    happen after the radius search has genuinely found nothing."""
    conn = _Conn(
        ("FROM public_property_records WHERE search_document ILIKE", SUBJECT),
        ("count(*)::int FROM public_property_records", 312),
    )
    result = _run("list_comparable_sales", conn, address="15 Main St, Dover DE")

    # The subject has coordinates and the radius query returned nothing; the
    # ZIP tier then also returns nothing here, but the radius was tried first.
    assert any("distance_miles" in query for query in conn.queries)


def test_no_comps_reports_the_scope_searched_and_the_dataset_coverage():
    """"No comps" and "this state has 312 geocoded sales in the dataset" are
    different answers, and only the first is about the market."""
    conn = _Conn(
        ("FROM public_property_records WHERE search_document ILIKE", SUBJECT),
        ("count(*)::int FROM public_property_records", 312),
    )
    result = _run("list_comparable_sales", conn, address="15 Main St, Dover DE")

    assert result["ok"] is True
    assert result["count"] == 0
    # No tier answered, so none is credited; both were tried.
    assert result["scope"]["basis"] is None
    assert result["scope"]["tiers_attempted"] == ["radius", "same_zip"]
    assert result["scope"]["radius_miles"] is None
    assert result["scope"]["sold_within_months"] == 12
    assert result["coverage"]["records_with_coordinates_and_sale_price"] == 312


def test_a_widened_lookback_says_so(monkeypatch):
    conn = _Conn(
        ("FROM public_property_records WHERE search_document ILIKE", SUBJECT),
        ("count(*)::int FROM public_property_records", 5),
    )

    async def _fake(conn_, subject, *, radius_miles, limit, months):
        return [] if months == 12 else [_comp("c1", 300000, 1500)]

    monkeypatch.setattr(tools, "_comps_near", _fake)
    result = _run("list_comparable_sales", conn, address="15 Main St, Dover DE")

    assert result["scope"]["sold_within_months"] == 24
    assert result["scope"]["widened_from_months"] == 12


def test_the_subject_lookup_never_scans_without_an_index():
    """Both resolution paths are indexed; an unindexed match here would seq-scan
    roughly seven million rows on every call."""
    conn = _Conn(("FROM public_property_records", SUBJECT))
    asyncio.run(tools._resolve_subject(conn, "15 Main St, Dover DE 19901"))

    query = conn.queries[0]
    assert "WHERE state=$1" in query, "the address index is keyed on state first"
    assert "regexp_replace" in query


def test_the_subject_lookup_falls_back_to_the_trigram_index_without_a_state():
    conn = _Conn(("search_document ILIKE", SUBJECT))
    asyncio.run(tools._resolve_subject(conn, "15 Main Street"))

    assert any("search_document ILIKE" in q for q in conn.queries)


# ---------------------------------------------------------------------------
# ARV
# ---------------------------------------------------------------------------

def _arv_conn(monkeypatch, comps):
    conn = _Conn(
        ("FROM public_property_records WHERE search_document ILIKE", SUBJECT),
        ("count(*)::int FROM public_property_records", 312),
    )

    async def _fake(conn_, subject, *, radius_miles, limit, months):
        return comps

    monkeypatch.setattr(tools, "_comps_near", _fake)
    return conn


def test_arv_never_reports_a_confidence_it_did_not_measure(monkeypatch):
    conn = _arv_conn(monkeypatch, [_comp("c1", 300000, 1500),
                                   _comp("c2", 330000, 1650),
                                   _comp("c3", 280000, 1400)])
    result = _run("estimate_arv", conn, address="15 Main St, Dover DE")

    assert result["ok"] is True
    assert result["confidence"] is None
    assert result["confidence_basis"]["scored"] is False
    assert "calibration" in result["confidence_basis"]["reason"]
    assert result["confidence_basis"]["comparable_count"] == 3
    assert result["arv"] == pytest.approx(360000, abs=1)


def test_arv_counts_the_comps_it_had_to_drop(monkeypatch):
    """A median taken over a silently smaller set is the error this reports
    instead of making."""
    conn = _arv_conn(monkeypatch, [_comp("c1", 300000, 1500),
                                   _comp("c2", 330000, None)])
    result = _run("estimate_arv", conn, address="15 Main St, Dover DE")

    assert result["comparables_found"] == 2
    assert result["comparables_usable"] == 1
    assert result["comparables_dropped_for_missing_sqft"] == 1
    assert result["confidence_basis"]["below_minimum_comparables"] is True


def test_arv_returns_nothing_when_no_comp_can_support_it(monkeypatch):
    conn = _arv_conn(monkeypatch, [_comp("c1", 300000, None)])
    result = _run("estimate_arv", conn, address="15 Main St, Dover DE")

    assert result["arv"] is None
    assert "cannot be taken" in result["error_detail"]
    assert result["coverage"]["records_with_coordinates_and_sale_price"] == 312


# ---------------------------------------------------------------------------
# Rehab and MAO
# ---------------------------------------------------------------------------

def test_rehab_labels_its_bands_national_and_names_the_missing_source():
    conn = _Conn(("FROM public_property_records", SUBJECT))
    result = _run("estimate_rehab", conn, address="15 Main St, Dover DE",
                  condition="moderate")

    assert result["ok"] is True
    assert result["method"] == "national_band_times_area"
    assert "no local labour or material rates were used" in result["basis"].lower()
    # 1800 sqft × $25-50 + 15% contingency
    assert result["cost_low"] == pytest.approx(51750, abs=1)
    assert result["cost_high"] == pytest.approx(103500, abs=1)


def test_a_gut_rehab_reports_an_unbounded_upper_cost():
    """The documented band is "$50-100+/sf"; the "+" has no upper bound to
    report, and inventing one would be the only way to fill this field."""
    conn = _Conn(("FROM public_property_records", SUBJECT))
    result = _run("estimate_rehab", conn, address="15 Main St", condition="gut")

    assert result["cost_high"] is None
    assert result["cost_high_unbounded"] is True


def test_rehab_flags_pre_1978_rather_than_pricing_it():
    conn = _Conn(("FROM public_property_records", SUBJECT))
    result = _run("estimate_rehab", conn, address="15 Main St", condition="light")

    assert any("lead-based paint" in flag for flag in result["risk_flags"])


def test_rehab_refuses_without_a_square_footage():
    conn = _Conn(("FROM public_property_records",
                  {**SUBJECT, "building_area_sqft": None}))
    result = _run("estimate_rehab", conn, address="15 Main St", condition="light")

    assert result["ok"] is False
    assert "nothing to multiply" in result["error"]


def test_mao_states_the_arithmetic_that_actually_ran():
    conn = _Conn()
    with_holding = _run("calculate_mao", conn, arv="300000", rehab="45000",
                        holding_costs="8000")
    without = _run("calculate_mao", conn, arv="300000", rehab="45000")

    assert with_holding["mao"] == 157000.0
    assert "holding_costs" in with_holding["trace"]["formulas"][0]
    assert without["mao"] == 165000.0
    assert "holding_costs" not in without["trace"]["formulas"][0]


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

_STATE_ROW = {
    "state_code": "DE", "state_name": "Delaware", "median_sale_price": 360000,
    "median_list_price": 360000, "median_days_on_market": 42,
    "months_of_supply": None, "yoy_price_change_pct": 4.0, "active_listings": None,
    "closed_sales_last_30d": None, "list_to_sale_ratio": None,
    "avg_price_per_sqft": None, "as_of_date": date(2024, 10, 1),
}


def _market_conn(distribution=None):
    return _Conn(
        ("SELECT state, count(*)::int AS n FROM public_property_records",
         distribution if distribution is not None else [{"state": "DE", "n": 900}]),
        ("SELECT county FROM public_property_records", "Kent"),
        ("FROM state_market_stats", _STATE_ROW),
        ("FROM county_market_stats", None),
        ("SELECT 1 FROM county_market_stats LIMIT 1", None),
    )


def test_market_trends_never_presents_a_state_median_as_a_zip_figure():
    result = _run("get_market_trends", _market_conn(), zip_code="19901")

    assert result["ok"] is True
    assert "No zip-level aggregate exists" in result["granularity_note"]
    assert result["state"]["geography"] == "DE"
    assert result["requested_zip"] == "19901"
    assert result["forecast"] is None
    assert "time series" in result["forecast_unavailable_reason"]


def test_a_two_year_old_median_is_reported_as_stale():
    """state_market_stats is seeded as of 2024-10-01 and never refreshed. A
    figure returned without its age reads as the current market."""
    result = _run("get_market_trends", _market_conn(), zip_code="19901")

    assert result["state"]["as_of"] == "2024-10-01"
    assert result["state"]["stale"] is True
    assert "historical" in result["state"]["staleness_note"]


def test_a_zip_the_dataset_cannot_agree_on_is_refused_with_the_conflict():
    """ZIP 19901 is Dover, Delaware. The TN harvester wrote it onto records in
    Dover, Tennessee, so a modal vote returns Tennessee on a 58% plurality with
    nothing to signal that anything went wrong. A ZIP has one state; a dataset
    that says otherwise is broken, not ambiguous."""
    conn = _market_conn([{"state": "TN", "n": 48}, {"state": "GA", "n": 16},
                         {"state": "NC", "n": 9}, {"state": "VT", "n": 6},
                         {"state": "NH", "n": 3}])
    result = _run("get_market_trends", conn, zip_code="19901")

    assert result["ok"] is False
    assert "appears under several state codes" in result["error"]
    assert "TN (48 records)" in result["error"]
    assert result["state_distribution"][0]["state"] == "TN"


def test_a_handful_of_stray_rows_does_not_block_a_clear_resolution():
    """The threshold has to separate a broken ZIP from a mostly-clean one, or
    it just refuses everything."""
    conn = _market_conn([{"state": "ID", "n": 26540}, {"state": "TN", "n": 12},
                         {"state": "OR", "n": 8}])
    result = _run("get_market_trends", conn, zip_code="83646")

    assert result["ok"] is True
    assert result["resolved_geography"]["state"] == "ID"
    assert result["resolved_geography"]["state_agreement"] > 0.99


def test_an_unresolvable_zip_refuses_instead_of_guessing_from_the_prefix():
    conn = _Conn(("SELECT 1 FROM public_property_records LIMIT 1", 1))
    result = _run("get_market_trends", conn, zip_code="00001")

    assert result["ok"] is False
    assert "guessing the geography from the prefix" in result["error"]


def test_the_zip_lookup_uses_an_equality_predicate():
    """The (zip_code, state) index added in 0076 only helps on equality; an
    ILIKE here would restore the 13.5-second scan it was written to remove."""
    conn = _market_conn()
    _run("get_market_trends", conn, zip_code="19901")

    zip_query = next(q for q in conn.queries
                     if "SELECT state, count(*)::int AS n" in q)
    assert "zip_code=$1" in zip_query
    assert "ILIKE" not in zip_query


def test_days_on_market_separates_the_zip_level_source_from_its_context():
    conn = _Conn(
        ("SELECT state, count(*)::int AS n FROM public_property_records",
         [{"state": "DE", "n": 900}]),
        ("SELECT county FROM public_property_records", "Kent"),
        ("FROM oracle_mls_listings", {"n": 0, "median_dom": None, "mean_dom": None,
                                      "active": 0, "closed": 0}),
        ("SELECT 1 FROM oracle_mls_listings LIMIT 1", None),
        ("FROM state_market_stats", _STATE_ROW),
    )
    result = _run("get_days_on_market", conn, zip_code="19901")

    assert result["zip_level"] is None
    assert result["zip_level_unavailable_reason"]
    assert result["state"]["median_days_on_market"] == 42
    assert result["by_property_type"] is None


# ---------------------------------------------------------------------------
# Listings, transactions, disclosures, media
# ---------------------------------------------------------------------------

def test_search_listings_reports_the_empty_mls_cache_instead_of_hiding_it():
    """Returning only owned listings without saying the other source held
    nothing reads as "these are all the listings that match"."""
    conn = _Conn(
        ("FROM listings l", [{"id": "l1", "address": "15 Main St", "price": 250000,
                              "status": "active", "is_shared_mls": False,
                              "updated_at": None, "state": "DE", "beds": 3,
                              "baths": 2, "sqft": 1800, "lead_id": None}]),
        ("SELECT 1 FROM oracle_mls_listings LIMIT 1", None),
    )
    result = _run("search_listings", conn, query="Main St")

    mls_source = next(s for s in result["sources_searched"]
                      if s["source"] == "oracle_mls_listings")
    assert mls_source["matched"] == 0
    assert "empty on this deployment" in mls_source["note"]
    assert len(result["owned_listings"]) == 1


def test_required_disclosures_distinguishes_an_unloaded_table_from_an_empty_state():
    unloaded = _Conn(("SELECT 1 FROM state_disclosure_forms LIMIT 1", None))
    result = _run("list_required_disclosures", unloaded, state="DE")
    assert result["ok"] is False
    assert result["code"] == "DATASET_NOT_LOADED"
    assert result["how_to_populate"]

    loaded_but_empty = _Conn(("SELECT 1 FROM state_disclosure_forms LIMIT 1", 1))
    result = _run("list_required_disclosures", loaded_but_empty, state="DE")
    assert result["ok"] is True
    assert result["count"] == 0
    assert "not a finding that DE requires no disclosures" in result["note"]


def test_the_transaction_workflow_never_selects_an_encrypted_column():
    conn = _Conn(
        ("FROM leads WHERE id=$1::uuid", {"id": "d1", "address": "15 Main St",
                                          "state": "DE", "sqft": 1800,
                                          "asking_price": 200000,
                                          "underwriting": None,
                                          "dossier_status": "draft"}),
        ("FROM transactions", [{"id": "t1", "status": "open", "state_code": "DE",
                                "property_address": "15 Main St",
                                "purchase_price": 200000, "earnest_money": 5000,
                                "financing_amount": None, "offer_deadline": None,
                                "inspection_deadline": None,
                                "financing_deadline": None,
                                "closing_deadline": None, "accepted_offer_id": None,
                                "closed_at": None, "created_at": None,
                                "updated_at": None}]),
    )
    result = _run("get_transaction_workflow", conn,
                  deal_id="22222222-2222-2222-2222-222222222222")

    assert result["ok"] is True
    assert "transaction_parties.contact_ciphertext" in result["excluded_fields"]
    assert not any("ciphertext" in q for q in conn.queries)


def test_the_financial_summary_names_what_it_cannot_compute():
    """A summary that silently drops holding costs reads as a deal with none."""
    conn = _Conn(
        ("FROM leads WHERE id=$1::uuid", {"id": "d1", "address": "15 Main St",
                                          "state": "DE", "sqft": 1800,
                                          "asking_price": 200000,
                                          "underwriting": '{"arv": 300000}',
                                          "dossier_status": "draft"}),
    )
    result = _run("get_deal_financial_summary", conn,
                  deal_id="22222222-2222-2222-2222-222222222222")

    assert result["recorded"]["arv"] == 300000
    assert set(result["not_recorded"]) == {
        "holding_costs", "closing_costs", "assignment_fee", "net_profit"}
    assert result["recorded"]["purchase_price"] is None


def test_ai_generated_images_are_counted_separately_from_photographs():
    conn = _Conn(("FROM property_media", [
        {"id": "m1", "kind": "photo", "caption": None, "sort_order": 1,
         "surface": "exterior", "uploaded_via": "agent", "review_status": "approved",
         "provenance": "captured", "generator": None, "content_type": "image/jpeg",
         "created_at": None},
        {"id": "m2", "kind": "photo", "caption": "Post-rehab concept",
         "sort_order": 2, "surface": "interior", "uploaded_via": "pipeline",
         "review_status": "pending", "provenance": "ai_generated",
         "generator": "sdxl", "content_type": "image/png", "created_at": None},
    ]))
    result = _run("list_property_photos", conn,
                  listing_id="33333333-3333-3333-3333-333333333333")

    assert result["count"] == 2
    assert result["ai_generated_count"] == 1
    assert "must be labelled" in result["provenance_note"]


def test_agent_performance_states_the_formula_and_the_gap():
    conn = _Conn(
        ("FROM clients", {"total": 10, "closed": 2, "lost": 1, "open": 7}),
        ("FROM transactions", {"total": 4, "closed": 1, "closed_volume": 200000,
                               "open_volume": 600000}),
        ("count(*)::int FROM showings", 12),
    )
    result = _run("get_agent_performance", conn)

    assert result["conversion_rate_pct"] == 20.0
    assert "÷" in result["conversion_rate_formula"]
    assert result["average_margin"] is None
    assert "pipeline_attribution" in result["not_recorded"]
    assert result["attribution_basis"]["transactions"] == "transactions.created_by"


def test_a_deal_from_another_workspace_is_not_found():
    conn = _Conn()
    for tool in ("list_closing_checklist", "get_transaction_workflow",
                 "get_deal_financial_summary"):
        result = _run(tool, conn, deal_id="22222222-2222-2222-2222-222222222222")
        assert result["ok"] is False
        assert "not in this workspace" in result["error"], tool


# ---------------------------------------------------------------------------
# Ops reads — offered to everyone, answered for the broker-owner
# ---------------------------------------------------------------------------

_OPS_TOOLS = ("get_tenant_health", "get_job_queue", "get_audit_trail",
              "list_integration_status", "get_billing_status",
              "list_billing_invoices", "list_recent_errors",
              "get_database_stats", "run_health_check")


def _ctx(role):
    return TenantContext(agent_id="agent@tenant.test",
                         tenant_id=CTX.tenant_id, role=role)


@pytest.mark.parametrize("tool", _OPS_TOOLS)
def test_an_ops_read_refuses_an_agent_and_names_the_role(tool):
    """The allowlist decides what the model is offered; require_role decides
    what this tenant may do. Hiding the tool from an agent would make the
    assistant claim the capability does not exist, which is a different and
    false statement from "that is not your information"."""
    conn = _Conn()
    result = asyncio.run(tools.execute(conn, _ctx(Role.AGENT), tool, {}))

    assert result["ok"] is False
    assert result["required_role"] == "broker_owner"
    assert "agent" in result["error"]
    assert not conn.queries, "the refusal ran a query anyway"


@pytest.mark.parametrize("tool", _OPS_TOOLS)
def test_the_ops_tools_are_still_offered_to_an_agent(tool):
    import ai_chat_store

    assert ai_chat_store.is_agent_tool_available(tool)


def test_a_broker_owner_gets_the_answer():
    conn = _Conn(("(SELECT count(*) FROM clients",
                  {"clients": 12, "leads": 40, "open_transactions": 3,
                   "failed_jobs": 1, "pending_approvals": 2}))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_tenant_health", {}))

    assert result["ok"] is True
    assert result["counts"]["failed_jobs"] == 1
    assert result["scope"] == "this workspace only"


def test_tenant_health_reads_the_lead_rollup_rather_than_counting_the_table():
    """count(*) on leads measured 10,185 ms here — an index-only scan over 6.9M
    entries, on a tool an agent can call at will. Migration 0038 maintains an
    exact rollup by trigger; it answers in 0.28 ms."""
    conn = _Conn(("(SELECT count(*) FROM clients", {"clients": 1, "leads": 40,
                  "open_transactions": 0, "failed_jobs": 0, "pending_approvals": 0}))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_tenant_health", {}))

    query = conn.queries[0]
    assert "FROM lead_pipeline_counts" in query
    assert "count(*) FROM leads" not in query
    assert "lead_pipeline_counts" in result["counts_source"]["leads"]


def test_the_job_queue_answers_about_the_queue_not_about_the_jobs_contents():
    """A job payload carries whatever the job was going to do — an email body, a
    contract's inputs. "How is the queue doing" does not ask for that."""
    conn = _Conn(("FROM automation_jobs", [
        {"job_type": "command:execute", "state": "failed", "risk_class": "outreach",
         "attempt_count": 3, "max_attempts": 5, "last_error_code": "provider_error",
         "scheduled_at": None, "started_at": None, "completed_at": None,
         "created_at": None},
    ]))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_job_queue", {}))

    assert result["count"] == 1
    assert "automation_jobs.payload" in result["excluded_fields"]
    assert not any("payload" in query for query in conn.queries)


def test_the_audit_trail_withholds_entry_metadata_and_the_chain_hash():
    conn = _Conn(("FROM audit_ledger", []))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_audit_trail", {}))

    assert "audit_ledger.metadata" in result["excluded_fields"]
    assert not any("metadata" in query for query in conn.queries)


def test_integration_status_never_selects_a_credential():
    conn = _Conn(("FROM provider_credentials", []))
    asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                              "list_integration_status", {}))

    joined = " ".join(conn.queries)
    assert "token_ciphertext" not in joined
    assert "refresh_ciphertext" not in joined


def test_contract_templates_say_when_none_is_approved():
    """draft_contract needs an approved template key, and a workspace with none
    should hear that rather than a bare empty list."""
    conn = _Conn(("FROM contract_templates", [
        {"template_key": "seller-purchase-standard", "version": "1.0",
         "document_type": "seller_purchase", "jurisdiction": "DE",
         "status": "draft", "attorney_reviewed_by": None,
         "attorney_reviewed_at": None, "required_fields": [], "updated_at": None},
    ]))
    result = asyncio.run(tools.execute(conn, CTX, "list_contract_templates", {}))

    assert result["approved_count"] == 0
    assert "No template in this workspace is approved" in result["note"]


def test_feature_flags_are_readable_by_an_agent_unlike_the_other_ops_tools():
    """The other ops tools report on the workspace's operations, which is the
    broker-owner's business. This one reports what the product can do, and an
    assistant that cannot see its own deployment will describe it wrongly."""
    result = asyncio.run(tools.execute(_Conn(), _ctx(Role.AGENT),
                                       "get_feature_flags", {}))
    assert result["ok"] is True
    assert "required_role" not in result


def test_a_flag_whose_call_site_defaults_off_is_reported_off(monkeypatch):
    """feature_enabled(SPEED_TO_LEAD) returns True for an unset env var, but
    speed_to_lead._enabled() reads default=False and keeps it off. It is the one
    feature that contacts a consumer unprompted, so reporting it as on would be
    the worst possible flag to get wrong."""
    monkeypatch.delenv("ORACLE_FEATURE_SPEED_TO_LEAD", raising=False)
    result = asyncio.run(tools.execute(_Conn(), CTX, "get_feature_flags", {}))

    speed = next(f for f in result["flags"] if f["feature"] == "speed_to_lead")
    assert speed["enabled"] is False
    assert speed["differs_from_generic_default"] is True
    assert "defaults OFF at its call site" in speed["note"]

    marketplace = next(f for f in result["flags"] if f["feature"] == "marketplace")
    assert marketplace["enabled"] is True
    assert marketplace["differs_from_generic_default"] is False


def test_an_explicit_env_value_wins_over_the_call_site_note(monkeypatch):
    monkeypatch.setenv("ORACLE_FEATURE_SPEED_TO_LEAD", "1")
    result = asyncio.run(tools.execute(_Conn(), CTX, "get_feature_flags", {}))

    speed = next(f for f in result["flags"] if f["feature"] == "speed_to_lead")
    assert speed["env_set"] is True
    assert speed["enabled"] is True
    assert speed["differs_from_generic_default"] is False


def test_billing_status_says_the_card_is_not_here_rather_than_omitting_it():
    conn = _Conn(
        ("FROM subscriptions", {"status": "active", "plan": "standard",
                                "current_period_end": None, "has_customer": True,
                                "has_subscription": True, "updated_at": None}),
        ("FROM billing_usage_events", {"n": 4, "failed": 1}),
    )
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_billing_status", {}))

    assert result["subscription"]["plan"] == "standard"
    assert result["unreported_usage_events"] == 4
    assert result["usage_events_with_report_errors"] == 1
    assert "payment_method" in result["not_available"]
    # The processor's ids are present/absent, never returned by value.
    joined = " ".join(conn.queries)
    assert "stripe_customer_id IS NOT NULL" in joined


def test_a_missing_subscription_is_not_reported_as_an_unpaid_one():
    conn = _Conn(("FROM billing_usage_events", {"n": 0, "failed": 0}))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_billing_status", {}))

    assert result["subscription"] is None
    assert "never set up here" in result["subscription_note"]


def test_no_invoice_is_reconstructed_from_local_usage():
    """An invoice assembled from usage rows would not be the document the
    brokerage was actually billed."""
    conn = _Conn(("FROM billing_usage_events", [
        {"metric": "ai_messages", "quantity": 120, "event_count": 12,
         "first_occurred_at": None, "last_occurred_at": None},
    ]))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "list_billing_invoices", {}))

    assert result["invoices"] == []
    assert "not mirror issued invoices" in result["invoices_unavailable_reason"]
    assert result["unreported_usage_by_metric"][0]["metric"] == "ai_messages"
    assert "not an invoice" in result["unreported_usage_note"]


def test_recent_errors_never_select_the_free_text_or_the_caller_address():
    conn = _Conn(("FROM automation_jobs", []), ("FROM audit_anomaly_alerts", []))
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "list_recent_errors", {}))

    joined = " ".join(conn.queries)
    assert "last_error_code" in joined
    assert "last_error," not in joined
    assert "evidence" not in joined
    assert "source_ip" not in joined
    assert "automation_jobs.last_error" in result["excluded_fields"]
    # The catalog claimed an application log. There isn't one.
    assert "only logged to stdout" in result["not_covered"]


def test_database_stats_admit_that_no_migration_ledger_exists():
    """Dev has no schema_migrations table. "Files on disk" is not evidence that
    they ran, and a version number invented from the filenames would be."""
    conn = _Conn(
        ("to_regclass", None),
        ("FROM lead_pipeline_counts", {"leads": 100, "clients": 4,
                                       "transactions": 0, "media": 1}),
    )
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "get_database_stats", {}))

    assert result["migration_ledger_present"] is False
    assert result["migrations_applied"] is None
    assert "not evidence that they ran" in result["migration_note"]


def test_database_wide_table_stats_are_withheld_from_a_broker_owner():
    """Table sizes are database-wide. Handing them to one tenant leaks the
    platform's aggregate scale and every other tenant's data volume."""
    rows = [{"table_name": "leads", "seq_scan": 3, "idx_scan": 900,
             "n_live_tup": 6_900_000, "total_size": "12 GB"}]
    routes = (("to_regclass", None),
              ("FROM lead_pipeline_counts", {"leads": 100, "clients": 4,
                                             "transactions": 0, "media": 1}),
              ("FROM pg_stat_user_tables", rows))

    owner = asyncio.run(tools.execute(_Conn(*routes), _ctx(Role.BROKER_OWNER),
                                      "get_database_stats", {}))
    assert "platform_tables" not in owner

    admin = asyncio.run(tools.execute(_Conn(*routes), _ctx(Role.PLATFORM_ADMIN),
                                      "get_database_stats", {}))
    assert admin["platform_tables"][0]["table_name"] == "leads"


def test_the_health_check_says_which_services_it_did_not_contact():
    """Reporting "payment processor: OK" without calling it is worse than saying
    it did not look."""
    conn = _Conn(
        ("SELECT 1", 1),
        ("count(*)::int FROM automation_jobs", 2),
        ("FROM provider_credentials", [{"provider": "twilio",
                                        "validation_status": "invalid",
                                        "disabled_at": None}]),
    )
    result = asyncio.run(tools.execute(conn, _ctx(Role.BROKER_OWNER),
                                       "run_health_check", {}))

    assert result["database"]["reachable"] is True
    assert isinstance(result["database"]["roundtrip_ms"], float)
    assert result["failed_jobs"] == 2
    assert result["unhealthy_providers"][0]["provider"] == "twilio"
    assert set(result["checks_not_performed"]) == {
        "payment_processor", "mls_feed", "inference_provider",
        "mail_transport", "cache"}
    assert "not whether the service is up now" in result["checks_not_performed_reason"]
