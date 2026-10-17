"""Regression tests for the shared public-property catalog search path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

import mls_portal
import harvesters.base
import harvesters.fl_fdor
from harvesters.property_adapter import PropertyRecord
from tenancy import Role, TenantContext


class _FakeConnection:
    def __init__(self) -> None:
        self.count_query = ""
        self.count_args: tuple = ()
        self.data_query = ""
        self.data_args: tuple = ()
        self.lead_lookup_query = ""
        self.lead_lookup_args: tuple = ()

    async def fetchrow(self, query: str, *args):
        self.count_query = query
        self.count_args = args
        return {"n": 34_999}

    async def fetch(self, query: str, *args):
        # The catalog page and the tenant-lead resolution are separate queries
        # on the same connection. Record only the catalog one, or the assertions
        # below silently start describing the lead lookup instead.
        if " FROM leads\n" in query:
            self.lead_lookup_query = query
            self.lead_lookup_args = args
            return []
        self.data_query = query
        self.data_args = args
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "public_record_id": "11111111-1111-1111-1111-111111111111",
                "parcel_id": "safe-parcel",
                "state": "DE",
                "source_key": "firehose:DE",
                "source_metadata": (
                    '{"published_field_sources":{"building_area_sqft":"SQFT"}}'
                ),
                "source_name": "New Castle County parcels + ownership",
                "coverage_scope": "county:New Castle",
                "detail_level": "standard",
                "observed_fields": ["parcel_id", "state"],
                "verification_required": True,
                "record_refreshed_at": datetime.now(timezone.utc),
                "match_score": 0,
                "match_type": "browse",
                "sqft": 1400,
                "address": "",
                "city": "",
                "zip": "",
                "county": "",
                "owner_name": "",
                "owner_type": "",
                "last_sale_date": None,
                "is_absentee": False,
                "price": None,
                "equity_percent": None,
                "distress_flags": [],
            }
        ]


def _run_search(monkeypatch, ctx: TenantContext, **kwargs):
    conn = _FakeConnection()
    params = {
        "city": None,
        "state": None,
        "zip": None,
        "min_price": None,
        "max_price": None,
        "beds": None,
        "q": None,
        "page": 1,
    }
    params.update(kwargs)

    @asynccontextmanager
    async def fake_tenant_tx(received_ctx):
        assert received_ctx == ctx
        yield conn

    monkeypatch.setattr(mls_portal, "tenant_tx", fake_tenant_tx)
    result = asyncio.run(mls_portal.mls_pipeline_search(ctx=ctx, **params))
    return result, conn


def test_pipeline_default_count_uses_a_planner_estimate_not_a_full_scan(monkeypatch):
    """An unfiltered browse has no WHERE to bound it, so an exact COUNT(*)
    means a full scan of the whole catalog on every page load — measured
    against the real table at 6.3M rows / 4.7GB, and the query that OOM'd a
    7.6GB dev box running it directly. `pg_class.reltuples` costs ~15ms
    because it reads a statistic instead of the table, and the response must
    say the total is an estimate rather than presenting it as exact — the
    same "never assert a positive fact the data can't support" rule the
    honesty tests elsewhere in this suite pin.
    """
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    result, conn = _run_search(monkeypatch, ctx)

    assert "reltuples" in conn.count_query
    assert "pg_class" in conn.count_query
    assert "FROM public_property_records" not in conn.count_query, (
        "the unfiltered path is doing a real table scan again"
    )
    assert conn.count_args == ()
    assert "ORDER BY match_score DESC, record_refreshed_at DESC, id ASC" in conn.data_query
    # LIMIT is PAGE_SIZE+1: has_more is read off the fetched rows, never off
    # the (possibly estimated) total — see the has_more tests below.
    assert conn.data_args == (mls_portal.PAGE_SIZE + 1, 0)
    assert result["total"] == 34_999
    assert result["total_is_estimate"] is True
    # The fake returns exactly one row regardless of LIMIT, i.e. fewer than
    # PAGE_SIZE+1 — this is the last-page case, and has_more must reflect
    # that even though `total` (34,999) would suggest otherwise.
    assert result["has_more"] is False
    assert result["source"] == "shared public property catalog"
    assert result["coverage"]["jurisdictions_live"] == 51
    assert result["listings"][0]["fact_coverage"] == {
        "observed": ["square_feet"],
        "missing": [
            "assessor_value",
            "last_recorded_sale",
            "bedrooms",
            "bathrooms",
            "lot_square_feet",
            "year_built",
            "total_rooms",
        ],
        "observed_count": 1,
        "required_count": 8,
        "complete": False,
        "source_fields": {"building_area_sqft": "SQFT"},
        "policy": "source_published_only",
    }


def test_public_catalog_state_search_is_normalized_and_not_tenant_scoped(monkeypatch):
    tenant_id = "22222222-2222-2222-2222-222222222222"
    ctx = TenantContext(agent_id="agent", tenant_id=tenant_id, role=Role.AGENT)
    result, conn = _run_search(monkeypatch, ctx, state="de", page=2)

    # A state filter is index-bounded (idx_public_property_state_recent), so
    # unlike the unfiltered browse this keeps the exact COUNT(*) — a filtered
    # result set is cheap to count and the number is worth being exact about.
    assert "FROM public_property_records" in conn.count_query
    assert "tenant_id" not in conn.count_query
    assert "state = $1" in conn.count_query
    assert conn.count_args == ("DE",)
    assert result["total_is_estimate"] is False
    assert "tenant_id" not in conn.data_query
    assert "state = $1" in conn.data_query
    assert "ORDER BY match_score DESC, record_refreshed_at DESC, id ASC" in conn.data_query
    assert conn.data_args == ("DE", mls_portal.PAGE_SIZE + 1, mls_portal.PAGE_SIZE)


class _FakeConnectionNRows(_FakeConnection):
    """Returns exactly `row_count` catalog rows, so has_more can be exercised
    at both sides of the PAGE_SIZE+1 boundary. `total` is deliberately absurd
    (1) so a test relying on total-vs-offset arithmetic instead of the actual
    fetched-row count would fail loudly rather than passing by coincidence.
    """

    def __init__(self, row_count: int) -> None:
        super().__init__()
        self._row_count = row_count

    async def fetchrow(self, query: str, *args):
        self.count_query = query
        self.count_args = args
        return {"n": 1}

    async def fetch(self, query: str, *args):
        if " FROM leads\n" in query:
            self.lead_lookup_query = query
            self.lead_lookup_args = args
            return []
        self.data_query = query
        self.data_args = args
        base = {
            "id": "11111111-1111-1111-1111-111111111111",
            "public_record_id": "11111111-1111-1111-1111-111111111111",
            "parcel_id": "safe-parcel",
            "state": "DE",
            "source_key": "firehose:DE",
            "source_metadata": "{}",
            "source_name": "New Castle County parcels + ownership",
            "coverage_scope": "county:New Castle",
            "detail_level": "standard",
            "observed_fields": ["parcel_id", "state"],
            "verification_required": True,
            "record_refreshed_at": datetime.now(timezone.utc),
            "match_score": 0,
            "match_type": "browse",
            "sqft": 1400,
            "address": "",
            "city": "",
            "zip": "",
            "county": "",
            "owner_name": "",
            "owner_type": "",
            "last_sale_date": None,
            "is_absentee": False,
            "price": None,
            "equity_percent": None,
            "distress_flags": [],
        }
        return [dict(base) for _ in range(self._row_count)]


def _run_search_with_conn(monkeypatch, ctx, conn, **kwargs):
    params = {
        "city": None, "state": None, "zip": None, "min_price": None,
        "max_price": None, "beds": None, "q": None, "page": 1,
    }
    params.update(kwargs)

    @asynccontextmanager
    async def fake_tenant_tx(received_ctx):
        assert received_ctx == ctx
        yield conn

    monkeypatch.setattr(mls_portal, "tenant_tx", fake_tenant_tx)
    return asyncio.run(mls_portal.mls_pipeline_search(ctx=ctx, **params))


def test_has_more_is_read_from_fetched_rows_not_from_total(monkeypatch):
    """The whole point of fetching PAGE_SIZE+1: pagination must stay correct
    even when `total` is an estimate that disagrees with what was actually
    fetched. Both fakes here report the same absurd total=1; only the row
    count fetched decides has_more, and the returned page is trimmed back to
    PAGE_SIZE either way."""
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )

    full_page_plus_one = _run_search_with_conn(
        monkeypatch, ctx, _FakeConnectionNRows(mls_portal.PAGE_SIZE + 1)
    )
    assert full_page_plus_one["has_more"] is True
    assert len(full_page_plus_one["listings"]) == mls_portal.PAGE_SIZE

    exactly_one_short = _run_search_with_conn(
        monkeypatch, ctx, _FakeConnectionNRows(mls_portal.PAGE_SIZE)
    )
    assert exactly_one_short["has_more"] is False
    assert len(exactly_one_short["listings"]) == mls_portal.PAGE_SIZE


def test_public_catalog_complex_filter_keeps_exact_catalog_count(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    result, conn = _run_search(monkeypatch, ctx, city="Milford", min_price=100_000)

    assert "FROM public_property_records" in conn.count_query
    assert "lead_pipeline_counts" not in conn.count_query
    assert "city ILIKE $1" in conn.count_query
    assert conn.count_args == ("Milford", 100_000)
    assert result["total_is_estimate"] is False
    assert conn.data_args == ("Milford", 100_000, mls_portal.PAGE_SIZE + 1, 0)


def test_public_catalog_search_normalizes_exact_address_and_ranks_matches(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    result, conn = _run_search(
        monkeypatch,
        ctx,
        q="  15 Main St., Dover  ",
        state="de",
    )

    assert "search_document ILIKE $2" in conn.count_query
    assert "regexp_replace(lower(parcel_id)" in conn.count_query
    assert conn.count_args == ("DE", "%15 Main St., Dover%", "15mainstdover")
    assert "THEN 100" in conn.data_query
    assert "'parcel_exact'" in conn.data_query
    assert conn.data_args == (
        "DE",
        "%15 Main St., Dover%",
        "15mainstdover",
        "15 main st dover",
        mls_portal.PAGE_SIZE + 1,
        0,
    )
    # Among equal scores the row whose words line up comes first (L11-07).
    assert "ORDER BY match_score DESC, (btrim(regexp_replace(lower(COALESCE(address, ''))" in conn.data_query
    assert result["accuracy"]["ranked_exact_matches"] is True
    assert result["total_is_estimate"] is False


def test_exact_florida_match_runs_one_bounded_official_source_join(monkeypatch):
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="11111111-1111-1111-1111-111111111111",
        role=Role.PLATFORM_ADMIN,
    )
    calls: dict[str, object] = {}
    record = PropertyRecord(
        parcel_id="18466-053-000",
        address="11380 NE 211TH TER",
        city="WALDO",
        state="FL",
        zip_code="32694",
        owner_name="CARVER AARON ANDREW",
        owner_type="individual",
        estimated_value=262_530,
        equity_percent=0.0,
        is_absentee_owner=False,
        distress_flags=[],
        last_sale_date=None,
        county="Alachua",
        building_area_sqft=1_563,
        lot_area_sqft=43_560,
        year_built=2019,
    )

    async def fake_lookup(_self, parcel_id: str):
        calls["parcel_id"] = parcel_id
        return [record]

    async def fake_upsert(tenant_id, agent_id, records, *, metrics):
        calls["upsert"] = (tenant_id, agent_id, records, metrics)
        return 1

    monkeypatch.setattr(
        harvesters.fl_fdor.FloridaFDORHarvester,
        "lookup_parcel",
        fake_lookup,
    )
    monkeypatch.setattr(harvesters.base, "upsert_public_records", fake_upsert)
    reconciled = asyncio.run(
        mls_portal._reconcile_sparse_public_record(
            rows=[{
                "parcel_id": "18466-053-000",
                "state": "FL",
                "source_key": "firehose:FL",
                "match_score": 100,
                "county": None,
                "last_sale_price": None,
                "beds": None,
                "baths": None,
                "rooms": None,
                "year_built": None,
                "property_class": None,
                "land_use": None,
                "lot_area_sqft": None,
                "sqft": None,
                "source_metadata": {},
            }],
            ctx=ctx,
            exact_match_only=True,
        )
    )

    assert reconciled is True
    assert calls["parcel_id"] == "18466-053-000"
    tenant_id, agent_id, records, metrics = calls["upsert"]
    assert tenant_id == ctx.tenant_id
    assert agent_id == "fl-parcel-reconciler"
    assert records == [record]
    assert metrics["source_key"] == "firehose:FL"


def test_completed_targeted_enrichment_does_not_repeat_network_join():
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="11111111-1111-1111-1111-111111111111",
        role=Role.PLATFORM_ADMIN,
    )
    reconciled = asyncio.run(
        mls_portal._reconcile_sparse_public_record(
            rows=[{
                "parcel_id": "18466-053-000",
                "state": "FL",
                "source_key": "firehose:FL",
                "match_score": 100,
                "beds": None,
                "source_metadata": {
                    "targeted_enrichment": {"completed": True},
                },
            }],
            ctx=ctx,
            exact_match_only=True,
        )
    )

    assert reconciled is False


@pytest.mark.parametrize(
    ("match_type", "status"),
    [
        ("parcel_exact", "exact_parcel"),
        ("address_exact", "exact_address"),
        # A record opened by its id was compared with nothing the user typed.
        ("record_id", "not_applicable"),
        # Equal only once every space and punctuation mark is removed.
        ("address_compact", "possible_match"),
        ("parcel_compact", "possible_match"),
        ("address_partial", "possible_match"),
        ("owner_partial", "possible_match"),
        ("city_partial", "possible_match"),
        ("text_partial", "possible_match"),
        ("browse", "not_applicable"),
        # A match type this serializer does not know is never promoted to an
        # exact one.
        ("unrecognised_matcher", "possible_match"),
    ],
)
def test_public_record_identity_names_the_match_and_is_never_verified(match_type, status):
    """A partial hit is a candidate, not this property (AC11.04, F11.04).

    The Houses workspace opens the top search result and shows its owner of
    record beside a one-click "Link to client". A search for "Smith" can rank
    "John Smithers" first on owner_partial, so the payload has to say what
    matched. Nothing records a person confirming a parcel yet (T11.03), so
    even an exact key match is reported as not verified.
    """
    record = mls_portal._public_record_json({
        "id": "r1",
        "parcel_id": "P-1",
        "state": "DE",
        "owner_name": "Example Owner LLC",
        "match_type": match_type,
        "match_score": 85,
    })

    assert record["identity"] == {
        "status": status,
        "basis": match_type,
        "verified": False,
    }


def test_public_record_without_a_match_type_is_a_browse_row():
    record = mls_portal._public_record_json({"id": "r1", "parcel_id": "P-1", "state": "DE"})

    assert record["identity"] == {
        "status": "not_applicable",
        "basis": "browse",
        "verified": False,
    }


def test_property_detail_payload_reports_an_opened_record_as_unverified(monkeypatch):
    """The API contract AC11.04 names: the detail payload carries it too.

    Fetching a record by its id compares nothing with what the user meant,
    just as a browse row does. Calling it an exact identity would turn the top
    hit of an owner_partial search into an "exact" record for any client that
    reads it back through the detail endpoint.
    """
    ctx = TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    record_id = "11111111-1111-1111-1111-111111111111"

    class _Conn:
        async def fetchrow(self, query, *args):
            assert "'record_id' AS match_type" in query
            assert "record_id_exact" not in query
            return {
                "id": record_id,
                "parcel_id": "safe-parcel",
                "state": "DE",
                "source_key": "firehose:DE",
                "owner_name": "Example Owner LLC",
                "match_score": 0,
                "match_type": "record_id",
            }

        async def fetch(self, query, *args):
            return []

    @asynccontextmanager
    async def fake_tenant_tx(received_ctx):
        assert received_ctx == ctx
        yield _Conn()

    monkeypatch.setattr(mls_portal, "tenant_tx", fake_tenant_tx)
    detail = asyncio.run(mls_portal.mls_pipeline_listing(record_id=record_id, ctx=ctx))

    assert detail["identity"] == {
        "status": "not_applicable",
        "basis": "record_id",
        "verified": False,
    }


# ── A person's decision on the house (T11.03, 0168) ──────────────────────────
#
# `verified` is True only while the newest decision on (this lead, THIS record)
# is a confirmation for the client the house is still linked to. It never
# changes what the search matched: status and basis stay the search's claim.

_RECORD_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_RECORD_ID = "99999999-9999-9999-9999-999999999999"
_LEAD_ID = "22222222-2222-4222-8222-222222222222"
_HEAD_ID = "33333333-3333-4333-8333-333333333333"
_DECIDED_AT = "2026-10-17T16:00:00+00:00"


def _link(*heads, linked=True) -> dict:
    """What _lead_ids_for_records returns for the row's parcel."""
    return {("P-1", "DE"): {
        "lead_id": _LEAD_ID,
        "linked": linked,
        "heads_by_record": {h["record_id"]: h for h in heads},
    }}


