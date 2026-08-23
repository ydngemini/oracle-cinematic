"""The agent can see and request spatial work — and cannot perform it.

Before this, no tour, capture, reconstruction or video tool existed on the agent
surface at all. "Is there a 3D tour of 12 Oak St?" was unanswerable, so any
answer the model gave was invented.

Both halves are pinned here. The read tool returns *every* asset a property has,
each with its own provenance, so the agent can say "the 360s are of this house,
the 3D model is a demo" rather than flattening both into one claim. The request
tools stage an approval and stop: a pod reconstruction rents a GPU and a video
bills a generation provider, and an agent that could loop on either would spend
without a ceiling.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

import ai_tools_gated as gated
import ai_tools_read as read_tools
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
LEAD_ID = "22222222-2222-4222-8222-222222222222"


class _Conn:
    """Answers the anchor-existence probe and the three tour reads."""

    def __init__(self, *, exists=True, media=None, scenes=None):
        self._exists = exists
        self._media = media or []
        self._scenes = scenes or []

    async def fetchval(self, query, *_a):
        return 1 if self._exists else None

    async def fetch(self, query, *_a, **_k):
        return self._scenes if "property_pano_scenes" in query else self._media

    async def fetchrow(self, *_a, **_k):
        return None


def _media_row(kind, *, provenance="captured", media_id="33333333-3333-4333-8333-333333333333"):
    return {"id": media_id, "kind": kind, "url": f"/api/media/{media_id}",
            "sort_order": 0, "provenance": provenance}


def _scene_row(scene_id, *, provenance="captured"):
    return {"id": scene_id, "media_id": f"m-{scene_id}", "url": f"/api/media/m-{scene_id}",
            "provenance": provenance, "floor_index": 0, "label": "", "sort_order": 0,
            "position_x": None, "position_y": None, "position_z": None,
            "heading_deg": None, "neighbour_ids": []}


def _read(conn, **tool_input):
    return asyncio.run(read_tools.execute(conn, CTX, "get_property_tour", tool_input))


def _request(tool, conn, **tool_input):
    return asyncio.run(gated.execute(
        conn, CTX, tool, tool_input, user_id="u1", message_id="m1",
        context_type="lead", context_id=LEAD_ID,
    ))


# ---------------------------------------------------------------------------
# Seeing
# ---------------------------------------------------------------------------

def test_the_agent_sees_every_asset_not_just_the_best_one():
    conn = _Conn(
        media=[_media_row("photo"), _media_row("splat", media_id="44444444-4444-4444-8444-444444444444")],
        scenes=[_scene_row("s1"), _scene_row("s2")],
    )

    result = _read(conn, lead_id=LEAD_ID)

    kinds = {a["kind"] for a in result["assets"]}
    assert {"splat", "pano", "photo", "exterior"} <= kinds


def test_the_agent_is_told_which_assets_are_not_the_property():
    """So it can say "the 360s are of this house, the 3D model is a demo"
    instead of describing a generated room as the home."""
    conn = _Conn(
        media=[_media_row("splat", provenance="synthetic")],
        scenes=[_scene_row("s1"), _scene_row("s2")],
    )

    result = _read(conn, lead_id=LEAD_ID)
    by_kind = {a["kind"]: a for a in result["assets"]}

    assert by_kind["splat"]["shows_this_property"] is False
    assert by_kind["pano"]["shows_this_property"] is True


def test_a_missing_capture_reports_the_providers_own_reason(monkeypatch):
    """"RunPod balance is $-0.05 — add credits" beats "unavailable", which sends
    someone hunting for an outage that is not there."""
    import reconstruction_providers as rp

    class _Broke:
        name = "runpod_pod"

        def available(self):
            return (False, "RunPod balance is $-0.05, below the $1.00 minimum — add credits")

    monkeypatch.setattr(rp, "get_provider", lambda: _Broke())

    result = _read(_Conn(media=[_media_row("photo")]), lead_id=LEAD_ID)

    assert "add credits" in result["interior_capture_unavailable_because"]


def test_a_request_without_a_subject_is_refused():
    assert "error" in _read(_Conn())


# ---------------------------------------------------------------------------
# Requesting — and never doing
# ---------------------------------------------------------------------------

def test_a_reconstruction_is_requested_and_no_gpu_is_rented(monkeypatch):
    import ai_tools_gated
    import reconstruction_providers as rp

    class _Ready:
        name = "runpod_pod"
        produces = "captured"

        def available(self):
            return (True, "")

    created: list = []

    async def _create_approval(ctx, **kwargs):
        created.append(kwargs)
        return {"id": "ap-1"}

    monkeypatch.setattr(rp, "get_provider", lambda: _Ready())
    monkeypatch.setattr("approval_service.create_approval", _create_approval)

    result = _request("request_property_reconstruction", _Conn(), lead_id=LEAD_ID)

    assert result["ok"] is True
    assert result["started"] is False, "the tool must not start the job"
    assert result["approval_id"] == "ap-1"
    assert created[0]["target_id"] == LEAD_ID
    # The claim the resulting media will be allowed to make is fixed at request
    # time, so a later provider swap cannot quietly upgrade it.
    assert created[0]["draft_payload"]["provenance"] == "captured"


def test_nothing_is_staged_when_the_job_could_not_run(monkeypatch):
    """An approval for a job that cannot run spends a human's attention on a
    decision with no effect."""
    import reconstruction_providers as rp

    async def _explode(ctx, **kwargs):
        raise AssertionError("no approval should be created")

    monkeypatch.setattr(rp, "get_provider",
                        lambda: type("P", (), {"name": "x", "produces": "captured",
                                               "available": lambda self: (False, "no credits")})())
    monkeypatch.setattr("approval_service.create_approval", _explode)

    result = _request("request_property_reconstruction", _Conn(), lead_id=LEAD_ID)

    assert result["ok"] is False
    assert "no credits" in result["error"]


def test_a_property_outside_this_workspace_stages_nothing(monkeypatch):
    async def _explode(ctx, **kwargs):
        raise AssertionError("no approval should be created for an unseen property")

    monkeypatch.setattr("approval_service.create_approval", _explode)

    result = _request("request_property_reconstruction", _Conn(exists=False), lead_id=LEAD_ID)

    assert result["ok"] is False
    assert "workspace" in result["error"]


def test_a_malformed_id_is_refused_before_anything_is_created(monkeypatch):
    async def _explode(ctx, **kwargs):
        raise AssertionError("no approval should be created")

    monkeypatch.setattr("approval_service.create_approval", _explode)

    result = _request("request_property_reconstruction", _Conn(), lead_id="not-a-uuid")

    assert result["ok"] is False and "UUID" in result["error"]


def test_a_video_is_requested_and_labelled_ai_generated(monkeypatch):
    import video_providers

    created: list = []

    async def _create_approval(ctx, **kwargs):
        created.append(kwargs)
        return {"id": "ap-2"}

    monkeypatch.setattr(video_providers, "get_provider",
                        lambda: type("V", (), {"name": "kling",
                                               "available": lambda self: (True, "")})())
    monkeypatch.setattr("approval_service.create_approval", _create_approval)

    result = _request("request_listing_video", _Conn(), lead_id=LEAD_ID,
                      brief="Show the kitchen and the garden.")

    assert result["ok"] is True
    assert result["generated"] is False
    # A generated walkthrough is not footage of the home, and the label travels
    # with the media rather than being decided later.
    assert created[0]["draft_payload"]["provenance"] == "ai_generated"


def test_a_video_request_needs_a_brief(monkeypatch):
    async def _explode(ctx, **kwargs):
        raise AssertionError("no approval should be created")

    monkeypatch.setattr("approval_service.create_approval", _explode)

    result = _request("request_listing_video", _Conn(), lead_id=LEAD_ID, brief="  ")

    assert result["ok"] is False and "brief" in result["error"]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_the_spend_tools_are_gated_and_the_read_tool_is_not():
    import ai_tool_policy

    assert "request_property_reconstruction" in ai_tool_policy.GATED_TOOLS
    assert "request_listing_video" in ai_tool_policy.GATED_TOOLS
    assert "get_property_tour" in ai_tool_policy.READ_ONLY_TOOLS


def test_all_three_are_offered_to_the_model():
    """A handler with no catalog entry is a tool the model can never call."""
    import ai_chat_agent

    for name in ("get_property_tour", "request_property_reconstruction",
                 "request_listing_video"):
        assert name in ai_chat_agent.TOOLS, name
