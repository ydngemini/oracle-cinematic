"""Putting a reconstruction into metres — and refusing when nothing can.

A photogrammetric cloud is the right shape and an arbitrary size. The pipeline
refuses without an anchor on purpose, because a guessed scale multiplies every
length and area by one constant and looks entirely correct doing it. These tests
defend the order the anchors are tried in, the bounds each is sanity-checked
against, and the refusal.
"""

from __future__ import annotations

import asyncio
import types

import pytest

import reconstruction_scale as scale


class _Row(dict):
    """asyncpg rows are mapping-like; dict is close enough for these."""


class _Conn:
    def __init__(self, row=None, value=None):
        self._row, self._value = row, value
        self.queries = []

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return self._row

    async def fetchval(self, sql, *args):
        return self._value


def _candidate(area, source="openstreetmap"):
    return types.SimpleNamespace(
        area_sqm=area, source=source, attribution="© OpenStreetMap contributors",
    )


@pytest.fixture
def footprints(monkeypatch):
    """Stand in for the licensed/OSM lookup."""
    holder = {"candidates": [], "raises": False}

    async def _resolve(*, address="", **kwargs):
        if holder["raises"]:
            raise RuntimeError("overpass is down")
        return holder["candidates"]

    module = types.SimpleNamespace(resolve_footprints=_resolve)
    package = types.SimpleNamespace(building_footprint=module)
    monkeypatch.setitem(__import__("sys").modules, "data_integrations", package)
    monkeypatch.setitem(
        __import__("sys").modules, "data_integrations.building_footprint", module
    )
    return holder


# ---------------------------------------------------------------------------
# Which anchor, and why that order
# ---------------------------------------------------------------------------

def test_the_building_footprint_is_preferred_over_recorded_area(footprints):
    """An outline measured against an outline does not care how many storeys the
    building has. Living area does — it counts every floor, and a plan is one."""
    footprints["candidates"] = [_candidate(140.0)]
    conn = _Conn(_Row(address="1 Test St", sqft=1800))

    anchor = asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1"))

    assert anchor.kind == "parcel_footprint_m2"
    assert anchor.value == 140.0
    assert "openstreetmap" in anchor.source


def test_recorded_area_is_used_when_no_footprint_matches(footprints):
    footprints["candidates"] = []
    conn = _Conn(_Row(address="1 Test St", sqft=1800))

    anchor = asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1"))

    assert anchor.kind == "known_total_sqft"
    assert anchor.value == 1800.0
    assert "storey" in anchor.caveat, "the multi-storey hazard has to travel with it"


def test_a_footprint_provider_outage_falls_through_rather_than_failing(footprints):
    """The lookup is a network call to somebody else's server. It failing is not
    a reason to produce no plan when the record holds a usable figure."""
    footprints["raises"] = True
    conn = _Conn(_Row(address="1 Test St", sqft=1800))

    anchor = asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1"))

    assert anchor is not None and anchor.kind == "known_total_sqft"


def test_no_anchor_means_no_plan(footprints):
    """The honest outcome. A plan with no anchor is a document full of confident
    measurements that are all wrong by the same factor."""
    footprints["candidates"] = []
    conn = _Conn(_Row(address="", sqft=None))

    assert asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1")) is None


def test_a_listing_has_no_recorded_area_to_fall_back_on(footprints):
    """`listings` carries no square footage column at all, so a listing-backed
    capture has only the footprint route. A schema fact, not an oversight."""
    footprints["candidates"] = []
    conn = _Conn(_Row(address="9 Listing Ave"))

    assert asyncio.run(scale.resolve_anchor(conn, listing_id="listing-1")) is None


# ---------------------------------------------------------------------------
# Sanity bounds — a number in the right column is not automatically a building
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("area", [4.0, 5_000.0])
def test_an_implausible_footprint_is_skipped(footprints, area):
    footprints["candidates"] = [_candidate(area)]
    conn = _Conn(_Row(address="1 Test St", sqft=1800))

    anchor = asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1"))

    assert anchor.kind == "known_total_sqft", f"{area} m² was accepted as a dwelling"


@pytest.mark.parametrize("sqft", [12, 90_000])
def test_an_implausible_square_footage_is_refused(footprints, sqft):
    footprints["candidates"] = []
    conn = _Conn(_Row(address="", sqft=sqft))

    assert asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1")) is None


def test_a_better_footprint_wins_over_a_rejected_one(footprints):
    """Candidates arrive best-first; a garden shed at the top must not shadow
    the house behind it."""
    footprints["candidates"] = [_candidate(3.0), _candidate(155.0, "regrid")]
    conn = _Conn(_Row(address="1 Test St", sqft=None))

    anchor = asyncio.run(scale.resolve_anchor(conn, lead_id="lead-1"))

    assert anchor.kind == "parcel_footprint_m2" and anchor.value == 155.0


# ---------------------------------------------------------------------------
# The cross-check — the only thing that can see a partial capture
# ---------------------------------------------------------------------------

def _document(total_sqft):
    return types.SimpleNamespace(total_sqft=total_sqft)


def test_a_plan_far_from_the_record_says_so():
    """Neither anchor can detect a capture that covered one room: solved against
    a footprint, the plan matches that footprint by construction. A second,
    independent figure is what catches it."""
    anchor = scale.ScaleAnchor("parcel_footprint_m2", 140.0, "a footprint")

    note = scale.cross_check(_document(400), anchor, 1800)

    assert note is not None
    assert "400" in note and "1,800" in note
    assert "part of the property" in note