def _head(decision="confirmed", *, same_client=True, record_id=_RECORD_ID) -> dict:
    return {"id": _HEAD_ID, "record_id": record_id, "decision": decision,
            "same_client": same_client, "decided_at": _DECIDED_AT}


def _decided_record(lead_ids, match_type="address_exact", *, can_decide=True) -> dict:
    return mls_portal._public_record_json(
        {"id": _RECORD_ID, "parcel_id": "P-1", "state": "DE", "match_type": match_type},
        lead_ids, can_decide=can_decide,
    )


def test_a_current_confirmation_verifies_this_record_and_keeps_the_search_claim():
    identity = _decided_record(_link(_head("confirmed")))["identity"]

    assert identity == {
        "status": "exact_address",
        "basis": "address_exact",
        "verified": True,
        "confirmation": {"state": "confirmed", "head_id": _HEAD_ID, "decided_at": _DECIDED_AT},
        "confirmable": True,
    }


def test_a_confirmed_possible_match_is_never_promoted():
    """The decision is about the house, not about what the user typed: an
    owner_partial hit stays a possible match (the L11-13 precedent)."""
    identity = _decided_record(_link(_head("confirmed")), "owner_partial")["identity"]

    assert (identity["status"], identity["basis"]) == ("possible_match", "owner_partial")
    assert identity["verified"] is True


