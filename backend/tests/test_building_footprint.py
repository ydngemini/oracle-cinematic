"""Address → building footprint, from licensed and openly-licensed sources.

This is the input `floorplan_pipeline.extract_from_parcel_geometry` was missing:
it could always turn a GeoJSON footprint into an exterior shell, but nothing
resolved an address to one, so the geometry had to be supplied by hand.

The thing worth guarding is honesty about provenance. ODbL requires attribution
wherever the geometry is shown, and an agent deciding whether to trust a
footprint needs to know which source produced it — so no candidate may exist
without both.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from data_integrations import building_footprint as bf

# A ~10m x 8m rectangle near a plausible residential coordinate.
LAT, LON = 39.7392, -104.9903
_D_LAT = 8.0 / 111_132.0
_D_LON = 10.0 / (111_320.0 * math.cos(math.radians(LAT)))

RING = [
    {"lat": LAT, "lon": LON},
    {"lat": LAT, "lon": LON + _D_LON},
    {"lat": LAT + _D_LAT, "lon": LON + _D_LON},
    {"lat": LAT + _D_LAT, "lon": LON},
]


def _overpass_payload(tags=None, geometry=None):
    return {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "geometry": geometry if geometry is not None else RING,
                "tags": tags if tags is not None else {"building": "house"},
            }
        ]
    }


@pytest.fixture
def fake_http(monkeypatch):
    """Capture the outbound request and return a scripted payload."""
    calls = {"urls": [], "bodies": []}
    payloads = {}

    async def _fetch(url, *, data=None):
        calls["urls"].append(url)
        calls["bodies"].append(data.decode() if data else None)
        for fragment, payload in payloads.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        return {}

    monkeypatch.setattr(bf, "_fetch_json", _fetch)
    calls["payloads"] = payloads
    return calls


@pytest.fixture(autouse=True)
def _no_regrid_token(monkeypatch):
    monkeypatch.delenv("REGRID_API_TOKEN", raising=False)


# --- OpenStreetMap ------------------------------------------------------------

def test_osm_footprint_carries_licence_and_attribution(fake_http):
    """ODbL is not optional decoration — the credit must travel with the shape."""
    fake_http["payloads"]["overpass"] = _overpass_payload()

    (candidate,) = asyncio.run(bf.osm_footprints(LAT, LON))

    assert candidate.source == "openstreetmap"
    assert candidate.licence == "ODbL-1.0"
    assert "OpenStreetMap" in candidate.attribution


def test_osm_geometry_is_a_closed_geojson_polygon(fake_http):
    fake_http["payloads"]["overpass"] = _overpass_payload()

    (candidate,) = asyncio.run(bf.osm_footprints(LAT, LON))
    ring = candidate.geometry["coordinates"][0]

    assert candidate.geometry["type"] == "Polygon"
    assert ring[0] == ring[-1], "ring must be explicitly closed for GeoJSON"
    assert candidate.area_sqm == pytest.approx(80, abs=2)


def test_tiny_structures_are_dropped(fake_http):
    """Sheds, bin stores and bus shelters are tagged building=* too, and a 3 m²
    outline would become a phantom dwelling."""
    tiny = [
        {"lat": LAT, "lon": LON},
        {"lat": LAT, "lon": LON + _D_LON * 0.15},
        {"lat": LAT + _D_LAT * 0.15, "lon": LON + _D_LON * 0.15},
        {"lat": LAT + _D_LAT * 0.15, "lon": LON},
    ]
    fake_http["payloads"]["overpass"] = _overpass_payload(geometry=tiny)

    assert asyncio.run(bf.osm_footprints(LAT, LON)) == []


def test_levels_are_read_but_never_invented(fake_http):
    fake_http["payloads"]["overpass"] = _overpass_payload(
        tags={"building": "house", "building:levels": "2"}
    )
    (with_levels,) = asyncio.run(bf.osm_footprints(LAT, LON))
    assert with_levels.levels == 2

    fake_http["payloads"]["overpass"] = _overpass_payload(tags={"building": "house"})
    (without,) = asyncio.run(bf.osm_footprints(LAT, LON))
    assert without.levels is None, "a guessed storey count multiplies every rehab line"


@pytest.mark.parametrize("bad", ["many", "0", "-3", "999"])
def test_implausible_level_counts_are_ignored(fake_http, bad):
    fake_http["payloads"]["overpass"] = _overpass_payload(
        tags={"building": "house", "building:levels": bad}
    )
    (candidate,) = asyncio.run(bf.osm_footprints(LAT, LON))
    assert candidate.levels is None


def test_radius_is_clamped(fake_http):
    """A wide search starts returning the neighbours' houses."""
    fake_http["payloads"]["overpass"] = _overpass_payload()

    asyncio.run(bf.osm_footprints(LAT, LON, radius_m=99_999))

    assert f"around:{bf.MAX_RADIUS_M}" in fake_http["bodies"][0]


def test_upstream_failure_yields_no_candidates_rather_than_raising(fake_http):
    """One source being down must not take the whole lookup with it."""
    fake_http["payloads"]["overpass"] = RuntimeError("overpass 504")

    assert asyncio.run(bf.osm_footprints(LAT, LON)) == []


