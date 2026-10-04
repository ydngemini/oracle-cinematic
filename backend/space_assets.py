"""space_assets.py — protected delivery and deletion of Neoh Space assets.

**Delivery.** A finished space is somebody's home. It is stored under
`splats/{tenant}/…` and was only ever reachable through the authenticated
`/api/media/{id}` route, which forces the browser to download the whole file
into memory (to attach the JWT) before the viewer can start — no streaming,
no real progress, no HTTP cache. A signed URL fixes that without making the
asset public:

* scoped to ONE media id, ONE tenant and the object key's suffix;
* short-lived (SPACE_ASSET_URL_TTL, default 10 min) — long enough to load,
  useless as a permanent link;
* HMAC-SHA256 under a key derived from the app secret, compared in constant
  time;
* served with Range support and `Cache-Control: private`, never listing a
  bucket or exposing a storage URL.

Minted only by an authenticated, RLS-scoped read of the media row (the tour
resolver), so possession of a URL proves an authorised caller asked for it in
the last few minutes.

**Deletion.** Deleting a space removes the delivered asset and every derived
companion (poses, point cloud, scene manifest, provenance manifest, preserved
raw output). Original photos and video are NOT touched here: they are the
customer's source media and are deleted only through the media/privacy paths
that own them (docs/neoh-space.md §Privacy).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Iterable, Optional

log = logging.getLogger("oracle.space_assets")

#: Seconds a signed asset URL stays valid.
URL_TTL = max(60, int(os.environ.get("SPACE_ASSET_URL_TTL", "600") or 600))
#: Every object stored beside a delivered space, by suffix of its key.
COMPANION_SUFFIXES = (".cameras.json", ".points.ply", ".scene.json")


def _key() -> bytes:
    explicit = os.environ.get("SPACE_ASSET_SIGNING_KEY", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    from auth import SECRET_KEY

    # Derived, not reused: a leaked asset signature must not double as a way
    # to forge anything else signed with the app secret.
    return hmac.new(SECRET_KEY.encode("utf-8"), b"neoh-space-asset-v1", hashlib.sha256).digest()


def _payload(media_id: str, tenant_id: str, expires: int) -> bytes:
    return f"{media_id}:{tenant_id}:{expires}".encode("ascii")


def sign(media_id: str, tenant_id: str, *, now: Optional[float] = None,
         ttl: Optional[int] = None) -> tuple[int, str]:
    expires = int((now if now is not None else time.time()) + (ttl or URL_TTL))
    mac = hmac.new(_key(), _payload(str(media_id), str(tenant_id), expires), hashlib.sha256).digest()
    return expires, base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def signed_path(media_id: str, tenant_id: str, **kw) -> str:
    expires, sig = sign(media_id, tenant_id, **kw)
    return f"/api/space/assets/{media_id}?t={tenant_id}&exp={expires}&sig={sig}"


def verify(media_id: str, tenant_id: str, expires: int, sig: str, *,
           now: Optional[float] = None) -> bool:
    if not sig or not tenant_id:
        return False
    if int(expires) < int(now if now is not None else time.time()):
        return False
    # A link valid for longer than the TTL was not minted by this service.
    if int(expires) - int(now if now is not None else time.time()) > URL_TTL + 5:
        return False
    expected = hmac.new(_key(), _payload(str(media_id), str(tenant_id), int(expires)),
                        hashlib.sha256).digest()
    supplied = sig + "=" * (-len(sig) % 4)
    try:
        given = base64.urlsafe_b64decode(supplied.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(expected, given)


def parse_range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """(start, end_inclusive) for a single `bytes=` range, or None for whole.

    Raises ValueError for an unsatisfiable range (caller answers 416).
    Multi-range requests are answered with the whole body, which RFC 9110
    permits.
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    spec = header[len("bytes="):].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s == "":
        # suffix range: last N bytes
        n = int(end_s)
        if n <= 0:
            raise ValueError("empty suffix range")
        return max(0, size - n), size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    if start >= size or start < 0 or end < start:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1)


def read_range(key: str, rng: Optional[tuple[int, int]]) -> tuple[bytes, int]:
    """Bytes of `key` (or of the range), plus the object's full size.

    Filesystem backends seek; object stores fetch the object (bounded by the
    delivery size cap) and slice — correct everywhere, optimal on disk.
    """
    import object_storage

    backend = object_storage._check_backend()
    if backend in getattr(object_storage, "_FILESYSTEM_BACKENDS", ()):
        path = object_storage._safe_destination(key)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if rng is None:
                return handle.read(), size
            handle.seek(rng[0])
            return handle.read(rng[1] - rng[0] + 1), size
    data = object_storage.get_bytes(key)
    if rng is None:
        return data, len(data)
    return data[rng[0]:rng[1] + 1], len(data)


def keys_for_space(s3_key: Optional[str], raw_keys: Iterable[Optional[str]] = ()) -> list[str]:
    """Every stored object that belongs to one delivered space."""
    keys: list[str] = []
    if s3_key:
        keys.append(s3_key)
        keys.extend(s3_key + suffix for suffix in COMPANION_SUFFIXES)
        stem = s3_key.rsplit(".", 1)[0]
        keys.append(stem + ".json")          # provenance / AI-disclosure manifest
    for raw in raw_keys:
        if raw:
            keys.append(raw)
            keys.extend(raw + suffix for suffix in (".cameras.json", ".points.ply"))
    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def delete_objects(keys: Iterable[str]) -> dict[str, int]:
    """Delete stored objects. Missing objects count as deleted (idempotent)."""
    import object_storage

    keys = list(keys)
    deleted = failed = 0
    if not object_storage.is_configured():
        return {"deleted": 0, "failed": len(keys)}
    for key in keys:
        try:
            object_storage.delete_object(key)
            deleted += 1
        except FileNotFoundError:
            deleted += 1
        except Exception:  # noqa: BLE001 — report, never half-raise mid-loop
            log.exception("Could not delete space object %s", key)
            failed += 1
    return {"deleted": deleted, "failed": failed}