def test_a_decision_on_another_record_of_the_same_parcel_says_nothing_about_this_one():
    identity = _decided_record(_link(_head("confirmed", record_id=_OTHER_RECORD_ID)))["identity"]

    assert identity["verified"] is False
    assert identity["confirmation"] == {"state": "none", "head_id": None, "decided_at": None}


def test_a_confirmation_for_the_houses_previous_client_is_out_of_date():
    identity = _decided_record(_link(_head("confirmed", same_client=False)))["identity"]

    assert identity["verified"] is False
    assert identity["confirmation"]["state"] == "out_of_date"
    assert identity["confirmation"]["head_id"] == _HEAD_ID


def test_a_rejection_reads_rejected_and_never_verified():
    identity = _decided_record(_link(_head("rejected")))["identity"]

    assert identity["verified"] is False
    assert identity["confirmation"]["state"] == "rejected"


def test_a_withdrawn_decision_is_undecided_and_keeps_its_head():
    """The next decision supersedes the withdrawal, so its id stays visible."""
    identity = _decided_record(_link(_head("withdrawn")))["identity"]

    assert identity["verified"] is False
    assert identity["confirmation"] == {"state": "none", "head_id": _HEAD_ID, "decided_at": _DECIDED_AT}


@pytest.mark.parametrize(("linked", "can_decide", "confirmable"),
                         [(True, True, True), (True, False, False), (False, True, False)])
