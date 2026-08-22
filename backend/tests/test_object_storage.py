"""Durable object storage across the three backends.

Video upload used to hard-require RECON_S3_BUCKET, so it returned 503 forever on
an Azure deployment. These cover the Azure Files backend that replaces it (the
one that actually runs in production), the traversal guard on its keys, and the
boto3-shaped adapter the contract vault is injected with.
"""

from __future__ import annotations

import importlib

import pytest

import object_storage


def _reload(monkeypatch, **env):
    for key in (
        "ORACLE_STORAGE_BACKEND",
        "ORACLE_MEDIA_ROOT",
        "ORACLE_BLOB_ACCOUNT_URL",
        "ORACLE_BLOB_CONTAINER",
        "AZURE_STORAGE_CONNECTION_STRING",
        "RECON_S3_BUCKET",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return importlib.reload(object_storage)


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    importlib.reload(object_storage)


@pytest.fixture
def files_backend(monkeypatch, tmp_path):
    return _reload(monkeypatch, ORACLE_MEDIA_ROOT=tmp_path)


# --- backend selection --------------------------------------------------------

def test_defaults_to_the_azure_files_mount(monkeypatch):
    mod = _reload(monkeypatch)

    assert mod.BACKEND == "azure-files"
    assert str(mod.MEDIA_ROOT) == "/mnt/neoh"


def test_unknown_backend_fails_loudly(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="gcs")

    assert mod.is_configured() is False
    with pytest.raises(mod.StorageError, match="ORACLE_STORAGE_BACKEND"):
        mod.put_bytes("k", b"x")


@pytest.mark.parametrize(
    "env, expected",
    [
        # azure-files with the default /mnt/neoh, which is not a real mount in
        # a test environment. "Configured" means a write would actually land,
        # so an unmounted default must answer False — see the writable-root case
        # below for the positive path.
        ({}, False),
        ({"ORACLE_STORAGE_BACKEND": "s3"}, False),                   # no bucket
        ({"ORACLE_STORAGE_BACKEND": "s3", "RECON_S3_BUCKET": "b"}, True),
        ({"ORACLE_STORAGE_BACKEND": "azure-blob"}, False),           # no account
        (
            {
                "ORACLE_STORAGE_BACKEND": "azure-blob",
                "ORACLE_BLOB_ACCOUNT_URL": "https://a.blob.core.windows.net",
            },
            True,
        ),
    ],
)
def test_is_configured_reports_whether_a_write_could_succeed(monkeypatch, env, expected):
    assert _reload(monkeypatch, **env).is_configured() is expected


def test_a_real_writable_mount_is_configured(monkeypatch, tmp_path):
    """The positive azure-files case: a root that exists and accepts writes."""
    assert _reload(monkeypatch, ORACLE_MEDIA_ROOT=tmp_path).is_configured() is True


# --- the azure files backend --------------------------------------------------

def test_put_and_get_round_trip(files_backend, tmp_path):
    files_backend.put_bytes("property-view/tenant-a/clip", b"video-bytes", "video/mp4")

    assert (tmp_path / "property-view/tenant-a/clip").read_bytes() == b"video-bytes"
    assert files_backend.get_bytes("property-view/tenant-a/clip") == b"video-bytes"


def test_put_bytes_returns_the_key_for_persisting(files_backend):
    assert files_backend.put_bytes("a/b", b"x") == "a/b"


def test_put_file_streams_an_existing_file(files_backend, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"on-disk")

    files_backend.put_file("clips/one", source, "video/mp4")

    assert files_backend.get_bytes("clips/one") == b"on-disk"


def test_writes_are_atomic(files_backend, tmp_path):
    """Replicas share the mount, so a reader must never see a partial file."""
    files_backend.put_bytes("clips/two", b"complete")

    leftovers = list(tmp_path.rglob("*.partial"))
    assert leftovers == []


def test_overwrite_replaces_cleanly(files_backend):
    files_backend.put_bytes("clips/three", b"first")
    files_backend.put_bytes("clips/three", b"second")

    assert files_backend.get_bytes("clips/three") == b"second"


@pytest.mark.parametrize("key", ["../escape", "a/../../escape", "/etc/passwd"])
def test_keys_cannot_escape_the_media_root(files_backend, key):
    with pytest.raises(files_backend.StorageError, match="escapes"):
        files_backend.put_bytes(key, b"x")


def test_a_mounted_share_has_no_expiring_link(files_backend):
    """Returning None is the contract: these bytes are served through the app's
    authenticated media endpoint, not handed out as a URL."""
    assert files_backend.signed_url("clips/four") is None


# --- the boto3-compatible adapter ---------------------------------------------

def test_adapter_uploads_through_the_configured_backend(files_backend, tmp_path):
    source = tmp_path / "contract.pdf"
    source.write_bytes(b"%PDF-1.7")

    store = files_backend.BotoCompatibleStore()
    store.upload_file(str(source), "ignored-bucket", "vault/tenant/doc.pdf",
                      ExtraArgs={"ContentType": "application/pdf"})

    assert files_backend.get_bytes("vault/tenant/doc.pdf") == b"%PDF-1.7"


def test_adapter_refuses_to_presign_on_a_mount(files_backend):
    """The vault hands out expiring links, so it must fail loudly rather than
    return something unusable when pointed at the file share."""
    store = files_backend.BotoCompatibleStore()

    with pytest.raises(files_backend.StorageError, match="expiring links"):
        store.generate_presigned_url("get_object", Params={"Key": "vault/doc.pdf"})


def test_adapter_rejects_unsupported_operations(files_backend):
    store = files_backend.BotoCompatibleStore()

    with pytest.raises(files_backend.StorageError, match="presign operation"):
        store.generate_presigned_url("put_object", Params={"Key": "k"})


def test_adapter_normalises_transfer_failures_to_oserror(files_backend, tmp_path):
    store = files_backend.BotoCompatibleStore()

    with pytest.raises(OSError):
        store.upload_file(str(tmp_path / "missing.pdf"), "b", "vault/doc.pdf")
