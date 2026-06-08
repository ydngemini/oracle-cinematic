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

logger = logging.getLogger("oracle.apis.geocoding")

_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
_USER_AGENT = "Oracle-RealEstate/1.0 (contact@oracle-app.dev)"
_last_request_time = 0.0


async def geocode(address: str) -> Optional[dict]:
    """Forward geocode an address to coordinates.

    Returns: { lat, lng, display_name, boundingbox, place_id, type }
    """
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
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read().decode()
        )
        results = json.loads(raw)
        if not results:
            return None

        r = results[0]
        return {
            "lat": float(r["lat"]),
            "lng": float(r["lon"]),
            "display_name": r.get("display_name", ""),
            "boundingbox": r.get("boundingbox"),
            "place_id": r.get("place_id"),
            "type": r.get("type", ""),
            "address_parts": r.get("address", {}),
        }
    except Exception as e:
        logger.warning("Geocode failed for '%s': %s", address, e)
        return None


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
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        raw = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read().decode()
        )
        result = json.loads(raw)
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
