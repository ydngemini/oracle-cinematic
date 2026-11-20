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
from uuid import UUID

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

# What the real converter's failure carries: this server's temp path and the
# tail of Node's output. The log needs it; the uploader must not get it.
_CONVERTER_TEXT = (
    "splat-transform could not convert upload.spz to .sog: Error: unsupported "
    "SPZ version 4 at /tmp/scan_k3j2/upload.spz (node:internal/fs/promises:638)"
)
_MISSING_TEXT = (
    "splat-transform not installed — cannot convert .spz to .sog (npm i -g "
    "@playcanvas/splat-transform@3.3.0; needs Node >= 22)"
)

class _Conn:
    def __init__(self, exists=True):
        self._exists = exists
        self.inserted: list = []

    async def fetchrow(self, query, *args):
        # record_access.require_lead_edit's read-back (0143): an editable lead.
        assert query.startswith("SELECT app_linked_access"), query
        return {"can_edit": True} if self._exists else None

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM" in query:
            return 1 if self._exists else None
        return 0                      # COALESCE(MAX(sort_order), -1) + 1

    async def execute(self, query, *args):
        self.inserted.append((query, args))


def _patch(monkeypatch, *, conn=None, storage=True, convert_fails=False, converter_missing=False):
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
        if converter_missing:
            raise worker.ConverterUnavailable(_MISSING_TEXT)
        if convert_fails:
            raise ProviderError(_CONVERTER_TEXT)
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


def test_a_conversion_failure_is_reported_not_swallowed(monkeypatch, caplog):
    """An SPZ v4 hitting a splat-transform too old to read it fails here. The
    uploader is told which of their files failed and what to export instead;
    the converter's own words, with this server's paths in them, go to the log
    with the input format an operator needs to spot an old converter pin."""
    conn, _, _ = _patch(monkeypatch, convert_fails=True)

    with caplog.at_level("WARNING", logger="oracle.property_view"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.upload_property_scan(
                lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
                capture_app="scaniverse", attested=True, ctx=CTX,
            ))

    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("That SPZ scan could not be converted")
    assert "SPZ (preferred) or PLY" in exc.value.detail
    for leaked in ("/tmp/", "node:", "splat-transform", ".sog"):
        assert leaked not in exc.value.detail
    assert ".spz conversion failed" in caplog.text and _CONVERTER_TEXT in caplog.text
    assert conn.inserted == []


def test_a_server_without_the_converter_is_a_503_not_the_uploaders_fault(monkeypatch, caplog):
    """Nothing about the file is wrong, so a 422 telling the uploader to fix it
    would send them re-exporting a good scan; the install hint is the operator's."""
    conn, stored, _ = _patch(monkeypatch, converter_missing=True)

    with caplog.at_level("ERROR", logger="oracle.property_view"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.upload_property_scan(
                lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
                capture_app="scaniverse", attested=True, ctx=CTX,
            ))

    assert exc.value.status_code == 503
    assert exc.value.detail == api.SCAN_CONVERSION_UNAVAILABLE
    assert _MISSING_TEXT in caplog.text
    assert conn.inserted == [] and stored == {}


def test_the_real_converter_reports_a_missing_install_as_its_own_type(monkeypatch, tmp_path):
    """The route can only tell the two apart if the converter does. It stays a
    ProviderError, so the pipeline keeps the GPU output for a retry as before."""
    import shutil

    import reconstruction_worker as worker
    from reconstruction_providers import ProviderError

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    src = tmp_path / "upload.spz"
    src.write_bytes(b"NGSP\x04\x00\x00\x00")

    with pytest.raises(worker.ConverterUnavailable) as exc:
        asyncio.run(worker._convert_to_delivery(src, tmp_path, "m1"))
    assert isinstance(exc.value, ProviderError)


