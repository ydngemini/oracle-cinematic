"""The complete-dimension engine: inside and outside, nothing left blank.

The contract under test is double-sided. Every field must be filled — even with
zero input data — AND every filled value must say where it came from
(measured/sourced/estimated/default), because an unlabelled guess inside a rehab
estimate is indistinguishable from a measurement.
"""

from __future__ import annotations

import math

import pytest

from floorplan_pipeline.dimensions import (
    DEFAULT_FOOTPRINT_AREA_M2,
    MODEL_VERSION,
    complete_dimensions,
)
from tour_api import _floors_from_plan

# A ~12m × 10m rectangle as GeoJSON (tiny degree offsets ≈ metres at equator).
def _rect_geometry(width_m=12.0, depth_m=10.0):
    half_lon = (width_m / 2) / 111_320.0
    half_lat = (depth_m / 2) / 111_132.0
    ring = [
        [-half_lon, -half_lat], [half_lon, -half_lat],
        [half_lon, half_lat], [-half_lon, half_lat],
        [-half_lon, -half_lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


# An L-shape: a 12×10 with a 6×5 bite out of one corner (fill ratio ~0.75).
def _l_geometry():
    def lon(m): return m / 111_320.0
    def lat(m): return m / 111_132.0
    ring = [
        [lon(0), lat(0)], [lon(12), lat(0)], [lon(12), lat(5)],
        [lon(6), lat(5)], [lon(6), lat(10)], [lon(0), lat(10)],
        [lon(0), lat(0)],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


ALL_FIELDS = {
    "footprint_area_m2", "footprint_perimeter_m", "levels", "storey_height_m",
    "wall_height_m", "total_height_m", "exterior_wall_thickness_m",
    "interior_wall_thickness_m", "bedrooms", "bathrooms", "doors", "windows",
    "total_floor_area_m2", "total_floor_area_sqft",
}


# --- the core contract --------------------------------------------------------

def test_zero_input_still_fills_every_field():
    """The extreme case: no footprint, no area, no levels, no anything."""
    document, manifest = complete_dimensions()
    payload = manifest.to_json()

    assert set(payload) >= ALL_FIELDS
    assert all(entry["value"] is not None for entry in payload.values())
    assert all(entry["basis"] for entry in payload.values())


def test_every_value_carries_a_provenance_label():
    _, manifest = complete_dimensions(footprint_geometry=_rect_geometry())

    for name, entry in manifest.to_json().items():
        assert entry["provenance"] in ("measured", "sourced", "estimated", "default"), name


def test_zero_input_is_labelled_default_not_measured():
    """Fabricating without saying so is the one forbidden outcome."""
    _, manifest = complete_dimensions()
    payload = manifest.to_json()

    assert payload["footprint_area_m2"]["provenance"] == "default"
    assert math.isclose(
        payload["footprint_area_m2"]["value"], DEFAULT_FOOTPRINT_AREA_M2, rel_tol=0.05
    )
    # And the manifest points review effort at ALL of it.
    assert set(manifest.estimated_fields()) == ALL_FIELDS


def test_real_footprint_is_labelled_measured():
    _, manifest = complete_dimensions(
        footprint_geometry=_rect_geometry(), footprint_source="regrid"
    )
    payload = manifest.to_json()

    assert payload["footprint_area_m2"]["provenance"] == "measured"
    assert math.isclose(payload["footprint_area_m2"]["value"], 120.0, rel_tol=0.02)
    assert "regrid" in payload["footprint_area_m2"]["basis"]
    # Measured fields are NOT in the review list.
    assert "footprint_area_m2" not in manifest.estimated_fields()


def test_sourced_levels_win_over_the_estimate():
    _, one = complete_dimensions(footprint_geometry=_rect_geometry())
    _, three = complete_dimensions(
        footprint_geometry=_rect_geometry(), sourced_levels=3
    )

    assert one.to_json()["levels"] == {
        **one.to_json()["levels"],
        "value": 1, "provenance": "estimated",
    }
    assert three.to_json()["levels"]["value"] == 3
    assert three.to_json()["levels"]["provenance"] == "sourced"


def test_area_only_yields_an_estimated_square_plate():
    """Assessor gave an area but no outline — usable, but never 'measured'."""
    document, manifest = complete_dimensions(sourced_area_m2=200.0)
    payload = manifest.to_json()

    assert payload["footprint_area_m2"]["provenance"] == "estimated"
    assert math.isclose(payload["footprint_area_m2"]["value"], 200.0, rel_tol=0.05)
    assert document.walls, "a plate still produces a buildable shell"


# --- the constructed document -------------------------------------------------

def test_document_builds_outside_and_inside():
    document, _ = complete_dimensions(footprint_geometry=_rect_geometry())

    exterior = [w for w in document.walls if not w.interior]
    interior = [w for w in document.walls if w.interior]
    assert len(exterior) >= 4
    assert interior, "rectangular plate should be scaffolded with interior walls"
    assert document.rooms, "rooms must exist"
    assert document.openings, "doors and windows must exist"
    types = {room.type for room in document.rooms}
    assert {"living", "kitchen", "bedroom", "bathroom"} <= types


def test_document_announces_itself_as_machine_output():
    document, _ = complete_dimensions(footprint_geometry=_rect_geometry())

    assert document.provenance.ai_generated is True
    assert document.provenance.model_version == MODEL_VERSION
    assert "estimate" in (document.provenance.notes or "").lower()


def test_irregular_plate_refuses_the_grid_scaffold():
    """A plausible-looking grid on an L-shape reads as truth — the honest move
    is one room over the shell, with the programme reported in the manifest."""
    document, manifest = complete_dimensions(footprint_geometry=_l_geometry())

    assert len(document.rooms) == 1
    assert document.rooms[0].type == "other"
    assert "partition" in document.rooms[0].name.lower()
    # The bed/bath programme is still resolved — nothing blank.
    assert manifest.to_json()["bedrooms"]["value"] >= 1


def test_multi_level_document_carries_every_level():
    document, _ = complete_dimensions(
        footprint_geometry=_rect_geometry(), sourced_levels=3
    )

    assert len(document.levels) == 3
    assert [level.index for level in document.levels] == [0, 1, 2]


def test_bedrooms_scale_with_floor_area():
    _, small = complete_dimensions(footprint_geometry=_rect_geometry(8, 7))
    _, big = complete_dimensions(
        footprint_geometry=_rect_geometry(14, 12), sourced_levels=2
    )

    assert big.to_json()["bedrooms"]["value"] > small.to_json()["bedrooms"]["value"]


def test_windows_scale_with_perimeter_and_never_hit_zero():
    _, manifest = complete_dimensions(footprint_geometry=_rect_geometry(6, 5))

    assert manifest.to_json()["windows"]["value"] >= 2


def test_document_serialises_for_the_api():
    import json

    document, manifest = complete_dimensions(footprint_geometry=_rect_geometry())

    assert json.dumps(document.to_json())
    assert json.dumps(manifest.to_json())


# --- tour floors from the saved plan ------------------------------------------

def test_floors_derive_from_plan_levels():
    document, _ = complete_dimensions(
        footprint_geometry=_rect_geometry(), sourced_levels=2, wall_height_m=3.0
    )

    floors = _floors_from_plan(document.to_json())

    assert [f["index"] for f in floors] == [0, 1]
    assert floors[0]["y"] == 0.0
    assert floors[1]["y"] == 3.0  # index × the plan's own storey height
    assert all(f["id"] and f["name"] for f in floors)


def test_no_plan_means_no_floors_not_invented_ones():
    assert _floors_from_plan(None) == []
    assert _floors_from_plan({}) == []
    assert _floors_from_plan("not json{") == []


def test_floors_accept_the_jsonb_string_form():
    import json

    document, _ = complete_dimensions(footprint_geometry=_rect_geometry())
    floors = _floors_from_plan(json.dumps(document.to_json()))

    assert len(floors) == 1
