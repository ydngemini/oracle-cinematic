"""A tour is the union of what a property has, not the maximum of it.

The resolver used to reduce everything to a single `best_tier`, and the viewer
rendered only that winner. Two consequences, both of which lost real work:

  * A property holding a splat AND 360s AND photos showed the splat and
    silently dropped the rest — captures an agent paid to take, invisible.
  * A property holding photos but no splat opened *nothing*, because the viewer
    returned early when there was no splat_url.

There is also an honesty bug inside the same design. `is_this_property` was one
flag for the whole tour, computed from the splat, so a demo splat sitting beside
genuine 360s of the house marked the entire tour "not this property" — and the
real captures were suppressed to avoid the contradiction.

These tests pin the replacement: every asset is returned, each carrying its own
provenance, and a label describes the asset rather than the tour.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import tour_api
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
LEAD_ID = "22222222-2222-4222-8222-222222222222"


def _media(kind, *, provenance="captured", url=None,
           media_id="33333333-3333-4333-8333-333333333333"):
    return {"id": media_id, "kind": kind, "url": url or f"/api/media/{media_id}",
            "sort_order": 0, "provenance": provenance}


def _scene(scene_id, *, floor=0, order=0, label="", position=None, neighbours=None,
           provenance="captured"):
    return {
        "id": scene_id, "media_id": f"m-{scene_id}", "url": f"/api/media/m-{scene_id}",
        "provenance": provenance, "floor_index": floor, "label": label,
        "sort_order": order,
        "position_x": position[0] if position else None,
        "position_y": position[1] if position else None,
        "position_z": position[2] if position else None,
        "heading_deg": None, "neighbour_ids": neighbours or [],
    }


def _patch_db(monkeypatch, rows, plan=None, scenes=None):
    class _Conn:
        async def fetch(self, query, *_a, **_k):
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
    return asyncio.run(
        tour_api.resolve_tour(lead_id=UUID(LEAD_ID), listing_id=None, ctx=CTX)
    )


def _kinds(result) -> set[str]:
    return {a["kind"] for a in result["assets"]}


def _of(result, kind) -> list[dict]:
    return [a for a in result["assets"] if a["kind"] == kind]


def test_a_property_with_everything_exposes_everything(monkeypatch):
    """The headline case. Under the tier ladder this returned best_tier 3 and
    the viewer showed the splat alone; the 360s and photos were unreachable."""
    _patch_db(
        monkeypatch,
        [
            _media("photo", media_id="44444444-4444-4444-8444-444444444444"),
            _media("splat", provenance="captured"),
        ],
        scenes=[_scene("s1"), _scene("s2")],
    )

    result = _resolve()

    assert {"splat", "pano", "photo", "exterior"} <= _kinds(result)
    assert _of(result, "splat")[0]["url"]
    assert _of(result, "pano")[0]["count"] == 2
    assert _of(result, "photo")[0]["count"] == 1


def test_photos_alone_are_still_a_tour(monkeypatch):
    """No splat is not the same as no tour. The viewer's `if (!splatUrl) return
    null` made this property open nothing at all."""
    _patch_db(monkeypatch, [_media("photo")])

    result = _resolve()

    assert "photo" in _kinds(result)
    assert _of(result, "photo")[0]["count"] == 1
    assert "exterior" in _kinds(result), "the address always supports an exterior"


def test_the_exterior_is_always_offered(monkeypatch):
    """A property with no media at all still has an address."""
    _patch_db(monkeypatch, [])

    result = _resolve()

    assert _kinds(result) == {"exterior"}
    assert _of(result, "exterior")[0]["is_this_property"] is True


def test_a_demo_splat_never_taints_the_real_captures_beside_it(monkeypatch):
    """The honesty fix, and the reason per-asset provenance is not cosmetic.

    Tour-wide, `is_this_property` is False here — the splat is generated. But
    the 360s and photos genuinely depict the home, and must not inherit the
    splat's standing."""
    _patch_db(
        monkeypatch,
        [
            _media("photo", media_id="44444444-4444-4444-8444-444444444444"),
            _media("splat", provenance="synthetic"),
        ],
        scenes=[_scene("s1"), _scene("s2")],
    )

    result = _resolve()

    splat = _of(result, "splat")[0]
    assert splat["is_this_property"] is False
    assert "not this home" in splat["label"].lower()

    assert _of(result, "pano")[0]["is_this_property"] is True
    assert _of(result, "photo")[0]["is_this_property"] is True
    # And it is still openable — the demo space is the only GPU-free way to
    # exercise the viewer's controls and bounds clamp.
    assert splat["url"]


def test_no_synthetic_asset_ever_claims_to_be_the_property(monkeypatch):
    """The invariant, stated over the whole asset list rather than one field."""
    _patch_db(
        monkeypatch,
        [_media("splat", provenance="synthetic")],
        scenes=[_scene("s1", provenance="synthetic")],
    )

    result = _resolve()

    for asset in result["assets"]:
        if asset.get("provenance") in ("synthetic", "mixed"):
            assert asset["is_this_property"] is False, asset["kind"]


def test_a_generated_splat_carries_its_disclosure_on_the_asset(monkeypatch):
    _patch_db(monkeypatch, [_media("splat", provenance="synthetic")])

    splat = _of(_resolve(), "splat")[0]

    assert splat["disclosure"], "a reconstruction must disclose that it is one"
    assert "actual home" not in splat["note"]


def test_one_360_is_offered_but_not_described_as_a_walkthrough(monkeypatch):
    """It still belongs in the tour — it just is not somewhere you can move
    between rooms, and the label must not promise that."""
    _patch_db(monkeypatch, [], scenes=[_scene("s1")])

    pano = _of(_resolve(), "pano")[0]

    assert pano["count"] == 1
    assert pano["walkable"] is False
    assert "walkthrough" not in pano["label"].lower()


def test_mixed_provenance_panos_do_not_claim_the_whole_set_is_real(monkeypatch):
    _patch_db(
        monkeypatch,
        [],
        scenes=[_scene("s1"), _scene("s2", provenance="synthetic")],
    )

    pano = _of(_resolve(), "pano")[0]

    assert pano["is_this_property"] is False
    assert pano["provenance"] == "mixed"
    # Per-scene truth survives the summary.
    assert [sc["is_this_property"] for sc in pano["scenes"]] == [True, False]