def _converter_on_path(monkeypatch, tmp_path, name, body):
    """Put a stand-in `splat-transform` or `npx` first on PATH (and only that
    one), then run the real converter on an SPZ."""
    import reconstruction_worker as worker

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / name
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    import shutil
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: real_which(n) if n == name else None)
    src = tmp_path / "upload.spz"
    src.write_bytes(b"NGSP\x04\x00\x00\x00")
    work = tmp_path / "work"
    work.mkdir()
    return worker, src, work


def test_a_converter_that_ran_and_refused_the_file_is_the_files_fault(monkeypatch, tmp_path):
    from reconstruction_providers import ProviderError

    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "splat-transform",
        'echo "Error: unsupported SPZ version 4" >&2; exit 1\n')
    with pytest.raises(ProviderError) as exc:
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))
    assert not isinstance(exc.value, worker.ConverterUnavailable)
    assert "unsupported SPZ version 4" in str(exc.value)


def test_a_converter_killed_by_a_signal_is_the_servers_fault(monkeypatch, tmp_path):
    """An out-of-memory kill says nothing about the file."""
    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "splat-transform", "kill -9 $$\n")
    with pytest.raises(worker.ConverterUnavailable, match="killed by signal 9"):
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))


def test_npx_unable_to_fetch_the_converter_is_the_servers_fault(monkeypatch, tmp_path):
    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "npx",
        'echo "npm ERR! code ENOTFOUND" >&2; echo "npm ERR! network request failed" >&2; exit 1\n')
    with pytest.raises(worker.ConverterUnavailable, match="npx could not run"):
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))


def test_npx_reporting_its_childs_signal_death_is_the_servers_fault(monkeypatch, tmp_path):
    """Under npx the converter is a grandchild: an out-of-memory kill reaches
    us as npx exiting 137 ("Killed"), not as a negative return code."""
    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "npx", 'echo "Killed" >&2; exit 137\n')
    with pytest.raises(worker.ConverterUnavailable, match="killed by signal 9"):
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))


def _gone(pid: int, wait: float = 1.0) -> bool:
    import os
    import time

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_stuck_converter_under_npx_is_killed_with_its_children(monkeypatch, tmp_path):
    """npx runs the converter as a grandchild that holds the output pipe.
    Killing npx alone left it running, and the request then waited for it
    (review r2: 8 s instead of 1 s with an 8 s child; forever with a stuck one)."""
    import time

    pid_file = tmp_path / "child.pid"
    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "npx",
        f"sh -c 'sleep 30 & echo $! > \"{pid_file}\"; wait'\n")
    monkeypatch.setattr(worker, "CONVERT_TIMEOUT_S", 1)

    started = time.monotonic()
    with pytest.raises(worker.ConverterUnavailable, match="within 1 s"):
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"the request waited {elapsed:.1f} s for an orphaned converter"
    assert _gone(int(pid_file.read_text())), "the converter's child is still running"


def test_a_stuck_converter_is_killed_and_reported_as_the_servers_fault(monkeypatch, tmp_path):
    """wait_for cancels the read, not the process: the converter used to
    outlive the request (and escape both handlers as a bare TimeoutError)."""
    pid_file = tmp_path / "pid"
    worker, src, work = _converter_on_path(
        monkeypatch, tmp_path, "splat-transform", f'echo $$ > "{pid_file}"; exec sleep 30\n')
    monkeypatch.setattr(worker, "CONVERT_TIMEOUT_S", 1)

    with pytest.raises(worker.ConverterUnavailable, match="within 1 s"):
        asyncio.run(worker._convert_to_delivery(src, work, "m1"))

    assert _gone(int(pid_file.read_text())), "the converter process is still running"


def test_no_storage_backend_refuses_before_converting(monkeypatch):
    conn, _, _ = _patch(monkeypatch, storage=False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.upload_property_scan(
            lead_id=LEAD_ID, listing_id=None, file=_upload(b"NGSP\x04\x00\x00\x00"),
            capture_app="scaniverse", attested=True, ctx=CTX,
        ))

    assert exc.value.status_code == 503
    assert conn.inserted == []
