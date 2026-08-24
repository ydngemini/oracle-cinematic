"""A phone scan becomes a walkable tour, and the claim it makes has an author.

This is the path that needs no GPU at all. Scaniverse and Polycam process a scan
on the device and export a finished splat, so someone walks the house with a
phone and the tour exists — which is the honest answer to "walk inside this
home" that no amount of address lookup can produce.

Two invariants. Format is decided by BYTES, never by filename or Content-Type,
because this file goes on to be presented as a walkthrough of someone's home.
And `provenance='captured'` is the flag that makes the tour say "you are walking
through this home" — nothing in the file can prove the address, so that claim is
made explicitly by a named person rather than assumed from an upload.
"""

from __future__ import annotations

import asyncio
import gzip
import io
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile

import property_view_api as api
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)
LEAD_ID = UUID("22222222-2222-4222-8222-222222222222")


# --- the sniffer, on bytes alone ---------------------------------------------

def test_a_ply_is_recognised():
    assert api._sniff_pointcloud(b"ply\nformat binary_little_endian 1.0\n") == "application/x-ply"


def test_an_spz_v4_is_recognised_by_its_magic():
    assert api._sniff_pointcloud(b"NGSP" + b"\x04\x00\x00\x00" + b"\x00" * 32) == "application/x-spz"


def test_a_legacy_gzipped_spz_is_recognised_after_decompression():
    """v1-v3 wrap the same payload in gzip, so the magic only appears once it is
    decompressed."""
    payload = b"NGSP" + b"\x02\x00\x00\x00" + b"\x00" * 64
    assert api._sniff_pointcloud(gzip.compress(payload)) == "application/x-spz"


def test_a_gzip_that_is_not_an_spz_is_refused():
    """"It decompresses" is not evidence of what it is."""
    assert api._sniff_pointcloud(gzip.compress(b"just some text")) is None


def test_a_photo_is_not_a_scan():
    assert api._sniff_pointcloud(b"\xff\xd8\xff\xe0" + b"\x00" * 32) is None


def test_a_filename_cannot_make_something_a_scan():
    """Content-Type and filename are whatever the client says they are."""
    assert api._sniff_pointcloud(b"<html>not a splat</html>") is None


# --- the route ---------------------------------------------------------------

class _Conn:
    def __init__(self, exists=True):
        self._exists = exists
        self.inserted: list = []

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM" in query:
            return 1 if self._exists else None
        return 0                      # COALESCE(MAX(sort_order), -1) + 1

    async def execute(self, query, *args):
        self.inserted.append((query, args))


def _patch(monkeypatch, *, conn=None, storage=True, convert_fails=False):
    conn = conn or _Conn()

    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    monkeypatch.setattr(api, "tenant_tx", _tx)

    fake_storage = type("s", (), {"is_configured": staticmethod(lambda: storage)})
    monkeypatch.setitem(__import__("sys").modules, "object_storage", fake_storage)

    import reconstruction_worker as worker
    from reconstruction_providers import ProviderError

    stored: dict = {}

    async def _convert(src, work_dir, media_id):
        if convert_fails:
            raise ProviderError("splat-transform could not convert upload.spz to .sog")
        out = Path(work_dir) / f"{media_id}.sog"
        out.write_bytes(b"SOG\x00converted")
        return out

    async def _store(src, media_id, **kwargs):
        stored.update(kwargs)
        stored["media_id"] = media_id
        return (f"/api/media/{media_id}", f"splats/{TENANT_ID}/{media_id}.sog")

    monkeypatch.setattr(worker, "_convert_to_delivery", _convert)
    monkeypatch.setattr(worker, "_store_splat", _store)

    ledgered: list = []

    class _Ledger:
        async def record(self, **kwargs):
            ledgered.append(kwargs)

    import audit_ledger
    monkeypatch.setattr(audit_ledger, "ledger", _Ledger())
    return conn, stored, ledgered


