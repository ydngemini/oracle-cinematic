"""Media bytes belong in object storage, and both shapes stay readable.

Storing full-size images in `media_blobs.bytes` made the primary database the
image server: every thumbnail render was a row read of the whole file competing
for the same connection pool, and every byte rode along in backups and
replication. It is the first thing to fall over under load, and it falls over as
a database outage rather than a slow image.

Two properties are load-bearing and neither can regress:

  * when storage is configured, new bytes go there and never into a blob;
  * rows written before it was configured must keep working forever, because
    nothing migrates them.
"""

from __future__ import annotations

import asyncio

import pytest

import media_storage


class _FakeStorage:
    """Stands in for object_storage, recording what it was asked to keep."""

    def __init__(self, configured=True):
        self.configured = configured
        self.objects: dict[str, bytes] = {}
        self.StorageError = RuntimeError

    def is_configured(self):
        return self.configured

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data
        return key

    def get_bytes(self, key):
        if key not in self.objects:
            raise self.StorageError(f"missing {key}")
        return self.objects[key]


@pytest.fixture
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setitem(__import__("sys").modules, "object_storage", fake)
    return fake


def test_configured_storage_takes_the_bytes(storage):
    key = asyncio.run(
        media_storage.put_media_bytes(b"image-bytes", "image/png", "tenant-1", kind="photo")
    )

    assert key is not None, "a configured backend must be used, not bypassed"
    assert storage.objects[key] == b"image-bytes"
    assert "tenant-1" in key, "keys carry the tenant for operator legibility"


def test_keys_are_unguessable(storage):
    """The key is not a secret — every read is authenticated — but two uploads
    of the same file must not collide, and a key must not encode the filename."""
    first = asyncio.run(media_storage.put_media_bytes(b"same", "image/png", "t", kind="photo"))
    second = asyncio.run(media_storage.put_media_bytes(b"same", "image/png", "t", kind="photo"))

    assert first != second


def test_without_storage_the_caller_is_told_to_use_a_blob(storage):
    storage.configured = False

    key = asyncio.run(media_storage.put_media_bytes(b"x", "image/png", "t", kind="photo"))

    assert key is None, "None is the signal to fall back to media_blobs"


def test_a_configured_backend_that_fails_raises_rather_than_silently_using_the_db(storage):
    """The failure mode this guards is subtle and load-dependent.

    Falling back to a blob when storage errors would quietly reintroduce
    database-as-image-server on exactly the deployment that configured storage
    to avoid it — and only under the conditions that made storage fail.
    """
    def explode(*_a, **_k):
        raise RuntimeError("bucket unreachable")

    storage.put_bytes = explode

    with pytest.raises(RuntimeError):
        asyncio.run(media_storage.put_media_bytes(b"x", "image/png", "t", kind="photo"))


def test_blob_backed_rows_still_read(storage):
    """Nothing migrates existing rows, so this path is permanent."""
    row = {"bytes": b"legacy-blob", "s3_key": None}

    assert asyncio.run(media_storage.load_media_bytes(row)) == b"legacy-blob"


def test_storage_backed_rows_read_through_the_backend(storage):
    storage.objects["property-media/t/photo/abc"] = b"stored"
    row = {"bytes": None, "s3_key": "property-media/t/photo/abc"}

    assert asyncio.run(media_storage.load_media_bytes(row)) == b"stored"


def test_a_row_with_neither_reports_missing_rather_than_empty(storage):
    """An empty bytes object would render as a broken image with no explanation."""
    row = {"bytes": None, "s3_key": None}

    assert asyncio.run(media_storage.load_media_bytes(row)) is None


def test_a_blob_wins_when_a_row_somehow_has_both(storage):
    """Belt and braces: the local copy is authoritative and needs no network."""
    storage.objects["k"] = b"remote"
    row = {"bytes": b"local", "s3_key": "k"}

    assert asyncio.run(media_storage.load_media_bytes(row)) == b"local"


# ---------------------------------------------------------------------------
# "Configured" has to mean "a write will actually land"
# ---------------------------------------------------------------------------

def test_a_named_but_unwritable_mount_is_not_configured(tmp_path, monkeypatch):
    """ORACLE_MEDIA_ROOT defaults to /mnt/neoh, which exists on a deployed
    replica and not on a laptop. A name-only check answered "configured"
    everywhere and the first write failed — survivable when this only gated
    video, not once photos route through it.
    """
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "azure-files")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path / "definitely" / "not" / "mounted")
    object_storage.reset_configuration_cache()
    # Make the parent unwritable so mkdir cannot quietly succeed.
    (tmp_path / "definitely").mkdir()
    (tmp_path / "definitely").chmod(0o500)
    try:
        assert object_storage.is_configured() is False
    finally:
        (tmp_path / "definitely").chmod(0o700)
        object_storage.reset_configuration_cache()


def test_a_writable_mount_is_configured(tmp_path, monkeypatch):
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "azure-files")
    monkeypatch.setattr(object_storage, "MEDIA_ROOT", tmp_path / "media")
    object_storage.reset_configuration_cache()
    try:
        assert object_storage.is_configured() is True
        assert (tmp_path / "media").is_dir(), "the probe should create the root"
    finally:
        object_storage.reset_configuration_cache()


def test_is_configured_never_raises(monkeypatch):
    """Callers ask this to decide how to degrade; it must always answer."""
    import object_storage

    monkeypatch.setattr(object_storage, "BACKEND", "not-a-real-backend")
    object_storage.reset_configuration_cache()
    try:
        assert object_storage.is_configured() is False
    finally:
        object_storage.reset_configuration_cache()
