"""Unit tests for the Video Marketing Studio pipeline (no live Azure/DB).

Covers the Sora 2 REST client, script drafting, PyAV stitching orchestration,
quota gating, and the API surface — all provider and database interactions are
mocked so the suite runs anywhere, matching the existing test conventions.
"""

from __future__ import annotations

import asyncio
import io
import json
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import object_storage
import video_studio as studio
import video_studio_api as api
from tenancy import Role, TenantContext


@pytest.fixture(autouse=True)
def _object_storage_in_tmp(tmp_path, monkeypatch):
    """Point durable storage at a tmp dir for every test in this module.

    _store_video writes the finished MP4 to object storage rather than
    media_blobs — migration 0066 enforces
    ``CHECK (kind <> 'video' OR s3_key IS NOT NULL)`` — so a job that reaches
    the storage step would otherwise try to write the real /mnt/neoh share.
    """
    monkeypatch.setattr(object_storage, "BACKEND", "azure-files")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
JOB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MEDIA_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LEAD_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

CTX = TenantContext(agent_id="agent@example.test", tenant_id=TENANT_ID, role=Role.AGENT)


def _fake_tenant_tx(conn, seen_contexts=None):
    @asynccontextmanager
    async def tx(ctx):
        if seen_contexts is not None:
            seen_contexts.append(ctx)
        yield conn

    return tx


class _FakeConn:
    """In-memory conn covering the worker/API queries used by these tests."""

    def __init__(self, *, quota_consumed=0.0, images=None, fetchrow_results=None):
        self.quota_consumed = quota_consumed
        self.images = images or []
        self.fetchrow_results = fetchrow_results or {}
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(("fetchval", query, args))
        if "SUM(quota_consumed_units)" in query:
            return self.quota_consumed
        if "MAX(sort_order)" in query:
            return -1
        return 0

    async def fetchrow(self, query, *args):
        self.queries.append(("fetchrow", query, args))
        if "SUM(quota_consumed_units)" in query:
            return {"consumed": self.quota_consumed}
        if "SELECT id, status FROM video_studio_jobs" in query:
            return self.fetchrow_results.get("job")
        return None

    async def fetch(self, query, *args):
        self.queries.append(("fetch", query, args))
        if "JOIN media_blobs" in query:
            return [{"bytes": data} for data in self.images]
        return []

    async def execute(self, query, *args):
        self.queries.append(("execute", query, args))
        return "UPDATE 1"


