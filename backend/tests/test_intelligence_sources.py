"""The listing that made intelligence authoring possible, and the licence gate.

Every POST on /api/intelligence requires at least one `source_record_id` that
`_verified_citations()` can resolve against `source_records`. Nothing listed
that table, so a person using the product could not discover the UUIDs an
analysis must cite — thirteen routes were unreachable behind one absent SELECT.
These tests cover the listing and the two refusals that keep it honest.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import HTTPException

import intelligence_api
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
REC_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REC_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
OBSERVED = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
RETRIEVED = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)


def _row(record_id=REC_A, *, property_level_allowed=True, outreach_use_allowed=False,
         purged_at=None, property_key="DE-NCC-0142"):
    return {
        "id": record_id,
        "source_key": "de-newcastle-assessor",
        "record_key": "NCC-2026-000142",
        "property_key": property_key,
        "jurisdiction": "DE",
        "observed_at": OBSERVED,
        "retrieved_at": RETRIEVED,
        "expires_at": None,
        "purged_at": purged_at,
        "source_name": "New Castle County Assessor",
        "source_url": "https://data.newcastlede.gov/parcels",
        "license_name": "municipal-open-data",
        "property_level_allowed": property_level_allowed,
        "outreach_use_allowed": outreach_use_allowed,
    }


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.rows


def _fake_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


async def _call(conn, *, property_key=None, jurisdiction=None, source_key=None, limit=50):
    """Invoke the route the way FastAPI does.

    Calling the coroutine directly leaves the `Query(...)` sentinels in place of
    their defaults, and a sentinel is truthy — which would make the
    "no filter given" guard silently pass. Every parameter is therefore passed
    explicitly here, as the resolved values the server always sees.
    """
    return await intelligence_api.citable_sources(
        property_key=property_key,
        jurisdiction=jurisdiction,
        source_key=source_key,
        limit=limit,
        ctx=CTX,
    )


# ── the listing ──────────────────────────────────────────────────────────────

def test_listing_returns_a_body_that_can_be_posted_back_unchanged(monkeypatch):
    """`cite` must validate as EvidenceInput with nothing added or missing.

    SourceCitation sets extra="forbid", so a client that spread the listing's
    metadata into a POST body would be rejected. The contract this test pins is
    that `row["cite"]` goes straight into `sources` — no field stripping in the
    client, which is the rule that would silently drift.
    """
    conn = _Conn([_row()])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    payload = asyncio.run(_call(conn, property_key="DE-NCC-0142"))

    assert payload["count"] == 1
    entry = payload["citable"][0]
    assert entry["source_key"] == "de-newcastle-assessor"
    assert entry["property_level_allowed"] is True
    # Open data being public does not make it lawful outreach material, so the
    # harvester default is False and the listing has to say so.
    assert entry["outreach_use_allowed"] is False
    assert entry["payload_purged"] is False

    evidence = intelligence_api.EvidenceInput.model_validate(entry["cite"])
    assert evidence.source_record_id == REC_A
    assert evidence.source == "New Castle County Assessor"
    assert evidence.record_id == "NCC-2026-000142"
    assert evidence.observed_at == OBSERVED.date()
    assert evidence.license == "municipal-open-data"


def test_listing_refuses_an_unfiltered_call_rather_than_scanning():
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_call(None))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "FILTER_REQUIRED"


def test_jurisdiction_alone_is_enough_because_market_evidence_has_no_property(monkeypatch):
    """A market forecast cites jurisdiction-wide rows whose property_key is NULL."""
    conn = _Conn([_row(property_key=None)])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    payload = asyncio.run(_call(conn, jurisdiction="DE"))

    assert payload["count"] == 1
    assert payload["citable"][0]["property_key"] is None
    assert payload["property_key"] is None


def test_purged_records_are_offered_and_flagged_not_hidden(monkeypatch):
    """Retention wipes raw_payload but keeps the hash — provenance survives.

    A purged record still proves an observation happened, so it stays citable;
    hiding it would quietly shrink the evidence a long-standing property can
    show. The flag is what lets the UI say the payload can no longer be re-read.
    """
    conn = _Conn([_row(purged_at=datetime(2026, 8, 20, tzinfo=timezone.utc))])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    payload = asyncio.run(_call(conn, property_key="DE-NCC-0142"))

    assert payload["count"] == 1
    assert payload["citable"][0]["payload_purged"] is True


def test_empty_result_is_a_count_not_an_error(monkeypatch):
    """"No observations retained" and "harvesters never ran" must look different
    from a broken screen, so an empty listing is a successful zero."""
    conn = _Conn([])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    payload = asyncio.run(_call(conn, property_key="DE-NCC-9999"))

    assert payload == {
        "property_key": "DE-NCC-9999",
        "jurisdiction": None,
        "limit": 50,
        "count": 0,
        "citable": [],
    }


# ── the licence gate ─────────────────────────────────────────────────────────

def test_property_analysis_refuses_a_source_whose_licence_forbids_property_use(monkeypatch):
    """source_licenses.property_level_allowed was written and never read.

    Every harvester leaves it True today, so this changes no current behaviour —
    it closes the gap before the first licensed feed arrives.
    """
    conn = _Conn([_row(property_level_allowed=False)])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(intelligence_api._verified_citations(CTX, [str(REC_A)]))

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "SOURCE_LICENSE_FORBIDS_PROPERTY_USE"
    assert excinfo.value.detail["source_record_ids"] == [str(REC_A)]


def test_market_analysis_accepts_the_same_source_because_no_property_is_described(monkeypatch):
    conn = _Conn([_row(property_level_allowed=False)])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    citations = asyncio.run(
        intelligence_api._verified_citations(CTX, [str(REC_A)], property_level=False)
    )

    assert len(citations) == 1
    assert citations[0].source == "New Castle County Assessor"


def test_an_invisible_source_record_is_named_rather_than_silently_dropped(monkeypatch):
    conn = _Conn([_row(REC_A)])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            intelligence_api._verified_citations(CTX, [str(REC_A), str(REC_B)])
        )

    assert excinfo.value.detail["code"] == "SOURCE_RECORD_NOT_VISIBLE"
    assert excinfo.value.detail["source_record_ids"] == [str(REC_B)]


def test_sources_route_is_declared_before_the_property_key_catch_all():
    """GET /{property_key} would otherwise match "sources" and look up a
    property by that name — a 200 with an empty analysis list, which is the
    worst possible failure because it looks like a working empty state."""
    paths = [getattr(route, "path", "") for route in intelligence_api.router.routes]
    assert paths.index("/api/intelligence/sources") < paths.index(
        "/api/intelligence/{property_key}"
    )


# ── the outreach licence ─────────────────────────────────────────────────────

def test_detectors_report_the_outreach_licence_instead_of_refusing(monkeypatch):
    """`outreach_use_allowed` was written since 0027 and read by nothing.

    Detectors is where public-record evidence becomes a list of people to
    approach, so it is where the answer belongs — but it reports rather than
    refuses. Every harvester leaves the flag False, so refusing would disable
    the endpoint outright; producing the candidates is legitimate, and acting
    on them is the step that needs the licence.
    """
    conn = _Conn([{"source_name": "New Castle County Assessor"}])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    blocked = asyncio.run(
        intelligence_api._outreach_blocked_sources(CTX, [str(REC_A), str(REC_A)])
    )

    assert blocked == ["New Castle County Assessor"]
    # De-duplicated before the query — a source cited twice is one lookup.
    assert conn.queries[0][1][0] == [str(REC_A)]


def test_a_permitting_licence_leaves_the_outreach_path_unblocked(monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    assert asyncio.run(
        intelligence_api._outreach_blocked_sources(CTX, [str(REC_A)])
    ) == []


def test_no_sources_means_no_query_rather_than_an_empty_ANY(monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(intelligence_api, "tenant_tx", _fake_tx(conn))

    assert asyncio.run(intelligence_api._outreach_blocked_sources(CTX, [])) == []
    assert conn.queries == []
