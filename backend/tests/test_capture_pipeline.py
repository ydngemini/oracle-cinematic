"""Capture input: a phone walk video must be usable, and traceable afterwards.

Two gaps this covers.

`_gather_source_images` read `kind='photo'` only, so a capture consisting of a
walk-through video — the natural way to shoot a house with a phone — yielded
zero images. The job then "succeeded" with nothing behind it.

And a finished splat recorded no link to the media that produced it, so a bad
capture could not be diagnosed and a re-capture could not supersede it.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

import reconstruction_worker as worker
from reconstruction_providers import ProviderError
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed here (it is in the backend image)",
)


def _job() -> worker.ReconstructionJob:
    return worker.ReconstructionJob(
        ctx=CTX, job_id=str(uuid4()), lead_id=str(uuid4()), listing_id=None
    )


def _make_video(path: Path, seconds: int = 3) -> None:
    """A real, decodable clip — a fake byte string would only test the mock."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
            str(path),
        ],
        check=True,
    )


def _gather(dest: Path):
    return worker._gather_source_images(_job(), dest)


def _patch_media(monkeypatch, rows, blobs):
    class _Conn:
        async def fetch(self, *_a, **_k):
            return rows

    @asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    async def _load(row):
        return blobs.get(str(row["id"]))

    monkeypatch.setattr(worker, "tenant_tx", _tx)
    monkeypatch.setattr(worker.media_storage, "load_media_bytes", _load)


def test_a_capture_video_becomes_frames(tmp_path, monkeypatch):
    video = tmp_path / "walk.mp4"
    _make_video(video, seconds=3)
    media_id = str(uuid4())

    _patch_media(
        monkeypatch,
        [{"id": media_id, "kind": "video", "s3_key": None, "bytes": None,
          "content_type": "video/mp4"}],
        {media_id: video.read_bytes()},
    )

    images, counts = asyncio.run(_gather(tmp_path / "work"))

    assert counts["videos"] == 1
    assert counts["frames"] > 0, "a 3s clip at 2fps should yield several frames"
    assert len(images) == counts["frames"]
    assert all(p.suffix == ".jpg" and p.is_file() for p in images)
    # The container itself must not reach a provider expecting images.
    assert not any(p.suffix in {".mp4", ".mov"} for p in images)


def test_photos_and_video_are_both_counted(tmp_path, monkeypatch):
    video = tmp_path / "walk.mp4"
    _make_video(video, seconds=2)
    photo_id, video_id = str(uuid4()), str(uuid4())

    # A 1x1 PNG is enough — nothing here decodes it.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
        "0000000049454e44ae426082"
    )

    _patch_media(
        monkeypatch,
        [
            {"id": photo_id, "kind": "photo", "s3_key": None, "bytes": None,
             "content_type": "image/png"},
            {"id": video_id, "kind": "video", "s3_key": None, "bytes": None,
             "content_type": "video/mp4"},
        ],
        {photo_id: png, video_id: video.read_bytes()},
    )

    images, counts = asyncio.run(_gather(tmp_path / "work"))

    assert counts["photos"] == 1
    assert counts["videos"] == 1
    assert counts["frames"] > 0
    assert len(images) == 1 + counts["frames"]


def test_missing_ffmpeg_fails_the_job_rather_than_reconstructing_nothing(tmp_path, monkeypatch):
    """The failure this whole phase exists to remove: a job that succeeds empty."""
    video = tmp_path / "walk.mp4"
    _make_video(video, seconds=1)
    media_id = str(uuid4())

    _patch_media(
        monkeypatch,
        [{"id": media_id, "kind": "video", "s3_key": None, "bytes": None,
          "content_type": "video/mp4"}],
        {media_id: video.read_bytes()},
    )
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(_gather(tmp_path / "work"))

    assert "ffmpeg" in str(excinfo.value)


def test_an_undecodable_video_fails_loudly(tmp_path, monkeypatch):
    media_id = str(uuid4())
    _patch_media(
        monkeypatch,
        [{"id": media_id, "kind": "video", "s3_key": None, "bytes": None,
          "content_type": "video/mp4"}],
        {media_id: b"this is not a video"},
    )

    with pytest.raises(ProviderError):
        asyncio.run(_gather(tmp_path / "work"))


def test_photo_only_captures_are_unaffected(tmp_path, monkeypatch):
    """The common path must not have changed shape."""
    photo_id = str(uuid4())
    _patch_media(
        monkeypatch,
        [{"id": photo_id, "kind": "photo", "s3_key": None, "bytes": None,
          "content_type": "image/jpeg"}],
        {photo_id: b"\xff\xd8\xff-not-really-decoded-here"},
    )

    images, counts = asyncio.run(_gather(tmp_path / "work"))

    assert counts == {"photos": 1, "videos": 0, "frames": 0}
    assert len(images) == 1