# ---------------------------------------------------------------------------
# Sora client
# ---------------------------------------------------------------------------
def test_headers_use_api_key_when_configured(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(studio, "SORA_API_KEY", "sk-test")
    headers = studio._sora_headers()
    assert headers == {"api-key": "sk-test"}


def test_headers_use_bearer_token_without_api_key(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_API_KEY", "")
    monkeypatch.setattr(studio, "SORA_API_KEY", "")
    monkeypatch.setattr(studio, "_sora_token", lambda: "entra-token")
    headers = studio._sora_headers()
    assert headers == {"Authorization": "Bearer entra-token"}


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.content = content

    def json(self):
        return self.payload

    @property
    def text(self):
        return json.dumps(self.payload)


def test_submit_posts_multipart_and_returns_job_id(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_headers", lambda: {"api-key": "k"})
    calls = []

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        calls.append((url, data, files, headers))
        return _FakeResponse(status_code=202, payload={"id": "videos/abc123"})

    monkeypatch.setattr(requests, "post", fake_post)

    job_id = studio._sora_submit(
        prompt="cinematic house tour", size="720x1280", seconds=8,
        image_bytes=b"JPEGDATA",
    )

    assert job_id == "videos/abc123"
    url, data, files, headers = calls[0]
    assert url == "https://neoh.openai.azure.com/openai/v1/videos"
    assert data["model"] == studio.SORA_DEPLOYMENT
    assert data["size"] == "720x1280"
    assert files is not None
    assert files["input_reference"][0] == "input.jpg"


def test_submit_rejects_provider_error(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_headers", lambda: {"api-key": "k"})

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        return _FakeResponse(status_code=400, payload={"error": {"message": "bad prompt"}})

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(studio.VideoStudioError) as error:
        studio._sora_submit(prompt="x", size="720x1280", seconds=8)

    assert "400" in str(error.value)


def test_status_maps_success_failure_and_errors(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_headers", lambda: {"api-key": "k"})

    def fake_get(url, headers=None, timeout=None):
        if "videos/ok" in url:
            return _FakeResponse(status_code=200, payload={"status": "succeeded"})
        if "videos/fail" in url:
            return _FakeResponse(status_code=200, payload={"status": "failed", "error": {"message": "content filter"}})
        if "videos/pending" in url:
            return _FakeResponse(status_code=200, payload={"status": "running"})
        if "videos/missing" in url:
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=200, payload={"status": "succeeded"})

    monkeypatch.setattr(requests, "get", fake_get)

    assert studio._sora_status("videos/ok") == ("succeeded", "")
    assert studio._sora_status("videos/pending") == ("running", "")
    status, error = studio._sora_status("videos/fail")
    assert status == "failed"
    assert "content filter" in error
    assert studio._sora_status("videos/missing") == ("failed", "provider job videos/missing not found")


def test_download_returns_video_bytes(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_headers", lambda: {"api-key": "k"})

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(status_code=200, content=b"MP4BYTES")

    monkeypatch.setattr(requests, "get", fake_get)

    assert studio._sora_download("videos/ok") == b"MP4BYTES"


def test_generate_clip_end_to_end(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_headers", lambda: {"api-key": "k"})

    submitted = []

    def fake_submit(*, prompt, size, seconds, image_bytes=None):
        submitted.append((prompt, size, seconds, image_bytes))
        return "videos/j1"

    monkeypatch.setattr(studio, "_sora_submit", fake_submit)
    monkeypatch.setattr(studio, "_sora_status", lambda job_id: ("succeeded", ""))
    monkeypatch.setattr(studio, "_sora_download", lambda job_id: b"MP4BYTES")

    result = asyncio.run(studio.generate_clip("prompt", "720x1280", 8, image_bytes=b"IMG"))

    assert result == b"MP4BYTES"
    assert submitted == [("prompt", "720x1280", 8, b"IMG")]


def test_generate_clip_propagates_provider_failure(monkeypatch):
    monkeypatch.setenv("ORACLE_AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "AZURE_OPENAI_ENDPOINT", "https://neoh.openai.azure.com")
    monkeypatch.setattr(studio, "_sora_submit", lambda **kw: "videos/j1")
    monkeypatch.setattr(studio, "_sora_status", lambda job_id: ("failed", "content filter"))

    with pytest.raises(studio.VideoStudioError) as error:
        asyncio.run(studio.generate_clip("prompt", "720x1280", 8))

    assert "content filter" in str(error.value)


# ---------------------------------------------------------------------------
# Script drafting
# ---------------------------------------------------------------------------
def test_template_script_is_deterministic_without_azure():
    script = studio._template_script(
        {"address": "123 Main St", "bedrooms": 3, "bathrooms": 2, "sqft": 1500}
    )
    assert "123 Main St" in script
    assert "3 bedrooms" in script


def test_draft_script_falls_back_to_template_when_endpoint_missing(monkeypatch):
    monkeypatch.delenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    script = studio.draft_script({"address": "456 Oak Ave"})
    assert "456 Oak Ave" in script


def test_draft_script_uses_foundry_when_configured(monkeypatch):
    monkeypatch.setenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    calls = []

    class _FakeResponse:
        output_text = "A cinematic tour of the property."

    class _FakeClient:
        def __init__(self):
            self.responses = self

        def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeResponse()

    monkeypatch.setattr(studio, "_foundry_responses_client", lambda: _FakeClient())

    script = studio.draft_script({"address": "789 Elm"})

    assert script == "A cinematic tour of the property."
    assert calls[0]["model"] == "Kimi-K2.6"


def test_per_clip_prompts_split_multiple_images():
    prompts = studio._per_clip_prompts("One. Two. Three. Four. Five.", 4)
    assert len(prompts) == 4
    assert all("cinematic" in p for p in prompts)


def test_per_clip_prompts_single_image_keeps_script():
    prompts = studio._per_clip_prompts("One sentence only.", 1)
    assert prompts == ["One sentence only."]


# ---------------------------------------------------------------------------
# Burned-in captions — social reels autoplay muted.
# ---------------------------------------------------------------------------
def test_captions_and_prompts_come_from_the_same_segmentation():
    """If they drifted, the words on screen would describe a different shot than
    the one the clip was generated from."""
    script = "One. Two. Three. Four."
    segments = studio._script_segments(script, 2)
    prompts = studio._per_clip_prompts(script, 2)
    assert len(segments) == len(prompts) == 2
    for segment, prompt in zip(segments, prompts):
        assert prompt.startswith(segment)


def test_caption_text_truncates_visibly_rather_than_silently():
    long = "word " * 60
    caption = studio.caption_text(long)
    assert len(caption) <= studio.CAPTION_MAX_CHARS + 1
    assert caption.endswith("…")


def test_caption_text_leaves_short_narration_intact():
    assert studio.caption_text("  Book a  private tour ") == "Book a private tour"


def test_caption_text_strips_the_cinematic_prompt_suffix():
    """`_per_clip_prompts` output must never reach the screen verbatim."""
    prompt = studio._per_clip_prompts("Four bedrooms. Two baths.", 2)[0]
    assert "cinematic" not in studio.caption_text(prompt)


def test_draw_caption_marks_the_lower_third_and_leaves_the_shot_alone():
    from PIL import Image

    base = Image.new("RGB", (720, 1280), (200, 215, 235))
    drawn = studio.draw_caption(base.copy(), "Four bedrooms and a west-facing deck")
    assert drawn.size == (720, 1280)
    before, after = base.load(), drawn.load()
    assert before[360, 100] == after[360, 100]           # the shot itself is untouched
    assert before[360, 1150] != after[360, 1150]         # the caption band is not


def test_draw_caption_is_a_no_op_for_empty_narration():
    from PIL import Image

    base = Image.new("RGB", (320, 240), (7, 7, 7))
    assert studio.draw_caption(base.copy(), "   ").tobytes() == base.tobytes()


def test_wrap_caption_ellipsises_when_it_exceeds_the_line_budget():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (400, 100)))
    font = studio._caption_font(1280)
    lines = studio._wrap_caption(draw, "alpha bravo charlie delta echo foxtrot golf hotel", font, 160)
    assert len(lines) <= studio.CAPTION_MAX_LINES
    assert lines[-1].endswith("…")


def test_captions_default_on_but_are_per_job_overridable(monkeypatch):
    monkeypatch.delenv("ORACLE_VIDEO_CAPTIONS", raising=False)
    assert studio._captions_enabled({}) is True
    assert studio._captions_enabled({"captions": False}) is False
    monkeypatch.setenv("ORACLE_VIDEO_CAPTIONS", "0")
    assert studio._captions_enabled({}) is False
    # An explicit per-job request still wins over the env default.
    assert studio._captions_enabled({"captions": True}) is True


def _synth_clip(path, width, height, frames=4, shade=180):
    """A real encoded MP4 — stitching is exactly the code that mocks hide."""
    import av
    import numpy as np
    from fractions import Fraction

    with av.open(str(path), "w") as out:
        stream = out.add_stream("h264", rate=30)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.time_base = Fraction(1, 30)
        for i in range(frames):
            frame = av.VideoFrame.from_ndarray(
                np.full((height, width, 3), shade, dtype=np.uint8), format="rgb24"
            )
            frame.pts, frame.time_base = i, Fraction(1, 30)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def test_stitching_clips_of_different_sizes_produces_one_monotonic_stream(tmp_path):
    """Every source clip's timestamps restart at zero. Without re-stamping onto a
    single clock the muxer rejects the second clip's first frame."""
    import av

    a, b, out = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "out.mp4"
    _synth_clip(a, 720, 1280, frames=4)
    _synth_clip(b, 640, 480, frames=4, shade=40)

    studio.stitch_clips([a, b], out, size="720x1280")

    with av.open(str(out)) as container:
        stream = container.streams.video[0]
        pts = [f.pts for f in container.decode(video=0)]
    assert (stream.codec_context.width, stream.codec_context.height) == (720, 1280)
    assert len(pts) == 8
    assert all(x < y for x, y in zip(pts, pts[1:]))


def test_stream_copying_identical_clips_keeps_decode_order_monotonic(tmp_path):
    """The production path for Sora output: same codec, same size, so no
    re-encode. B-frames put the first DTS *behind* zero, so each clip has to be
    shifted onto the end of the previous one or the muxer rejects the file.
    """
    import av

    a, b, out = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "out.mp4"
    _synth_clip(a, 720, 1280, frames=6)
    _synth_clip(b, 720, 1280, frames=6, shade=60)

    studio.stitch_clips([a, b], out, size="720x1280")

    with av.open(str(out)) as container:
        stream = container.streams.video[0]
        dts = [p.dts for p in container.demux(stream) if p.dts is not None]
    with av.open(str(out)) as container:
        frames = list(container.decode(video=0))
    assert all(x < y for x, y in zip(dts, dts[1:])), dts
    assert len(frames) == 12          # nothing dropped at the boundary


def test_captions_burn_into_the_pixels_of_the_stitched_video(tmp_path):
    """The whole point: the text must survive into the encoded frames, because
    every social destination drops sidecar subtitle tracks on upload."""
    import av
    import numpy as np

    clip, plain, captioned = tmp_path / "c.mp4", tmp_path / "p.mp4", tmp_path / "k.mp4"
    _synth_clip(clip, 720, 1280, frames=4, shade=220)

    studio.stitch_clips([clip, clip], plain, size="720x1280")
    studio.stitch_clips([clip, clip], captioned, size="720x1280",
                        captions=["Four bedrooms and a west-facing deck", "Book a tour"])

    def first_frame(path):
        with av.open(str(path)) as container:
            return next(container.decode(video=0)).to_ndarray(format="rgb24")

    base, drawn = first_frame(plain), first_frame(captioned)
    band = slice(int(1280 * 0.72), int(1280 * 0.95))
    assert not np.array_equal(base[band], drawn[band])
    assert np.abs(base[:200].astype(int) - drawn[:200].astype(int)).max() < 16


def test_a_single_clip_with_captions_is_not_stream_copied(tmp_path):
    """The one-clip fast path copies the file verbatim — which would silently
    drop the captions the caller asked for."""
    import av
    import numpy as np

    clip, out = tmp_path / "c.mp4", tmp_path / "out.mp4"
    _synth_clip(clip, 720, 1280, frames=4, shade=220)
    studio.stitch_clips([clip], out, size="720x1280", captions=["Book a private tour"])

    with av.open(str(out)) as container:
        drawn = next(container.decode(video=0)).to_ndarray(format="rgb24")
    band = drawn[int(1280 * 0.72):int(1280 * 0.95)]
    assert band.min() < 150          # the dark caption band exists over a bright clip


# ---------------------------------------------------------------------------
# Image resize
# ---------------------------------------------------------------------------
def test_resize_to_size_center_crops_to_target():
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGB", (200, 400), color=(10, 20, 30)).save(source, format="JPEG")
    resized = studio._resize_to_size(source.getvalue(), "720x1280")
    image = Image.open(io.BytesIO(resized))
    assert image.size == (720, 1280)


# ---------------------------------------------------------------------------
# Worker — _process_job with mocked DB + provider
# ---------------------------------------------------------------------------
def _job_row(*, kind="text", script="", images=None, size="720x1280"):
    return {
        "id": JOB_ID,
        "tenant_id": TENANT_ID,
        "kind": kind,
        "size": size,
        "script": script,
        "source": {
            "property": {"address": "123 Main St"},
            "images": images or [],
            "voiceover": False,
            # These tests stub the provider with placeholder bytes, so the real
            # decode-and-redraw caption path has nothing to decode. Caption
            # rendering is covered above against genuinely encoded clips; the
            # wiring is covered by test_process_job_passes_captions_to_stitching.
            "captions": False,
            "lead_id": LEAD_ID,
            "listing_id": None,
        },
    }


def test_process_job_text_end_to_end(monkeypatch):
    conn = _FakeConn(quota_consumed=0.0)
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(studio, "draft_script", lambda prop: "Welcome to 123 Main St.")

    async def fake_generate_clip(*args, **kwargs):
        return b"MP4BYTES"

    monkeypatch.setattr(studio, "generate_clip", fake_generate_clip)
    import ws_hub

    broadcasts = []
    monkeypatch.setattr(ws_hub, "broadcast", lambda tenant, frame: broadcasts.append((tenant, frame)))

    asyncio.run(studio._process_job(_job_row(), "vs-0"))

    statuses = [args[1] for _, query, args in conn.queries if "UPDATE video_studio_jobs" in query]
    assert "ready" in statuses
    assert broadcasts and broadcasts[0][1]["type"] == "VIDEO_READY"


def test_process_job_voiceover_extends_prompts(monkeypatch):
    conn = _FakeConn(quota_consumed=0.0)
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))

    captured = {}

    async def fake_generate_clip(prompt, size, seconds, image_bytes=None):
        captured.setdefault("prompts", []).append(prompt)
        return b"MP4BYTES"

    monkeypatch.setattr(studio, "generate_clip", fake_generate_clip)

    row = _job_row(script="Welcome to 123 Main St.")
    row["source"]["voiceover"] = True
    asyncio.run(studio._process_job(row, "vs-0"))

    assert captured["prompts"], "voiceover job should reach generation"
    for prompt in captured["prompts"]:
        assert "spoken aloud" in prompt


def test_process_job_passes_captions_to_stitching(monkeypatch):
    """The default job burns captions, and they carry the narration — not the
    cinematic direction appended to the Sora prompt."""
    conn = _FakeConn(quota_consumed=0.0)
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))

    async def fake_generate_clip(*args, **kwargs):
        return b"MP4BYTES"

    monkeypatch.setattr(studio, "generate_clip", fake_generate_clip)

    seen = {}

    def fake_stitch(clip_paths, out_path, *, size, captions=None):
        seen["captions"] = captions
        out_path.write_bytes(b"FINAL")

    monkeypatch.setattr(studio, "stitch_clips", fake_stitch)

    row = _job_row(script="Welcome to 123 Main St.")
    row["source"].pop("captions")          # fall through to the env default (on)
    monkeypatch.delenv("ORACLE_VIDEO_CAPTIONS", raising=False)
    asyncio.run(studio._process_job(row, "vs-0"))

    assert seen["captions"] == ["Welcome to 123 Main St."]
    assert "cinematic" not in seen["captions"][0]


def test_process_job_omits_captions_when_the_job_opts_out(monkeypatch):
    conn = _FakeConn(quota_consumed=0.0)
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))

    async def fake_generate_clip(*args, **kwargs):
        return b"MP4BYTES"

    monkeypatch.setattr(studio, "generate_clip", fake_generate_clip)

    seen = {}

    def fake_stitch(clip_paths, out_path, *, size, captions=None):
        seen["captions"] = captions
        out_path.write_bytes(b"FINAL")

    monkeypatch.setattr(studio, "stitch_clips", fake_stitch)
    asyncio.run(studio._process_job(_job_row(script="Welcome."), "vs-0"))
    assert seen["captions"] is None


