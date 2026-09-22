"""Durable object storage, independent of which cloud is underneath.

Four backends, chosen by ORACLE_STORAGE_BACKEND:

  local                  A plain directory on this host, ORACLE_MEDIA_ROOT
                         (default ./var/media under the working dir). No SDK, no
                         credentials, no cloud account — the backend that lets
                         the whole stack run offline. Same on-disk write-then-
                         rename semantics as azure-files; bytes are served
                         through the app's own authenticated media endpoint
                         (signed_url returns None) since a local dir has no URL.
  azure-files (default)  Write through the shared Azure Files mount described in
                         infra/azure/README.md. Every backend replica sees the
                         same path, so this needs no SDK and no credentials.
                         Mechanically identical to `local` — the difference is
                         only the default root and the operational expectation
                         that the path is a shared mount.
  azure-blob             Azure Blob Storage, with SAS links for expiring reads.
                         Prefers a user-delegation SAS signed by the managed
                         identity; falls back to an account key only if the
                         connection string carries one.
  s3                     Any S3-compatible object store: AWS S3, or DigitalOcean
                         Spaces (Spaces speaks the S3 API — set
                         ORACLE_S3_ENDPOINT_URL to its regional endpoint, e.g.
                         https://nyc3.digitaloceanspaces.com, and it's the
                         same code path, not a separate backend).

Callers work in opaque keys ("property-view/<tenant>/<random>"). Only
signed_url() cares where the bytes physically live.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("oracle.object_storage")

BACKEND = os.getenv("ORACLE_STORAGE_BACKEND", "azure-files").strip().lower()

# `local` and `azure-files` are both a directory on disk with identical write
# semantics — the only difference is the default root and the operational
# meaning of the path (a local dir vs. a shared mount).
_FILESYSTEM_BACKENDS = ("local", "azure-files")

# azure-files defaults to /mnt/neoh — the Container Apps mount point, which does
# not exist on a laptop. `local` defaults to ./var/media under the working dir
# so a bare checkout can write media with no configuration at all.
_DEFAULT_ROOT = "./var/media" if BACKEND == "local" else "/mnt/neoh"
MEDIA_ROOT = Path(os.getenv("ORACLE_MEDIA_ROOT", _DEFAULT_ROOT))

# azure-blob
BLOB_CONTAINER = os.getenv("ORACLE_BLOB_CONTAINER", "neoh-media")
BLOB_ACCOUNT_URL = os.getenv("ORACLE_BLOB_ACCOUNT_URL", "")
BLOB_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

# s3 (AWS S3, or any S3-compatible store such as DigitalOcean Spaces)
S3_BUCKET = os.getenv("ORACLE_S3_BUCKET") or os.getenv("RECON_S3_BUCKET", "")
S3_REGION = os.getenv("ORACLE_S3_REGION") or os.getenv("AWS_REGION", "us-east-1")
# Unset (None) on real AWS S3 — boto3's default endpoint already resolves the
# bucket's region correctly. Spaces (and any other S3-compatible store) has no
# such default, so it must be given explicitly, e.g.
# https://nyc3.digitaloceanspaces.com.
S3_ENDPOINT_URL = os.getenv("ORACLE_S3_ENDPOINT_URL") or None
# Explicit keys for a store with no IAM-role/instance-metadata credential
# chain (Spaces has none). Falls back to boto3's normal credential
# resolution — including AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY — when unset,
# so an existing AWS S3 deployment using IAM roles needs no change.
S3_ACCESS_KEY_ID = os.getenv("ORACLE_S3_ACCESS_KEY_ID") or None
S3_SECRET_ACCESS_KEY = os.getenv("ORACLE_S3_SECRET_ACCESS_KEY") or None


def _s3_client():
    import boto3  # lazy — only the s3 backend needs the AWS SDK

    # Virtual-hosted-style addressing (bucket.endpoint/key, not
    # endpoint/bucket/key) is DigitalOcean's own documented requirement for
    # Spaces (docs/products/spaces/reference/s3-sdk-examples/) — only forced
    # when a custom endpoint is actually configured, so a real AWS S3
    # deployment keeps boto3's own default behaviour unchanged.
    from botocore.client import Config

    return boto3.client(
        "s3",
        region_name=S3_REGION,
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "virtual"}) if S3_ENDPOINT_URL else None,
    )


_VALID_BACKENDS = ("local", "azure-files", "azure-blob", "s3")


class StorageError(RuntimeError):
    """Raised when a backend is misconfigured or a transfer fails."""


def _check_backend() -> str:
    if BACKEND not in _VALID_BACKENDS:
        raise StorageError(
            f"ORACLE_STORAGE_BACKEND={BACKEND!r} is not supported; use one of "
            + ", ".join(_VALID_BACKENDS)
        )
    return BACKEND


def is_configured() -> bool:
    """Whether durable storage can actually accept a write right now.

    Callers use this to degrade honestly (a 503 that says video is unavailable)
    instead of raising a KeyError from deep inside an upload handler.

    For the mount-backed backend this means the mount is really there and
    really writable — not merely that someone set a path string. ORACLE_MEDIA_ROOT
    defaults to /mnt/neoh, which exists on a deployed replica and does not exist
    on a developer's laptop, so a name-only check answered "yes" everywhere and
    the first write failed. That was survivable while this only gated video
    (which 503s and says so); it is not survivable now that photos route through
    here too.

    Never raises: a caller asking "can I write?" must always get an answer.
    """
    try:
        backend = _check_backend()
    except StorageError:
        return False
    if backend in _FILESYSTEM_BACKENDS:
        return _mount_is_writable()
    if backend == "azure-blob":
        return bool(BLOB_CONNECTION_STRING or BLOB_ACCOUNT_URL)
    return bool(S3_BUCKET)


# Probing the filesystem on every upload would be wasteful, and the answer only
# changes when a mount appears or goes away — a process restart either way.
_mount_writable: Optional[bool] = None


def _mount_is_writable() -> bool:
    global _mount_writable
    if _mount_writable is not None:
        return _mount_writable

    root = str(MEDIA_ROOT).strip()
    if not root:
        _mount_writable = False
        return False
    try:
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        _mount_writable = os.access(MEDIA_ROOT, os.W_OK)
        if not _mount_writable:
            logger.info(
                "Media root %s exists but is not writable; falling back to database blobs.",
                MEDIA_ROOT,
            )
    except OSError as exc:
        logger.info(
            "Media root %s is not usable (%s); falling back to database blobs.",
            MEDIA_ROOT, exc,
        )
        _mount_writable = False
    return _mount_writable


def reset_configuration_cache() -> None:
    """Forget the cached mount probe. For tests, and for a re-check after a
    mount is attached without restarting the process."""
    global _mount_writable
    _mount_writable = None


def _safe_destination(key: str) -> Path:
    """Resolve a key under MEDIA_ROOT, refusing anything that escapes it.

    Keys are server-generated today, but this is the one backend where a
    traversal would hand an attacker the whole filesystem."""
    root = MEDIA_ROOT.resolve()
    destination = (root / key).resolve()
    if destination != root and root not in destination.parents:
        raise StorageError("storage key escapes the media root")
    return destination


# --- writes -------------------------------------------------------------------

def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store `data` at `key`. Returns the key, so call sites can persist it."""
    backend = _check_backend()

    if backend in _FILESYSTEM_BACKENDS:
        destination = _safe_destination(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader on another replica never observes a
        # half-written file, which a plain open().write() would expose.
        staging = destination.with_suffix(destination.suffix + ".partial")
        staging.write_bytes(data)
        staging.replace(destination)
        return key

    if backend == "azure-blob":
        _blob_client(key).upload_blob(
            data,
            overwrite=True,
            content_settings=_content_settings(content_type),
        )
        return key

    if not S3_BUCKET:
        raise StorageError("ORACLE_STORAGE_BACKEND=s3 but ORACLE_S3_BUCKET (or RECON_S3_BUCKET) is unset")
    _s3_client().put_object(
        Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type
    )
    return key


def put_file(key: str, path: str | os.PathLike[str], content_type: str) -> str:
    """Store a file already on disk. Streams rather than reading it into memory."""
    backend = _check_backend()

    if backend in _FILESYSTEM_BACKENDS:
        destination = _safe_destination(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_suffix(destination.suffix + ".partial")
        shutil.copyfile(path, staging)
        staging.replace(destination)
        return key

    if backend == "azure-blob":
        with open(path, "rb") as handle:
            _blob_client(key).upload_blob(
                handle,
                overwrite=True,
                content_settings=_content_settings(content_type),
            )
        return key

    if not S3_BUCKET:
        raise StorageError("ORACLE_STORAGE_BACKEND=s3 but ORACLE_S3_BUCKET (or RECON_S3_BUCKET) is unset")
    _s3_client().upload_file(
        str(path), S3_BUCKET, key, ExtraArgs={"ContentType": content_type}
    )
    return key


def get_bytes(key: str) -> bytes:
    backend = _check_backend()

    if backend in _FILESYSTEM_BACKENDS:
        return _safe_destination(key).read_bytes()
    if backend == "azure-blob":
        return _blob_client(key).download_blob().readall()

    return _s3_client().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


# --- expiring reads -----------------------------------------------------------

def signed_url(key: str, expires_seconds: int = 3600) -> Optional[str]:
    """A time-limited read URL, or None when the backend cannot mint one.

    The Azure Files backend deliberately returns None: a mounted share has no
    public URL, so those bytes must be served through the app's own
    authenticated media endpoint rather than handed out as a link."""
    backend = _check_backend()

    if backend in _FILESYSTEM_BACKENDS:
        return None

    if backend == "azure-blob":
        return _blob_sas_url(key, expires_seconds)

    return _s3_client().generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires_seconds
    )


def presigned_put_url(key: str, expires_seconds: int = 3600) -> Optional[str]:
    """A URL something outside this process can PUT one object to, or None.

    The write-side twin of `signed_url`, and it exists for exactly one
    caller: a rented GPU that has to hand back a reconstruction without
    holding any of our credentials.

    azure-files returns None for the same reason it does on the read side — a
    mounted share has no URL to hand out. A deployment on azure-files therefore
    has no blob transport, which `PodProvider.available()` reports rather than
    discovering mid-job.
    """
    backend = _check_backend()

    if backend in _FILESYSTEM_BACKENDS:
        return None

    if backend == "azure-blob":
        return _blob_sas_url(key, expires_seconds, write=True)

    return _s3_client().generate_presigned_url(
        "put_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires_seconds
    )


# --- azure blob plumbing ------------------------------------------------------

def _content_settings(content_type: str):
    from azure.storage.blob import ContentSettings

    return ContentSettings(content_type=content_type)


def _blob_service():
    from azure.storage.blob import BlobServiceClient

    if BLOB_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
    if not BLOB_ACCOUNT_URL:
        raise StorageError(
            "ORACLE_STORAGE_BACKEND=azure-blob requires ORACLE_BLOB_ACCOUNT_URL "
            "or AZURE_STORAGE_CONNECTION_STRING"
        )
    from azure.identity import DefaultAzureCredential

    return BlobServiceClient(
        account_url=BLOB_ACCOUNT_URL, credential=DefaultAzureCredential()
    )


def _blob_client(key: str):
    return _blob_service().get_blob_client(container=BLOB_CONTAINER, blob=key)


def _blob_sas_url(key: str, expires_seconds: int, *, write: bool = False) -> str:
    """A time-limited URL for one blob.

    `write=True` grants create+write on that single blob and nothing else — no
    read, no list, no delete, and no reach beyond the exact key. That matters
    because a write SAS is handed to a rented GPU we do not control: the pod
    must be able to deposit its result and must not be able to enumerate or
    overwrite anything else in the container.
    """
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    service = _blob_service()
    start = _dt.datetime.now(_dt.timezone.utc)
    expiry = start + _dt.timedelta(seconds=expires_seconds)

    permission = (
        BlobSasPermissions(create=True, write=True) if write
        else BlobSasPermissions(read=True)
    )
    kwargs: dict[str, Any] = {
        "account_name": service.account_name,
        "container_name": BLOB_CONTAINER,
        "blob_name": key,
        "permission": permission,
        "expiry": expiry,
    }
    if service.credential is not None and getattr(service.credential, "account_key", None):
        kwargs["account_key"] = service.credential.account_key
    else:
        # Managed identity: no account key exists, so the SAS is signed with a
        # short-lived user-delegation key instead. This is the preferred shape —
        # it inherits the identity's RBAC and can be revoked centrally.
        kwargs["user_delegation_key"] = service.get_user_delegation_key(start, expiry)

    token = generate_blob_sas(**kwargs)
    return f"{service.url.rstrip('/')}/{BLOB_CONTAINER}/{key}?{token}"


# --- boto3-compatible adapter -------------------------------------------------

class BotoCompatibleStore:
    """Exposes the two boto3 S3 methods contract_vault calls.

    contract_vault takes an injected `s3_client`, which is a good seam — this
    adapter lets the vault target Azure without touching its validation, key
    naming or error handling. Failures surface as OSError because that is what
    the vault already treats as a transfer failure.
    """

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803
        extra = ExtraArgs or {}
        try:
            put_file(key, filename, extra.get("ContentType", "application/octet-stream"))
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise SDK errors for the caller
            raise OSError(f"object storage upload failed: {exc}") from exc

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=3600):  # noqa: N803
        if operation != "get_object":
            raise StorageError(f"unsupported presign operation {operation!r}")
        key = (Params or {}).get("Key", "")
        try:
            url = signed_url(key, ExpiresIn)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OSError(f"object storage presign failed: {exc}") from exc
        if url is None:
            raise StorageError(
                f"ORACLE_STORAGE_BACKEND={BACKEND} cannot mint expiring links; "
                "use azure-blob for the contract vault"
            )
        return url