def test_only_a_linked_house_is_confirmable_and_only_by_a_decider(linked, can_decide, confirmable):
    identity = _decided_record(_link(linked=linked), can_decide=can_decide)["identity"]

    assert identity["confirmable"] is confirmable
    assert identity["verified"] is False


def test_a_row_with_no_lead_keeps_exactly_the_search_claim():
    identity = _decided_record({}, "parcel_exact")["identity"]

    assert identity == {"status": "exact_parcel", "basis": "parcel_exact", "verified": False}


@pytest.mark.parametrize(("role", "confirmable"),
                         [(Role.BROKER_OWNER, True), (Role.AGENT, False), (Role.PLATFORM_ADMIN, False)])
def test_the_detail_route_reads_the_decision_through_the_lead_lookup(monkeypatch, role, confirmable):
    """The Houses detail card, end to end: the one lead query carries the
    head decisions (jsonb, as asyncpg returns it without a codec). A platform
    admin is not offered the form: their lead lookup spans every tenant, so
    the card's lead is not one they chose."""
    import json

    ctx = TenantContext(agent_id="owner", tenant_id="00000000-0000-0000-0000-000000000000", role=role)
    lead_queries = []

    class _Conn:
        async def fetchrow(self, query, *args):
            return {"id": _RECORD_ID, "parcel_id": "P-1", "state": "DE", "source_key": "firehose:DE",
                    "match_score": 0, "match_type": "record_id"}

        async def fetch(self, query, *args):
            lead_queries.append(query)
            return [{"parcel_id": "P-1", "state": "DE", "id": _LEAD_ID, "linked": True,
                     "heads": json.dumps([_head("confirmed")])}]

    @asynccontextmanager
    async def fake_tenant_tx(received_ctx):
        yield _Conn()

    monkeypatch.setattr(mls_portal, "tenant_tx", fake_tenant_tx)
    detail = asyncio.run(mls_portal.mls_pipeline_listing(record_id=_RECORD_ID, ctx=ctx))

    assert len(lead_queries) == 1
    assert detail["lead_id"] == _LEAD_ID
    assert detail["identity"]["status"] == "not_applicable"
    assert detail["identity"]["verified"] is True
    assert detail["identity"]["confirmable"] is confirmable


