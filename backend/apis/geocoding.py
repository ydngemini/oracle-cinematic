"""
Geocoding API — free address-to-coordinates via Nominatim (OpenStreetMap).

No API key required. Rate limit: 1 req/sec (enforced here).
Returns lat/lng, formatted address, bounding box, and place metadata.

Used by:
  - Tour generator: position properties on a map
  - Deal pipeline: enrich leads with coordinates
  - Spatial agent: verify address validity before reconstruction
"""

import asyncio
import logging
import time
import urllib.parse
import urllib.request
import json
from typing import Optional

import source_status as ss

logger = logging.getLogger("oracle.apis.geocoding")

_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
_USER_AGENT = "Oracle-RealEstate/1.0 (contact@oracle-app.dev)"
_last_request_time = 0.0


async def geocode(address: str) -> Optional[dict]:
    """Forward geocode an address to coordinates.

    Returns: { lat, lng, display_name, boundingbox, place_id, type }, or None
    for a miss AND for a failure — `geocode_checked` tells them apart.
    """
    result, _status = await geocode_checked(address)
    return result


async def geocode_checked(address: str) -> tuple[Optional[dict], dict]:
    """(result or None, CONTRACT C6 source status). None + `checked` is a
    verified "address not found"; None + `timed_out` / `unavailable` means the
    geocoder was not read and the address was NOT checked (audit A02)."""
    global _last_request_time
    now = time.monotonic()
    wait = max(0, 1.0 - (now - _last_request_time))
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_time = time.monotonic()

    params = urllib.parse.urlencode({
        "q": address,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    })
    url = f"{_NOMINATIM_URL}/search?{params}"

    try:
        from data_integrations.cache import cached_external

        async def fetch_upstream() -> dict:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            raw = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=10).read().decode()
            )
            return {"results": json.loads(raw)}

        payload = await cached_external(
            "geocode",
            {"provider": "nominatim", "operation": "search", "address": address.strip()},
            fetch_upstream,
            ttl=90 * 86_400,
        )
        results = payload.get("results")
        if not isinstance(results, list):
            return None, ss.unavailable(ss.BAD_RESPONSE)
        if not results:
            return None, ss.checked(matched=0)

        r = results[0]
        return {
            "lat": float(r["lat"]),
            "lng": float(r["lon"]),
            "display_name": r.get("display_name", ""),
            "boundingbox": r.get("boundingbox"),
            "place_id": r.get("place_id"),
            "type": r.get("type", ""),
            "address_parts": r.get("address", {}),
        }, ss.checked(matched=1)
    except Exception as e:  # noqa: BLE001 — reported as a status, never "not found"
        status, reason = ss.classify(e)
        # Never the address in the log (CONTRACT C5.4).
        logger.warning("Geocode %s (%s): %s", status, reason, type(e).__name__)
        return None, ss.entry(status, reason=reason)


async def reverse_geocode(lat: float, lng: float) -> Optional[dict]:
    """Reverse geocode coordinates to an address."""
    global _last_request_time
    now = time.monotonic()
    wait = max(0, 1.0 - (now - _last_request_time))
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_time = time.monotonic()

    params = urllib.parse.urlencode({
        "lat": lat,
        "lon": lng,
        "format": "json",
        "addressdetails": 1,
    })
    url = f"{_NOMINATIM_URL}/reverse?{params}"

    try:
        from data_integrations.cache import cached_external

        async def fetch_upstream() -> dict:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            raw = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=10).read().decode()
            )
            return {"result": json.loads(raw)}

        payload = await cached_external(
            "geocode",
            {
                "provider": "nominatim",
                "operation": "reverse",
                "lat": round(float(lat), 6),
                "lng": round(float(lng), 6),
            },
            fetch_upstream,
            ttl=90 * 86_400,
        )
        result = payload.get("result") or {}
        if "error" in result:
            return None
        return {
            "lat": float(result["lat"]),
            "lng": float(result["lon"]),
            "display_name": result.get("display_name", ""),
            "address_parts": result.get("address", {}),
        }
    except Exception as e:
        logger.warning("Reverse geocode failed for (%f, %f): %s", lat, lng, e)
        return None
