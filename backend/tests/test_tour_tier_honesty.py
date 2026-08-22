"""A generated splat must never be described as the actual home.

RECONSTRUCTION_PROVIDER defaults to `stub`, which synthesises a 4x2.6x4m
checkerboard box. That output went through the ordinary pipeline and landed as
a plain property_media(kind='splat') row, so the resolver counted it as tier 3
and returned:

    "Free-roam the actual home in a photoreal 3D reconstruction."

On a default deployment that was false for every property in the system. These
tests are the guard on that sentence.

The demo splat stays viewable on purpose — it is the only way to exercise the
viewer, its controls and its bounds clamp without a GPU. What it must not do is
set the tier or claim to be the property.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import tour_api
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
LEAD_ID = "22222222-2222-4222-8222-222222222222"


def _media(kind, *, provenance="captured", url=None, media_id="33333333-3333-4333-8333-333333333333"):
    return {
        "id": media_id,
        "kind": kind,
        "url": url or f"/api/media/{media_id}",
        "sort_order": 0,
        "provenance": provenance,
    }


def _scene(scene_id, *, floor=0, order=0, label="", position=None, neighbours=None):
    return {
        "id": scene_id,
        "media_id": f"m-{scene_id}",
        "url": f"/api/media/m-{scene_id}",
        "floor_index": floor,
        "label": label,
        "sort_order": order,
        "position_x": position[0] if position else None,
        "position_y": position[1] if position else None,
        "position_z": position[2] if position else None,
        "heading_deg": None,
        "neighbour_ids": neighbours or [],
    }


def _patch_db(monkeypatch, rows, plan=None, scenes=None):
    class _Conn:
        async def fetch(self, query, *_a, **_k):
            # The resolver reads media and pano scenes on the same connection.
            if "property_pano_scenes" in query:
                return scenes or []
            return rows

        async def fetchrow(self, *_a, **_k):
            return {"document": plan} if plan is not None else None

    @asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(tour_api, "tenant_tx", _tx)


def _resolve():
    from uuid import UUID
    return asyncio.run(tour_api.resolve_tour(lead_id=UUID(LEAD_ID), listing_id=None, ctx=CTX))


# ---------------------------------------------------------------------------
# The core assertion
# ---------------------------------------------------------------------------

def test_synthetic_splat_does_not_claim_to_be_the_actual_home(monkeypatch):
    _patch_db(monkeypatch, [_media("splat", provenance="synthetic")])

    result = _resolve()

    assert result["best_tier"] != 3, "a generated room is not a tour of this property"
    assert result["is_this_property"] is False
    assert "actual home" not in result["honest_note"]
    assert "not this home" in result["badge"].lower()
    assert result["tiers"]["splat"] is False, "the tier flag tracks captured media only"
    # Still openable — it is a genuinely useful viewer smoke test.
    assert result["splat_url"], "the demo space should remain viewable"
    assert result["disclosure"], "and must carry a disclosure when it is shown"


def test_captured_splat_is_tier_three_and_says_so(monkeypatch):
    _patch_db(monkeypatch, [_media("splat", provenance="captured")])

    result = _resolve()

    assert result["best_tier"] == 3
    assert result["is_this_property"] is True
    assert result["badge"] == "Full 3D Walkthrough"
    assert "actual home" in result["honest_note"]
    assert result["tiers"]["splat"] is True
    assert result["walkable_interior"] is True


def test_a_synthetic_splat_never_outranks_real_photos(monkeypatch):
    """The tier must reflect the real media, not the demo asset sitting beside it."""
    _patch_db(monkeypatch, [
        _media("photo", media_id="44444444-4444-4444-8444-444444444444"),
        _media("splat", provenance="synthetic"),
    ])

    result = _resolve()

    assert result["best_tier"] == 1, "photos are what this property actually has"
    assert result["photo_count"] == 1
    assert result["is_this_property"] is False
    assert result["tiers"]["photos"] is True
    assert result["tiers"]["splat"] is False


def test_a_real_capture_wins_over_a_leftover_demo(monkeypatch):
    """Once a real capture lands, the demo must stop being what gets shown."""
    real_url = "/api/media/55555555-5555-4555-8555-555555555555"
    _patch_db(monkeypatch, [
        _media("splat", provenance="synthetic", url="/api/media/demo"),
        _media("splat", provenance="captured", url=real_url,
               media_id="55555555-5555-4555-8555-555555555555"),
    ])

    result = _resolve()

    assert result["best_tier"] == 3
    assert result["is_this_property"] is True
    assert result["splat_url"] == real_url


def test_legacy_rows_without_provenance_are_treated_as_captured(monkeypatch):
    """Pre-0071 rows came from a configured real provider; COALESCE covers them."""
    row = _media("splat")
    row.pop("provenance")
    row["provenance"] = "captured"  # what COALESCE(provenance,'captured') yields
    _patch_db(monkeypatch, [row])

    result = _resolve()

    assert result["best_tier"] == 3
    assert result["is_this_property"] is True


# ---------------------------------------------------------------------------
# Existing behaviour that must not regress
# ---------------------------------------------------------------------------

def test_no_media_degrades_to_exterior_without_erroring(monkeypatch):
    _patch_db(monkeypatch, [])

    result = _resolve()

    assert result["best_tier"] == 0
    assert result["walkable_interior"] is False
    assert result["splat_url"] is None
    assert result["disclosure"] is None
    assert result["is_this_property"] is True, "nothing is being shown, so nothing is misdescribed"


def test_resolver_still_refuses_a_request_with_no_subject():
    with pytest.raises(tour_api.HTTPException) as excinfo:
        asyncio.run(tour_api.resolve_tour(lead_id=None, listing_id=None, ctx=CTX))
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------------------
# The chain that made all of the above invisible
# ---------------------------------------------------------------------------

def test_public_records_carry_the_tenants_lead_id():
    """`lead_id: None` here strands every tier above exterior.

    HouseSelection spreads this row verbatim into HouseWorkspace, which passes
    lead_id to useTour, which early-returns when it is null — so the resolver is
    never called and no tier badge, filmstrip or "step inside" ever appears,
    regardless of what has actually been captured.
    """
    import mls_portal

    rows = [
        {"id": "r1", "parcel_id": "P-1", "state": "DE", "address": "1 Main St"},
        {"id": "r2", "parcel_id": "P-2", "state": "DE", "address": "2 Main St"},
    ]
    lead_ids = {("P-1", "DE"): LEAD_ID}

    first = mls_portal._public_record_json(rows[0], lead_ids)
    second = mls_portal._public_record_json(rows[1], lead_ids)

    assert first["lead_id"] == LEAD_ID
    # No lead for this parcel is a real answer: nothing has been captured
    # against it, so exterior tier is correct.
    assert second["lead_id"] is None


def test_lead_lookup_is_one_query_for_the_whole_page():
    """Per-row lookups would add a query per card on every search."""
    import mls_portal

    calls = []

    class _Conn:
        async def fetch(self, query, *args):
            calls.append((query, args))
            return [{"parcel_id": "P-1", "state": "DE", "id": LEAD_ID}]

    rows = [
        {"parcel_id": f"P-{i}", "state": "DE"} for i in range(20)
    ]
    result = asyncio.run(mls_portal._lead_ids_for_records(_Conn(), rows))

    assert len(calls) == 1, f"expected a single batched query, made {len(calls)}"
    assert result == {("P-1", "DE"): LEAD_ID}


def test_lead_lookup_skips_the_query_when_no_row_has_a_parcel():
    import mls_portal

    class _Conn:
        async def fetch(self, *_a, **_k):
            raise AssertionError("should not query when there is nothing to match")

    assert asyncio.run(mls_portal._lead_ids_for_records(_Conn(), [{"address": "x"}])) == {}


# ---------------------------------------------------------------------------
# Tier 2 — a rung that was in the ladder but structurally unreachable
# ---------------------------------------------------------------------------

def test_a_single_360_is_a_view_not_a_walkthrough(monkeypatch):
    """One vantage point cannot support "walk room-to-room"."""
    _patch_db(
        monkeypatch,
        [_media("pano", media_id="p1")],
        scenes=[_scene("s1", label="Living room")],
    )

    result = _resolve()

    assert result["best_tier"] == 1, "a lone 360 shows the room but is not a walkthrough"
    assert result["walkable_interior"] is False
    assert result["pano_scene_count"] == 1
    assert "room-to-room" not in result["honest_note"]


def test_two_linked_scenes_are_tier_two(monkeypatch):
    _patch_db(
        monkeypatch,
        [_media("pano", media_id="p1"), _media("pano", media_id="p2")],
        scenes=[_scene("s1", order=0, label="Hall"), _scene("s2", order=1, label="Kitchen")],
    )

    result = _resolve()

    assert result["best_tier"] == 2
    assert result["walkable_interior"] is True
    assert result["badge"] == "360° Walkthrough"
    assert "actual home" in result["honest_note"]
    assert result["pano_scene_count"] == 2
    assert result["disclosure"], "a 360 tour still carries its capture disclosure"


def test_scenes_fall_back_to_capture_order_when_no_links_recorded(monkeypatch):
    """An ordered upload describes a route even without explicit adjacency."""
    _patch_db(
        monkeypatch, [],
        scenes=[_scene("s1", order=0), _scene("s2", order=1), _scene("s3", order=2)],
    )

    scenes = _resolve()["pano_scenes"]

    assert [s["scene_id"] for s in scenes] == ["s1", "s2", "s3"]
    assert scenes[0]["neighbours"] == ["s2"]
    assert scenes[1]["neighbours"] == ["s1", "s3"]
    assert scenes[2]["neighbours"] == ["s2"]


def test_recorded_links_win_over_the_sequential_fallback(monkeypatch):
    _patch_db(
        monkeypatch, [],
        scenes=[
            _scene("s1", order=0, neighbours=["s3"]),
            _scene("s2", order=1),
            _scene("s3", order=2),
        ],
    )

    scenes = {s["scene_id"]: s for s in _resolve()["pano_scenes"]}

    assert scenes["s1"]["neighbours"] == ["s3"], "an explicit link must not be overwritten"
    assert scenes["s2"]["neighbours"] == ["s1", "s3"], "and the others still get the fallback"


def test_links_to_deleted_scenes_are_dropped(monkeypatch):
    """property_media deletes cascade to scenes, leaving dangling neighbour ids."""
    _patch_db(monkeypatch, [], scenes=[_scene("s1", neighbours=["gone", "s2"]), _scene("s2", order=1)])

    scenes = {s["scene_id"]: s for s in _resolve()["pano_scenes"]}

    assert scenes["s1"]["neighbours"] == ["s2"]


def test_unsurveyed_scenes_report_no_position_rather_than_a_guess(monkeypatch):
    _patch_db(
        monkeypatch, [],
        scenes=[_scene("s1"), _scene("s2", order=1, position=(1.0, 0.0, 2.0))],
    )

    scenes = {s["scene_id"]: s for s in _resolve()["pano_scenes"]}

    assert scenes["s1"]["position"] is None, "no coordinates recorded means none reported"
    assert scenes["s2"]["position"] == {"x": 1.0, "y": 0.0, "z": 2.0}


def test_a_captured_splat_still_outranks_panos(monkeypatch):
    _patch_db(
        monkeypatch,
        [_media("splat", provenance="captured")],
        scenes=[_scene("s1"), _scene("s2", order=1)],
    )

    result = _resolve()

    assert result["best_tier"] == 3
    assert result["pano_scene_count"] == 2, "the 360s are still offered alongside"
