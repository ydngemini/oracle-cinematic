"""The guided route: an ordered walk over the vantage points that exist.

The scene graph is free roam — a visitor can go anywhere, which is the right
default and a poor first impression. SPHR's runtime (MIT, lukehollis/sphr)
models the guided version as an ordered list of *tourpoints* over the same
spaces, and that separation is the idea adopted here.

Two properties are defended above all: the route never invents a stop, and it
never invents a name. A tour that announces a room nobody photographed, or calls
one "Room 3" because the reconstruction had no OCR pass, is the confident
fiction the rest of this pipeline exists to refuse.
"""

from __future__ import annotations

import tour_api

# ---------------------------------------------------------------------------
# Guided route — SPHR's tourpoint idea over the scene graph we already have
# ---------------------------------------------------------------------------

def _scene(scene_id, floor=0, label="", captured=True):
    return {
        "scene_id": scene_id, "media_id": scene_id, "url": f"/{scene_id}.jpg",
        "floor_index": floor, "label": label,
        "position": {"x": 0, "y": 0, "z": 0}, "heading_deg": 0,
        "neighbours": [], "provenance": "captured" if captured else "ai_generated",
        "is_this_property": captured,
    }


def test_a_single_vantage_point_is_a_view_not_a_tour():
    """The same rule the pano tier uses. One 360 is somewhere you can look, not
    somewhere you can be walked through."""
    assert tour_api._tourpoints([_scene("a")], None, []) == []


def test_the_route_references_scenes_rather_than_copying_them():
    """A route is a VIEW of the graph. Copy the positions into it and the two
    drift out of step the first time a scene moves."""
    scenes = [_scene("a"), _scene("b"), _scene("c")]

    points = tour_api._tourpoints(scenes, None, [])

    assert [p["scene_id"] for p in points] == ["a", "b", "c"]
    for point in points:
        assert "position" not in point and "url" not in point


def test_the_route_never_invents_a_stop():
    """It can only walk somewhere the capture actually produced."""
    scenes = [_scene("a"), _scene("b")]

    points = tour_api._tourpoints(scenes, None, [{"index": 0, "name": "Ground"}])

    assert len(points) == len(scenes)
    assert {p["scene_id"] for p in points} <= {"a", "b"}


def test_stops_are_ordered_by_floor_then_by_capture_order():
    """Capture order is the order the photographer walked, which is a better
    route than anything derivable from the positions — they were there."""
    scenes = [_scene("up", floor=1), _scene("a", floor=0), _scene("b", floor=0)]

    points = tour_api._tourpoints(scenes, None, [])

    assert [p["scene_id"] for p in points] == ["a", "b", "up"]
    assert [p["index"] for p in points] == [0, 1, 2]


def test_placeholder_room_names_are_not_announced():
    """The reconstruction path names every room 'Room 1', 'Room 2' because it
    has no OCR pass. A tour that announces 'Room 3' is worse than one that says
    nothing."""
    document = {"rooms": [{"name": "Room 1"}, {"name": "Room 2"}]}

    points = tour_api._tourpoints([_scene("a"), _scene("b")], document, [])

    assert not any(p["label"].lower().startswith("room ") for p in points)


def test_real_room_names_are_used_when_they_match_the_stops():
    document = {"rooms": [{"name": "Kitchen"}, {"name": "Living Room"}]}

    points = tour_api._tourpoints([_scene("a"), _scene("b")], document, [])

    assert [p["label"] for p in points] == ["Kitchen", "Living Room"]


def test_a_partial_name_match_labels_nothing():
    """Labelling some stops and not others reads as missing data rather than as
    a deliberate absence."""
    document = {"rooms": [{"name": "Kitchen"}]}

    points = tour_api._tourpoints([_scene("a"), _scene("b")], document, [])

    assert "Kitchen" not in [p["label"] for p in points]


def test_narration_is_left_for_a_human_to_write():
    """A sentence invented about a room the model has never seen is the one
    thing a property tour must not do."""
    points = tour_api._tourpoints([_scene("a"), _scene("b")], None, [])

    assert all(p["narration"] == "" for p in points)
