"""Uploads that reach a native decoder are checked by content, not by name (T17.10, L17-28).

Two upload routes handed client bytes to a decoder that picks the format from
the content itself:

* ``/api/crm/floorplan/extract-image`` passed any bytes to OpenCV's
  ``cv2.imdecode``, which also decodes TIFF, JPEG 2000, PFM, HDR and the
  Netpbm family; only the pixel count was bounded.
* ``/api/voice/log-walkthrough/{lead_id}`` checked the file name's suffix and
  queued the bytes for faster-whisper, which opens them with ffmpeg (PyAV).
  ffmpeg probes the content, so a ``.mp3`` holding an HLS playlist or any other
  demuxer's input was accepted.

Both now sniff the leading bytes against a short allow-list and refuse
everything else with 415 before any database work or decoding.
"""

from __future__ import annotations

import asyncio
import io
import struct
import uuid

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import floorplan_api
import voice_intel
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="agent-a", tenant_id=str(uuid.uuid4()), role=Role.AGENT)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + struct.pack("<I", 40) + b"WEBPVP8 " + b"\x00" * 24
BMP = b"BM" + b"\x00" * 40
TIFF_LE = b"II*\x00" + b"\x00" * 32
TIFF_BE = b"MM\x00*" + b"\x00" * 32
JP2 = b"\x00\x00\x00\x0cjP  \r\n\x87\n" + b"\x00" * 24
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 24
PFM = b"PF\n640 480\n-1.0\n" + b"\x00" * 16


def _upload(data: bytes, name: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


# --------------------------------------------------------------------------- floor plan


@pytest.mark.parametrize("data,expected", [
    (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (BMP, "image/bmp"),
])
def test_plan_images_opencv_should_decode_are_recognised(data, expected):
    assert floorplan_api._sniff_plan_image(data) == expected


@pytest.mark.parametrize("data", [TIFF_LE, TIFF_BE, JP2, HEIC, PFM, b"<svg/>", b"%PDF-1.7", b""])
def test_other_formats_are_not_plan_images(data):
    assert floorplan_api._sniff_plan_image(data) is None


@pytest.mark.parametrize("data,hint", [
    (TIFF_LE, "TIFF"), (HEIC, "HEIC"), (JP2, "PNG or JPEG"), (PFM, "PNG or JPEG"),
])
def test_extract_image_refuses_other_formats_before_any_db_work(monkeypatch, data, hint):
    def no_db(*_a, **_k):  # pragma: no cover - reaching it is the failure
        raise AssertionError("the route opened a transaction for a refused upload")

    monkeypatch.setattr(floorplan_api, "tenant_tx", no_db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(floorplan_api.extract_image(
            file=_upload(data, "plan.png"), metres_per_pixel=0.01, known_total_sqft=None,
            wall_height_m=2.5, lead_id=uuid.uuid4(), listing_id=None, ctx=CTX,
        ))
    assert exc.value.status_code == 415
    assert hint in exc.value.detail


def test_a_png_still_reaches_the_subject_check(monkeypatch):
    reached = {}

    class _Tx:
        async def __aenter__(self):
            reached["tx"] = True
            raise RuntimeError("stop here")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(floorplan_api, "tenant_tx", lambda ctx: _Tx())
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(floorplan_api.extract_image(
            file=_upload(PNG, "plan.png"), metres_per_pixel=0.01, known_total_sqft=None,
            wall_height_m=2.5, lead_id=uuid.uuid4(), listing_id=None, ctx=CTX,
        ))
    assert reached == {"tx": True}


# --------------------------------------------------------------------------- walkthrough audio

M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 24
WAV = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 24
MP3_ID3 = b"ID3\x04\x00\x00" + b"\x00" * 30
MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 32
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
OGG = b"OggS\x00\x02" + b"\x00" * 30
HLS = b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nfile:///etc/passwd\n"
AAC_ADTS = b"\xff\xf1\x50\x80" + b"\x00" * 32  # MPEG sync with layer 0: AAC, not MP3


@pytest.mark.parametrize("data,expected", [
    (M4A, ".m4a"), (WAV, ".wav"), (MP3_ID3, ".mp3"), (MP3_FRAME, ".mp3"),
    (WEBM, ".webm"), (OGG, ".ogg"),
])
def test_allowed_audio_containers_are_recognised(data, expected):
    assert voice_intel._sniff_audio(data) == expected


@pytest.mark.parametrize("data", [HLS, AAC_ADTS, PNG, b"%PDF-1.7", WEBP, b""])
def test_other_content_is_not_walkthrough_audio(data):
    assert voice_intel._sniff_audio(data) is None


def test_a_playlist_named_mp3_is_refused_before_any_db_work(monkeypatch):
    def no_db(*_a, **_k):  # pragma: no cover - reaching it is the failure
        raise AssertionError("the route opened a transaction for a refused upload")

    monkeypatch.setattr(voice_intel, "tenant_tx", no_db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(voice_intel.log_walkthrough(
            lead_id=str(uuid.uuid4()), audio_file=_upload(HLS, "note.mp3"), ctx=CTX,
        ))
    assert exc.value.status_code == 415


def test_accepted_audio_is_stored_under_the_suffix_its_content_has(monkeypatch, tmp_path):
    """A WebM recording named .ogg is accepted, and queued as .webm."""
    stored = {}

    class _Conn:
        async def fetchval(self, sql, *args):
            stored["args"] = args
            return "job-1"

    class _Tx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    async def lead_ok(conn, lead_id):
        return None

    async def backlog(*_a, **_k):
        return 0

    monkeypatch.setattr(voice_intel, "tenant_tx", lambda ctx: _Tx())
    monkeypatch.setattr(voice_intel.record_access, "require_lead_edit", lead_ok)
    monkeypatch.setattr(voice_intel.record_access, "pipeline_count", backlog)
    monkeypatch.setattr(voice_intel, "_transcription_available", lambda: True)
    monkeypatch.setattr(voice_intel, "STAGING_DIR", tmp_path)

    out = asyncio.run(voice_intel.log_walkthrough(
        lead_id=str(uuid.uuid4()), audio_file=_upload(WEBM, "note.ogg"), ctx=CTX,
    ))
    assert out["status"] == "queued"
    suffix, audio = stored["args"][4], stored["args"][5]
    assert suffix == ".webm"
    assert audio == WEBM  # the whole file, not the bytes after the sniffed header
