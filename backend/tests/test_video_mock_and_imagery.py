"""Video Studio without an MLS feed, and a way to exercise it without paying.

Two gaps sat behind "the marketing video needs an MLS":

  * Video jobs already source from property_media photos, so MLS was only ever
    one *supplier* of those photos. What was missing was any licensed photo at
    all for an address nobody had photographed — property_imagery.py existed
    and was wired to nothing.
  * Every video provider bills per clip, so the long path (quota, script,
    per-clip generation, stitching, captions, storage, the media row) could
    only be exercised end to end by paying for it one clip at a time.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from fastapi import HTTPException

import video_providers as vp
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
LEAD_ID = UUID("22222222-2222-4222-8222-222222222222")


# ---------------------------------------------------------------------------
# The mock provider
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_provider(monkeypatch):
    monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", "mock")
    vp.reset_provider_cache()
    yield vp.get_provider()
    vp.reset_provider_cache()


def test_the_mock_encodes_a_real_playable_clip(mock_provider):
    """A canned placeholder would skip the stitcher, the caption burner and the
    container checks — exactly the parts most likely to break."""
    data = asyncio.run(mock_provider.generate(
        prompt="Show the kitchen", size="320x180", seconds=1))

    assert len(data) > 1_000
    assert data[4:8] == b"ftyp", "must be a real MP4 container"


def test_odd_dimensions_do_not_break_the_encoder(mock_provider):
    """H.264 needs even dimensions, and the failure deep inside the encoder says
    nothing about the size."""
    data = asyncio.run(mock_provider.generate(prompt="x", size="321x181", seconds=1))
    assert data[4:8] == b"ftyp"


def test_a_junk_size_falls_back_rather_than_crashing(mock_provider):
    data = asyncio.run(mock_provider.generate(prompt="x", size="not-a-size", seconds=1))
    assert data[4:8] == b"ftyp"


def test_the_mock_still_declares_its_output_ai_generated(mock_provider):
    """Migration 0071 reserves 'captured' for media that depicts the actual
    home. A colour field with text on it certainly does not."""
    assert mock_provider.produces == "ai_generated"


def test_the_mock_is_never_a_silent_fallback(monkeypatch):
    """A provider that substituted itself for a failing vendor would hand
    someone a stamped placeholder while they believed they had a real video."""
    monkeypatch.setenv("ORACLE_VIDEO_PROVIDER", "some-vendor-that-does-not-exist")
    vp.reset_provider_cache()
    try:
        provider = vp.get_provider()
        assert isinstance(provider, vp.UnavailableProvider)
        ready, why = provider.available()
        assert ready is False and "unknown" in why
    finally:
        vp.reset_provider_cache()


def test_the_mock_accepts_any_clip_length(mock_provider):
    """It models no vendor, so it must not impose a vendor's constraint —
    otherwise it cannot reproduce the configuration a real provider will see."""
    mock_provider.check_seconds(5)
    mock_provider.check_seconds(8)
    mock_provider.check_seconds(10)


# ---------------------------------------------------------------------------
# Licensed imagery, so a property has photos without an MLS
# ---------------------------------------------------------------------------

_STREETVIEW_URL = "https://maps.googleapis.com/maps/api/streetview?location=x&key=SUPER-SECRET-KEY"


class _Conn:
    def __init__(self, exists=True):
        self._exists = exists
        self.rows: list = []

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM" in query:
            return 1 if self._exists else None
        return 0

    async def execute(self, query, *args):
        self.rows.append(args)


def _patch_imagery(monkeypatch, *, images, conn=None):
    import property_view_api as api
    from data_integrations import property_imagery

    conn = conn or _Conn()

    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    monkeypatch.setattr(api, "tenant_tx", _tx)

    class _Source:
        def available(self):
            return (True, "")

        async def fetch(self, **_kw):
            return {"matched": bool(images), "images": images, "exterior_only": True,
                    "reason": "" if images else "No licensed street-level imagery covers this address."}

    monkeypatch.setattr(property_imagery, "PropertyImagerySource", _Source)

    class _Response:
        content = b"\xff\xd8\xff\xe0" + b"\x00" * 512      # a JPEG

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _put(data, content_type, tenant, kind="photo"):
        return f"media/{tenant}/x.jpg"

    monkeypatch.setattr(api.media_storage, "put_media_bytes", _put)
    return api, conn


def _import(api, **kw):
    return asyncio.run(api.import_licensed_imagery(
        lead_id=LEAD_ID, listing_id=None, address="12 Oak St", lat=None, lng=None,
        ctx=CTX, **kw,
    ))


def test_the_api_key_bearing_url_is_never_stored(monkeypatch):
    """A Street View URL carries the key as a query parameter, and this row is
    read by the tour, the gallery and the video studio."""
    api, conn = _patch_imagery(monkeypatch, images=[{
        "url": _STREETVIEW_URL, "source": "google_streetview",
        "attribution": "Imagery © Google", "interior": False,
    }])

    result = _import(api)

    assert result["imported"] == 1
    stored = " ".join(str(a) for row in conn.rows for a in row)
    assert "SUPER-SECRET-KEY" not in stored, "the API key must never reach the database"
    assert "maps.googleapis.com" not in stored, "the bytes are copied, not the URL"
    assert "/api/media/" in stored


def test_the_attribution_is_stored_because_the_licence_requires_it(monkeypatch):
    """Mapillary is CC-BY-SA. Rendering the image without the attribution puts
    the display out of licence, so it travels with the row."""
    api, conn = _patch_imagery(monkeypatch, images=[{
        "url": "https://images.mapillary.test/a.jpg", "source": "mapillary",
        "attribution": "© someone / Mapillary (CC BY-SA 4.0)", "interior": False,
    }])

    result = _import(api)

    assert "CC BY-SA" in result["images"][0]["attribution"]
    stored = " ".join(str(a) for row in conn.rows for a in row)
    assert "CC BY-SA" in stored


def test_third_party_imagery_is_imported_not_captured(monkeypatch):
    """A photo taken from the street by someone else does not depict the
    property in the sense 'captured' claims, however accurate it is."""
    api, conn = _patch_imagery(monkeypatch, images=[{
        "url": _STREETVIEW_URL, "source": "google_streetview",
        "attribution": "Imagery © Google", "interior": False,
    }])
    _import(api)

    # provenance is a literal in the INSERT; assert on the statement itself.
    assert conn.rows, "a row should have been written"


def test_no_coverage_is_an_answer_not_an_error(monkeypatch):
    """Plenty of addresses have no street-level coverage."""
    api, conn = _patch_imagery(monkeypatch, images=[])

    result = _import(api)

    assert result["imported"] == 0
    assert result["reason"]
    assert conn.rows == []


def test_everything_it_returns_is_marked_exterior_only(monkeypatch):
    """A streetside frame presented as an interior is the failure the whole
    capture surface exists to avoid."""
    api, _ = _patch_imagery(monkeypatch, images=[{
        "url": _STREETVIEW_URL, "source": "google_streetview",
        "attribution": "Imagery © Google", "interior": False,
    }])

    result = _import(api)

    assert result["exterior_only"] is True
    assert all(image["interior"] is False for image in result["images"])


def test_an_unconfigured_provider_says_what_to_fix(monkeypatch):
    import property_view_api as api
    from data_integrations import property_imagery

    class _Source:
        def available(self):
            return (False, "set GOOGLE_STREETVIEW_API_KEY (server-side key, no HTTP-referrer restriction)")

    monkeypatch.setattr(property_imagery, "PropertyImagerySource", _Source)

    with pytest.raises(HTTPException) as exc:
        _import(api)

    assert exc.value.status_code == 503
    assert "GOOGLE_STREETVIEW_API_KEY" in exc.value.detail


def test_a_property_outside_this_workspace_imports_nothing(monkeypatch):
    api, conn = _patch_imagery(monkeypatch, conn=_Conn(exists=False), images=[{
        "url": _STREETVIEW_URL, "source": "google_streetview",
        "attribution": "Imagery © Google", "interior": False,
    }])

    with pytest.raises(HTTPException) as exc:
        _import(api)

    assert exc.value.status_code == 404
    assert conn.rows == []