class _FakeMatchConnection:
    """Returns one catalog row labelled the way the search SQL labelled it.

    The SQL's exact branches compare compacted text, so the label is set here
    by hand; what is under test is what the payload then claims about it.
    """

    def __init__(self, row: dict, *more: dict) -> None:
        self._row = row
        self._more = more
        self.count_query = ""
        self.count_args: tuple = ()
        self.data_query = ""
        self.data_args: tuple = ()

    async def fetchrow(self, query: str, *args):
        self.count_query = query
        self.count_args = args
        return {"n": 1}

    async def fetch(self, query: str, *args):
        if " FROM leads\n" in query:
            return []
        self.data_query = query
        self.data_args = args
        return [dict(self._row), *(dict(r) for r in self._more)]


def _match_row(**fields) -> dict:
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "parcel_id": "safe-parcel",
        "state": "DE",
        "source_key": "firehose:DE",
        "owner_name": "Example Owner LLC",
        "address": "",
        "match_score": 98,
    }
    row.update(fields)
    return row


def _search_ctx() -> TenantContext:
    return TenantContext(
        agent_id="operator",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )


@pytest.mark.parametrize(
    ("query", "field", "value", "sql_match_type", "status", "basis"),
    [
        # Removing every space and slash makes a different house equal the
        # query: 15 1/2 Main St and 1512 Main St are two parcels.
        ("1512 Main St", "address", "15 1/2 MAIN ST", "address_exact", "possible_match", "address_compact"),
        ("22 2nd St", "address", "2 22ND ST", "address_exact", "possible_match", "address_compact"),
        ("12 1st St", "address", "1 21ST ST", "address_exact", "possible_match", "address_compact"),
        # Unit numbers are kept, so a unit split differently is not the one asked for.
        ("10 Elm St Apt 12", "address", "10 ELM ST APT 1-2", "address_exact", "possible_match", "address_compact"),
        # Case and punctuation runs still do not matter when the tokens agree.
        ("1512 Main St.", "address", "1512 MAIN ST", "address_exact", "exact_address", "address_exact"),
        ("10 Elm St, Apt 12", "address", "10 ELM ST APT 12", "address_exact", "exact_address", "address_exact"),
        ("10 Elm St #12", "address", "10 ELM ST 12", "address_exact", "exact_address", "address_exact"),
        # Parcel numbers: the same digits split differently are another parcel.
        ("12-345", "parcel_id", "123-45", "parcel_exact", "possible_match", "parcel_compact"),
        ("12-345", "parcel_id", "12-345", "parcel_exact", "exact_parcel", "parcel_exact"),
        ("12 345", "parcel_id", "12-345", "parcel_exact", "exact_parcel", "parcel_exact"),
        # A PIN printed with dashes and stored as bare digits (or the reverse)
        # has no boundaries to compare on one side: the digits decide.
        ("19-14-209-013-0000", "parcel_id", "19142090130000", "parcel_exact", "exact_parcel", "parcel_exact"),
        ("19142090130000", "parcel_id", "19-14-209-013-0000", "parcel_exact", "exact_parcel", "parcel_exact"),
        # A street name in another script is a word too; the SQL's compaction
        # drops it, the check does not.
        ("5 大街", "address", "5", "address_exact", "possible_match", "address_compact"),
        ("Calle Ñ 5", "address", "CALLE 5", "address_exact", "possible_match", "address_compact"),
        ("Calle Ñ 5", "address", "CALLE Ñ 5", "address_exact", "exact_address", "address_exact"),
    ],
)
def test_exact_match_requires_the_same_token_boundaries(
    monkeypatch, query, field, value, sql_match_type, status, basis,
):
    """An exact address or parcel claim needs the words to line up (AC11.04).

    HouseSelection opens the top hit. If "15 1/2 MAIN ST" is reported as an
    exact address for "1512 Main St", the workspace shows another parcel's
    owner as an address match, without the possible-match warning, beside a
    one-click "Link to client".
    """
    conn = _FakeMatchConnection(_match_row(**{field: value, "match_type": sql_match_type}))

    result = _run_search_with_conn(monkeypatch, _search_ctx(), conn, q=query)

    listing = result["listings"][0]
    assert listing["identity"] == {"status": status, "basis": basis, "verified": False}
    assert listing["match_type"] == basis