def _upload(data: bytes, name="scan.spz"):
    return UploadFile(filename=name, file=io.BytesIO(data))


def _run(monkeypatch, data=b"NGSP\x04\x00\x00\x00", *, attested=True,
         capture_app="scaniverse", **patch_kw):
    conn, stored, ledgered = _patch(monkeypatch, **patch_kw)
    result = asyncio.run(api.upload_property_scan(
        lead_id=LEAD_ID, listing_id=None, file=_upload(data),
        capture_app=capture_app, attested=attested, ctx=CTX,
    ))
    return result, conn, stored, ledgered


def test_a_scan_is_stored_as_a_captured_splat(monkeypatch):
    result, conn, stored, _ = _run(monkeypatch)

    assert result["provenance"] == "captured"
    assert result["kind"] == "splat"
    assert result["generator"] == "scaniverse"
    insert_sql, args = conn.inserted[0]
    assert "INSERT INTO property_media" in insert_sql
    assert "'captured'" in insert_sql


def test_an_unattested_scan_is_refused_and_nothing_is_stored(monkeypatch):
    """`captured` is what makes the tour claim this is the actual home, and
    nothing in the file can prove the address."""
    conn, stored, ledgered = _patch(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="scaniverse", attested=False, ctx=CTX,
        ))

    assert exc.value.status_code == 422
    assert conn.inserted == [], "nothing may be recorded without the attestation"
    assert stored == {}


def test_the_attestation_names_who_made_it(monkeypatch):
    """Strike this and 'captured' is an assertion nobody is accountable for."""
    result, _, stored, ledgered = _run(monkeypatch)

    assert stored["extra_manifest"]["attestedBy"] == "agent@tenant.test"
    assert ledgered and ledgered[0]["action"] == "property_scan_attested"
    assert ledgered[0]["user_id"] == "agent@tenant.test"
    assert ledgered[0]["target_id"] == result["media_id"]


def test_a_scan_is_not_labelled_ai_generated(monkeypatch):
    """A phone scan is a photographic record of a real room, not a
    reconstruction inferred from photos. The AI disclosure would be a false
    statement about how it was made."""
    _, _, stored, _ = _run(monkeypatch)

    assert stored["generated"] is False


def test_a_capture_app_is_required(monkeypatch):
    conn, _, _ = _patch(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="   ", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 422
    assert conn.inserted == []


def test_a_photo_uploaded_as_a_scan_is_refused(monkeypatch):
    conn, _, _ = _patch(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None,
            file=_upload(b"\xff\xd8\xff\xe0" + b"\x00" * 64, name="scan.spz"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 415
    assert conn.inserted == []


def test_an_oversize_scan_names_the_smaller_format(monkeypatch):
    """The fix is an export setting, so the error should say so."""
    conn, _, _ = _patch(monkeypatch)
    huge = b"NGSP" + b"\x00" * (api.MAX_SCAN_BYTES + 16)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(huge),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 413
    assert "SPZ" in exc.value.detail
    assert conn.inserted == []


def test_a_property_outside_this_workspace_stores_nothing(monkeypatch):
    conn, stored, _ = _patch(monkeypatch, conn=_Conn(exists=False))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 404
    assert stored == {}


def test_exactly_one_property_anchor_is_required(monkeypatch):
    _patch(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=None, listing_id=None, file=_upload(b"NGSP\x04"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))
    assert exc.value.status_code == 422


def test_a_conversion_failure_is_reported_not_swallowed(monkeypatch):
    """An SPZ v4 hitting a splat-transform too old to read it fails here, and
    the message has to name the format rather than say 'upload failed'."""
    conn, _, _ = _patch(monkeypatch, convert_fails=True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 422
    assert ".sog" in exc.value.detail
    assert conn.inserted == []


def test_no_storage_backend_refuses_before_converting(monkeypatch):
    conn, _, _ = _patch(monkeypatch, storage=False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 503
    assert conn.inserted == []