def test_process_job_quota_exceeded_raises(monkeypatch):
    conn = _FakeConn(quota_consumed=115.0)
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(studio, "DAILY_QUOTA_SECONDS", 120)
    monkeypatch.setattr(studio, "CLIP_SECONDS", 8)

    with pytest.raises(studio.QuotaExceeded):
        asyncio.run(studio._process_job(_job_row(), "vs-0"))


def test_process_job_image_missing_photos_fails(monkeypatch):
    conn = _FakeConn(quota_consumed=0.0, images=[])
    monkeypatch.setattr(studio, "tenant_tx", _fake_tenant_tx(conn))

    async def fake_generate_clip(*args, **kwargs):
        return b"MP4BYTES"

    monkeypatch.setattr(studio, "generate_clip", fake_generate_clip)
    monkeypatch.setattr(studio, "_resize_to_size", lambda data, size: data)

    with pytest.raises(studio.VideoStudioError) as error:
        asyncio.run(
            studio._process_job(
                _job_row(kind="image", images=["dddddddd-dddd-dddd-dddd-dddddddddddd"]),
                "vs-0",
            )
        )
    assert "None of the referenced photos" in str(error.value)


# ---------------------------------------------------------------------------
# API surface — validation + gating (no DB pool)
# ---------------------------------------------------------------------------
def test_create_model_rejects_more_than_max_images(monkeypatch):
    monkeypatch.setattr(studio, "MAX_IMAGES", 4)
    with pytest.raises(ValidationError):
        api.VideoJobCreate(
            kind="image",
            size="720x1280",
            images=[f"{i:032x}" for i in range(1, 6)],
            lead_id=LEAD_ID,
        )