def test_a_plan_close_to_the_record_says_nothing():
    """The two numbers measure different things — living area counts every
    storey — so a modest gap is expected and not worth alarming about."""
    anchor = scale.ScaleAnchor("parcel_footprint_m2", 140.0, "a footprint")

    assert scale.cross_check(_document(1650), anchor, 1800) is None


def test_an_anchor_cannot_cross_check_itself():
    """A plan solved against the recorded square footage will always agree with
    the recorded square footage. Comparing them would be theatre."""
    anchor = scale.ScaleAnchor("known_total_sqft", 1800.0, "the lead")

    assert scale.cross_check(_document(400), anchor, 1800) is None


def test_the_anchor_describes_itself_for_provenance():
    anchor = scale.ScaleAnchor(
        "parcel_footprint_m2", 140.0, "a 140 m² footprint from openstreetmap",
        caveat="It assumes the capture covers the whole building.",
    )

    described = anchor.describe()

    assert "140 m²" in described and "whole building" in described
    assert anchor.as_kwargs() == {"parcel_footprint_m2": 140.0}


# ---------------------------------------------------------------------------
# The wiring — a plan is a bonus, never a way to lose a good reconstruction
# ---------------------------------------------------------------------------

def _job(tmp_path):
    return types.SimpleNamespace(
        ctx=types.SimpleNamespace(tenant_id="t", agent_id="a"),
        job_id="job-1", lead_id="11111111-1111-1111-1111-111111111111",
        listing_id=None,
    )


def _splat_with_cloud(tmp_path):
    import capture_sidecars

    splat = tmp_path / "model.sog"
    splat.write_bytes(b"compressed")
    capture_sidecars.points_sidecar_for(splat).write_bytes(b"ply\n")
    return splat


@pytest.mark.parametrize("boom", [
    RuntimeError("the footprint service exploded"),
    MemoryError("out of memory mid-extraction"),
])
def test_a_broken_plan_never_fails_the_reconstruction(tmp_path, monkeypatch, boom):
    """The splat is the deliverable and it has already succeeded. A missing
    anchor, an unmeasurable capture, or a provider having a bad afternoon must
    not turn a good reconstruction into a failed job."""
    import reconstruction_worker as worker

    monkeypatch.setattr(worker, "FLOORPLAN_FROM_RECONSTRUCTION", True)

    def _explode(*a, **k):
        raise boom

    monkeypatch.setattr(worker, "tenant_tx", _explode)

    # Must return, not raise.
    asyncio.run(worker._derive_floorplan(_job(tmp_path), _splat_with_cloud(tmp_path), "runpod_pod"))


def test_no_point_cloud_means_no_attempt(tmp_path, monkeypatch):
    """Delivery is .sog and `parse_ply` cannot read it. Without the points
    sidecar there is nothing to measure, and that is not an error."""
    import reconstruction_worker as worker

    monkeypatch.setattr(worker, "FLOORPLAN_FROM_RECONSTRUCTION", True)
    reached = []
    monkeypatch.setattr(worker, "tenant_tx", lambda *a, **k: reached.append(1))

    splat = tmp_path / "model.sog"
    splat.write_bytes(b"compressed")          # no .points.ply beside it
    asyncio.run(worker._derive_floorplan(_job(tmp_path), splat, "runpod_pod"))

    assert reached == [], "it went looking for an anchor with nothing to measure"


def test_the_switch_turns_it_off(tmp_path, monkeypatch):
    """Writing a floor plan into the CRM is a visible act, so it has a switch."""
    import reconstruction_worker as worker

    monkeypatch.setattr(worker, "FLOORPLAN_FROM_RECONSTRUCTION", False)
    reached = []
    monkeypatch.setattr(worker, "tenant_tx", lambda *a, **k: reached.append(1))

    asyncio.run(worker._derive_floorplan(_job(tmp_path), _splat_with_cloud(tmp_path), "runpod_pod"))

    assert reached == []


def test_a_reconstruction_derived_plan_is_accepted_by_the_api_model():
    """The pipeline produces source="reconstruction" and both the API validator
    and the DB CHECK rejected it, so a plan derived from a capture could never
    be saved. Nothing surfaced it until a reconstruction actually tried to write
    one — until this branch, nothing did.

    Kept distinct from "ai_vision" on purpose: one is measured 3D structure
    sliced horizontally, the other a model's guess from flat photos, and one
    word for both puts invented and measured geometry together on a surface that
    feeds rehab costing.
    """
    pytest.importorskip("cv2")
    import floorplan_api

    assert "reconstruction" in floorplan_api._VALID_SOURCES

    document = floorplan_api.FloorplanDocumentIn.model_validate({
        "schema_version": 1,
        "units": "metric",
        "levels": [{"id": "l1", "name": "Ground Floor", "index": 0}],
        "walls": [{"id": "w1", "start": [0.0, 0.0], "end": [4.0, 0.0],
                   "thickness": 0.1, "height": 2.5, "levelId": "l1"}],
        "rooms": [{"id": "r1", "name": "Room 1", "type": "other",
                   "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]],
                   "levelId": "l1"}],
        "openings": [],
        "provenance": {
            "source": "reconstruction",
            "ai_generated": True,
            "model_version": "floorplan-mask-1.0.0",
            "confidence": 0.5,
        },
    })

    assert document.provenance.source == "reconstruction"


def test_the_database_check_allows_it_too():
    """A validator that accepts what the table refuses just moves the failure
    one layer down, into a transaction that has already done work."""
    from pathlib import Path

    migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / \
        "0080_floorplan_reconstruction_source.sql"
    assert migration.is_file(), "the CHECK was never widened"
    sql = migration.read_text()
    assert "reconstruction" in sql
    assert "DROP CONSTRAINT IF EXISTS" in sql, "re-running the migration must be safe"
