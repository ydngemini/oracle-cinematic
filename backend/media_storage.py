"""Where property media bytes actually live.

Historically every photo went into `media_blobs.bytes` — a bytea column in the
primary database. That makes Postgres the image server: each thumbnail view is
a row read of the full file over the DB connection pool, competing with every
CRM query for the same ten connections, and every byte is carried in backups
and replication. It is the first thing that falls over as usage grows, and it
falls over as a *database* outage rather than a slow image.

Video already avoided this (0066 forbids a video row from carrying a blob at
all). This module generalises that: when object storage is configured, bytes go
there and `property_media.s3_key` points at them; otherwise they fall back to a
blob so a bare `docker compose up` still works with no cloud account.

Both shapes must stay readable forever — existing deployments hold blob-backed
rows, and nothing here migrates them. `load_media_bytes` is the single reader
that understands both, so a caller cannot accidentally support only one.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Optional

logger = logging.getLogger("oracle.media_storage")

# Keys are opaque and unguessable. The tenant prefix is for operator legibility
# when browsing a bucket, not for access control — every read still goes through
# an authenticated route and RLS on property_media.
_KEY_PREFIX = "property-media"


def storage_available() -> bool:
    """True when durable object storage is configured for this deployment."""
    try:
        import object_storage

        return object_storage.is_configured()
    except Exception:  # noqa: BLE001 — an unimportable backend is an unavailable one
        return False


async def put_media_bytes(
    data: bytes, content_type: str, tenant_id: str, *, kind: str = "photo"
) -> Optional[str]:
    """Store bytes durably and return the key, or None to fall back to a blob.

    Returns None only when object storage is not configured. A configured
    backend that *fails* raises, because silently writing a 25 MB photo into the
    database after the operator deliberately configured storage would reintroduce
    the exact problem this module exists to remove — quietly, and only under load.
    """
    if not storage_available():
        return None

    import object_storage

    key = f"{_KEY_PREFIX}/{tenant_id}/{kind}/{secrets.token_hex(16)}"
    # put_bytes is synchronous and does real network or disk I/O.
    return await asyncio.to_thread(object_storage.put_bytes, key, data, content_type)


async def load_media_bytes(row: Any) -> Optional[bytes]:
    """Bytes for a media row that may be blob-backed or storage-backed.

    `row` needs `bytes` (from a LEFT JOIN on media_blobs) and `s3_key`. Returns
    None when neither holds anything, which callers should treat as "this media
    is gone", not as an empty file.
    """
    blob = row["bytes"] if "bytes" in row else None
    if blob is not None:
        return bytes(blob)

    key = row["s3_key"] if "s3_key" in row else None
    if not key:
        return None

    import object_storage

    try:
        return await asyncio.to_thread(object_storage.get_bytes, key)
    except object_storage.StorageError as exc:
        logger.warning("Object-storage media unreadable (key=%s): %s", key, exc)
        raise


# The SELECT list every reader of media bytes needs. Kept here so a new caller
# cannot write an inner JOIN on media_blobs and silently skip every
# storage-backed row — which is exactly how the reconstruction worker came to
# ignore uploads it should have used.
MEDIA_BYTES_SELECT = """
           pm.id, pm.kind, pm.s3_key,
           COALESCE(pm.content_type, mb.content_type) AS content_type,
           mb.bytes
      FROM property_media AS pm
      LEFT JOIN media_blobs AS mb ON mb.media_id = pm.id
"""