def test_create_model_rejects_duplicate_images():
    with pytest.raises(ValidationError):
        api.VideoJobCreate(
            kind="image",
            size="720x1280",
            images=[
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
            ],
            lead_id=LEAD_ID,
        )


def test_create_model_rejects_invalid_size():
    with pytest.raises(ValidationError):
        api.VideoJobCreate(kind="text", size="800x600", lead_id=LEAD_ID)


def test_property_anchor_required():
    with pytest.raises(HTTPException) as error:
        api._require_property_anchor(
            api.VideoJobCreate(kind="text", size="720x1280")
        )
    assert error.value.status_code == 422


def test_verify_images_owned_rejects_cross_tenant(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(api, "tenant_tx", _fake_tenant_tx(conn))
    import uuid as uuid_module

    # Fake count(*) → 0 → mismatch → 422
    async def fake_fetchval(query, *args):
        return 0

    conn.fetchval = fake_fetchval
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            api._verify_images_owned(
                CTX, [uuid_module.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")]
            )
        )
    assert error.value.status_code == 422


class _ReadyProvider:
    """A configured provider double.

    Tests that are about quota, validation or ownership must not also depend on
    whether a video backend happens to be wired, so they install this.
    """
    name = "fake"
    produces = "ai_generated"
    allowed_seconds = None   # accepts any clip length

    def available(self):
        return (True, "")

    def check_seconds(self, seconds):
        return None


def _install_ready_provider(monkeypatch):
    import video_providers
    monkeypatch.setattr(video_providers, "get_provider", lambda: _ReadyProvider())

def test_create_job_quota_429(monkeypatch):
    _install_ready_provider(monkeypatch)
    conn = _FakeConn(quota_consumed=115.0)
    monkeypatch.setattr(api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(studio, "DAILY_QUOTA_SECONDS", 120)
    monkeypatch.setattr(studio, "CLIP_SECONDS", 8)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            api.create_video_job(
                api.VideoJobCreate(
                    kind="text", size="720x1280",
                    property={"address": "1 A St"}, lead_id=LEAD_ID,
                ),
                CTX,
            )
        )
    assert error.value.status_code == 429
    assert error.value.detail["code"] == "VIDEO_QUOTA_EXCEEDED"


def test_config_surface_never_leaks_secrets(monkeypatch):
    conn = _FakeConn(quota_consumed=12.0)
    monkeypatch.setattr(api, "tenant_tx", _fake_tenant_tx(conn))
    result = asyncio.run(api.video_studio_config(CTX))
    assert result["max_images"] == studio.MAX_IMAGES
    assert "api_key" not in json.dumps(result).lower()
    assert "token" not in json.dumps(result).lower()