@pytest.mark.parametrize("query", ["#", "-", " / ", "ñ", "大街"])
def test_query_with_no_letters_or_digits_never_matches_exactly(monkeypatch, query):
    """An empty compact query equals every blank address and parcel.

    The search must not add the exact branches for it at all; otherwise every
    record without an address ranks 98 as an "Address match" above every real
    partial hit.
    """
    # What the old SQL returned for such a query: a record with no address,
    # labelled address_exact.
    conn = _FakeMatchConnection(_match_row(address=None, match_type="address_exact"))

    result = _run_search_with_conn(monkeypatch, _search_ctx(), conn, q=query)

    assert "" not in conn.count_args
    assert "" not in conn.data_args
    assert "regexp_replace(lower(COALESCE(address" not in conn.count_query
    assert "'address_exact'" not in conn.data_query
    assert "'parcel_exact'" not in conn.data_query
    assert result["listings"][0]["identity"]["status"] == "possible_match"


def test_record_with_a_blank_address_is_never_an_exact_address(monkeypatch):
    conn = _FakeMatchConnection(_match_row(address="  ", match_type="address_exact"))

    result = _run_search_with_conn(monkeypatch, _search_ctx(), conn, q="15 Main St")

    assert result["listings"][0]["identity"]["status"] == "possible_match"


def test_unseparated_digits_split_two_ways_in_the_results_are_both_possible(monkeypatch):
    """'12345' typed bare matches parcel 12-345 and parcel 123-45: the digits
    alone cannot say which, so neither is an exact parcel (L11-07)."""
    conn = _FakeMatchConnection(
        _match_row(parcel_id="12-345", match_type="parcel_exact", match_score=100),
        _match_row(id="22222222-2222-2222-2222-222222222222", parcel_id="123-45",
                   match_type="parcel_exact", match_score=100),
    )
    result = _run_search_with_conn(monkeypatch, _search_ctx(), conn, q="12345")
    assert [l["identity"]["status"] for l in result["listings"]] == ["possible_match", "possible_match"]