# --- Regrid -------------------------------------------------------------------

def test_regrid_is_skipped_without_a_token(fake_http):
    assert asyncio.run(bf.regrid_footprints("123 Main St")) == []
    assert fake_http["urls"] == []


def test_regrid_requests_matched_buildings(fake_http, monkeypatch):
    """The existing client hard-coded this to false, so building geometry was
    being asked for and thrown away."""
    monkeypatch.setenv("REGRID_API_TOKEN", "tok")
    fake_http["payloads"]["regrid.com"] = {"parcels": {"features": []}}

    asyncio.run(bf.regrid_footprints("123 Main St"))

    assert "return_matched_buildings=true" in fake_http["urls"][0]


def test_regrid_candidate_is_marked_licensed(fake_http, monkeypatch):
    monkeypatch.setenv("REGRID_API_TOKEN", "tok")
    ring = [[p["lon"], p["lat"]] for p in RING]
    ring.append(ring[0])
    fake_http["payloads"]["regrid.com"] = {
        "parcels": {
            "features": [
                {
                    "properties": {
                        "matched_buildings": [
                            {"geometry": {"type": "Polygon", "coordinates": [ring]}}
                        ]
                    }
                }
            ]
        }
    }

    (candidate,) = asyncio.run(bf.regrid_footprints("123 Main St"))

    assert candidate.source == "regrid"
    assert "licensed" in candidate.licence.lower()


# --- normalisation ------------------------------------------------------------

def test_multipolygon_keeps_only_the_largest_ring():
    """A detached garage in the same record would otherwise become extra rooms."""
    big = [[LON, LAT], [LON + _D_LON, LAT], [LON + _D_LON, LAT + _D_LAT], [LON, LAT + _D_LAT], [LON, LAT]]
    small = [
        [LON, LAT], [LON + _D_LON * 0.2, LAT],
        [LON + _D_LON * 0.2, LAT + _D_LAT * 0.2], [LON, LAT + _D_LAT * 0.2], [LON, LAT],
    ]

    polygon = bf._as_polygon({"type": "MultiPolygon", "coordinates": [[small], [big]]})

    assert polygon["coordinates"][0] == big


def test_unsupported_geometry_is_rejected():
    assert bf._as_polygon({"type": "Point", "coordinates": [LON, LAT]}) is None
    assert bf._as_polygon({}) is None


# --- resolver -----------------------------------------------------------------

def test_resolver_returns_empty_rather_than_guessing(fake_http):
    """No footprint is a real answer. Inventing one would put fabricated square
    footage into a rehab estimate."""
    fake_http["payloads"]["overpass"] = {"elements": []}

    assert asyncio.run(bf.resolve_footprints(address="", lat=LAT, lon=LON)) == []


def test_resolver_prefers_licensed_address_matched_data(fake_http, monkeypatch):
    """Regrid matches the address; OSM only matches proximity."""
    monkeypatch.setenv("REGRID_API_TOKEN", "tok")
    ring = [[p["lon"], p["lat"]] for p in RING]
    ring.append(ring[0])
    fake_http["payloads"]["regrid.com"] = {
        "parcels": {"features": [{"properties": {"matched_buildings": [
            {"geometry": {"type": "Polygon", "coordinates": [ring]}}]}}]}
    }
    fake_http["payloads"]["overpass"] = _overpass_payload()

    candidates = asyncio.run(bf.resolve_footprints(address="123 Main St", lat=LAT, lon=LON))

    assert [c.source for c in candidates][0] == "regrid"
    assert "openstreetmap" in [c.source for c in candidates]


def test_every_candidate_has_provenance(fake_http, monkeypatch):
    monkeypatch.setenv("REGRID_API_TOKEN", "tok")
    fake_http["payloads"]["overpass"] = _overpass_payload()
    fake_http["payloads"]["regrid.com"] = {"parcels": {"features": []}}

    for candidate in asyncio.run(bf.resolve_footprints(address="x", lat=LAT, lon=LON)):
        assert candidate.source and candidate.licence and candidate.attribution
        assert json.dumps(candidate.to_dict())  # serialises for the API response


# --- the point of all this ----------------------------------------------------

def test_a_resolved_footprint_converts_to_an_exterior_shell(fake_http):
    """End to end: the resolver's output is exactly what the existing pipeline
    consumes, and that pipeline still refuses to invent interior walls."""
    from floorplan_pipeline import extract_from_parcel_geometry

    fake_http["payloads"]["overpass"] = _overpass_payload()
    (candidate,) = asyncio.run(bf.osm_footprints(LAT, LON))

    document = extract_from_parcel_geometry(candidate.geometry, wall_height_m=2.5)

    assert document.walls, "exterior walls should be derived from the outline"
    # Machine output must announce itself — the DB CHECK enforces this too.
    assert document.provenance.ai_generated is True
    assert document.provenance.source == "parcel_vector"
    # A footprint contains no interior information; anything else is fabrication.
    assert all(wall.interior is False for wall in document.walls)
