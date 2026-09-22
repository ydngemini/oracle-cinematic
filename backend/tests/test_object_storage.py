"""Durable object storage across the four backends.

Video upload used to hard-require RECON_S3_BUCKET, so it returned 503 forever on
an Azure deployment. These cover the Azure Files backend that replaces it (the
one that actually runs in production), the `local` directory backend that lets
the stack run with no cloud account, the traversal guard on their keys, and the
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
        "ORACLE_S3_BUCKET",
        "ORACLE_S3_REGION",
        "ORACLE_S3_ENDPOINT_URL",
        "ORACLE_S3_ACCESS_KEY_ID",
        "ORACLE_S3_SECRET_ACCESS_KEY",
        "AWS_REGION",
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


# --- s3-compatible: AWS S3 or DigitalOcean Spaces ----------------------------

def test_s3_client_targets_real_aws_by_default(monkeypatch):
    """No endpoint override, no explicit keys: boto3's own default AWS
    endpoint and credential chain apply, unchanged from before Spaces support
    existed — an existing AWS deployment needs no new configuration."""
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="s3", RECON_S3_BUCKET="b")
    assert mod.S3_ENDPOINT_URL is None
    assert mod.S3_ACCESS_KEY_ID is None
    assert mod.S3_SECRET_ACCESS_KEY is None

    calls = []

    class _FakeBoto3:
        @staticmethod
        def client(service, **kwargs):
            calls.append((service, kwargs))
            return object()

    import sys
    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    mod._s3_client()

    # No forced addressing_style against real AWS S3 — boto3's own default
    # applies, exactly as it did before Spaces support existed.
    assert calls[0][1]["config"] is None


def test_s3_client_points_at_a_spaces_endpoint_when_configured(monkeypatch):
    mod = _reload(
        monkeypatch,
        ORACLE_STORAGE_BACKEND="s3",
        ORACLE_S3_BUCKET="neoh-media",
        ORACLE_S3_REGION="nyc3",
        ORACLE_S3_ENDPOINT_URL="https://nyc3.digitaloceanspaces.com",
        ORACLE_S3_ACCESS_KEY_ID="spaces-key",
        ORACLE_S3_SECRET_ACCESS_KEY="spaces-secret",
    )
    assert mod.S3_BUCKET == "neoh-media"
    assert mod.is_configured() is True

    calls = []

    class _FakeBoto3:
        @staticmethod
        def client(service, **kwargs):
            calls.append((service, kwargs))
            return object()

    import sys
    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)

    mod._s3_client()

    assert len(calls) == 1
    service, kwargs = calls[0]
    config = kwargs.pop("config")
    assert service == "s3"
    assert kwargs == {
        "region_name": "nyc3",
        "endpoint_url": "https://nyc3.digitaloceanspaces.com",
        "aws_access_key_id": "spaces-key",
        "aws_secret_access_key": "spaces-secret",
    }
    # DigitalOcean's own documented requirement for Spaces: virtual-hosted-
    # style addressing (bucket.endpoint/key), not path-style.
    assert config.s3["addressing_style"] == "virtual"


def test_a_bare_recon_s3_bucket_still_works_unaided(monkeypatch):
    """ORACLE_S3_BUCKET is preferred but RECON_S3_BUCKET (the pre-existing
    recon-pipeline var) still selects the bucket on its own — no forced
    rename for an existing deployment."""
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="s3", RECON_S3_BUCKET="recon-only")
    assert mod.S3_BUCKET == "recon-only"


# --- the local directory backend ---------------------------------------------

def test_local_backend_defaults_to_a_relative_dir_no_cloud(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="local")

    assert mod.BACKEND == "local"
    assert str(mod.MEDIA_ROOT) == "var/media"  # ./var/media, Path-normalised


def test_local_backend_creates_its_root_and_is_configured(monkeypatch, tmp_path):
    mod = _reload(
        monkeypatch,
        ORACLE_STORAGE_BACKEND="local",
        ORACLE_MEDIA_ROOT=tmp_path / "media",
    )
    assert mod.is_configured() is True
    assert (tmp_path / "media").is_dir()


def test_local_backend_round_trips_and_serves_through_the_app(monkeypatch, tmp_path):
    mod = _reload(
        monkeypatch,
        ORACLE_STORAGE_BACKEND="local",
        ORACLE_MEDIA_ROOT=tmp_path,
    )
    mod.put_bytes("property-media/t/pic", b"jpeg-bytes", "image/jpeg")

    assert (tmp_path / "property-media/t/pic").read_bytes() == b"jpeg-bytes"
    assert mod.get_bytes("property-media/t/pic") == b"jpeg-bytes"
    # A local directory has no public URL — bytes go through the media endpoint.
    assert mod.signed_url("property-media/t/pic") is None
    assert mod.presigned_put_url("property-media/t/pic") is None


@pytest.mark.parametrize("key", ["../escape", "a/../../escape", "/etc/passwd"])
def test_local_backend_keys_cannot_escape_the_root(monkeypatch, tmp_path, key):
    mod = _reload(
        monkeypatch, ORACLE_STORAGE_BACKEND="local", ORACLE_MEDIA_ROOT=tmp_path
    )
    with pytest.raises(mod.StorageError, match="escapes"):
        mod.put_bytes(key, b"x")


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


# --- write capabilities handed to machines we do not control ------------------
#
# PodProvider's blob transport hands a rented RunPod GPU a URL so it can deposit
# a finished reconstruction. That URL is the *entire* capability of a machine we
# never talk to again and do not control, so what it grants is a security
# boundary rather than a detail.

def _sas_kwargs(monkeypatch, mod, *, write):
    """Capture what would be signed, without an Azure account."""
    captured: dict = {}

    class _Perm:
        def __init__(self, **flags):
            captured["permission"] = flags

    class _Service:
        account_name = "acct"
        url = "https://acct.blob.core.windows.net"

        class credential:  # noqa: N801 - stands in for a shared-key credential
            account_key = "k"

    def _generate(**kwargs):
        captured.update({k: v for k, v in kwargs.items() if k != "permission"})
        return "sig=stub"

    fake = type("m", (), {"BlobSasPermissions": _Perm, "generate_blob_sas": _generate})
    monkeypatch.setitem(__import__("sys").modules, "azure.storage.blob", fake)
    monkeypatch.setattr(mod, "_blob_service", lambda: _Service())
    mod._blob_sas_url("recon-outputs/job/model.sog", 3600, write=write)
    return captured


def test_a_write_url_grants_only_create_and_write(monkeypatch):
    """No read, no list, no delete. The pod must be able to deposit its result
    and must NOT be able to enumerate the container, read other tenants'
    reconstructions, or overwrite anything it was not given."""
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="azure-blob",
                  ORACLE_BLOB_ACCOUNT_URL="https://acct.blob.core.windows.net")
    granted = _sas_kwargs(monkeypatch, mod, write=True)["permission"]

    assert granted.get("write") is True
    assert granted.get("create") is True
    for forbidden in ("read", "list", "delete", "add", "update"):
        assert not granted.get(forbidden), (
            f"a write URL handed to a rented GPU must not grant {forbidden}"
        )


def test_a_read_url_grants_no_write(monkeypatch):
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="azure-blob",
                  ORACLE_BLOB_ACCOUNT_URL="https://acct.blob.core.windows.net")
    granted = _sas_kwargs(monkeypatch, mod, write=False)["permission"]

    assert granted.get("read") is True
    for forbidden in ("write", "create", "delete", "list"):
        assert not granted.get(forbidden)


def test_a_write_url_is_scoped_to_one_blob(monkeypatch):
    """Scoped to the exact key, not a prefix or the container."""
    mod = _reload(monkeypatch, ORACLE_STORAGE_BACKEND="azure-blob",
                  ORACLE_BLOB_ACCOUNT_URL="https://acct.blob.core.windows.net")
    captured = _sas_kwargs(monkeypatch, mod, write=True)

    assert captured["blob_name"] == "recon-outputs/job/model.sog"


def test_a_mount_cannot_hand_out_a_write_url(files_backend):
    """azure-files returns None rather than something unusable, so the caller
    can refuse the job up front instead of computing a result with nowhere to
    put it."""
    assert files_backend.presigned_put_url("recon-outputs/job/model.sog") is None
