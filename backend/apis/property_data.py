"""
Open Property Data APIs — free public record enrichment.

Sources (no API key required):
  1. OpenStreetMap Overpass — building footprints, POIs, schools, transit
  2. FEMA Flood Map — flood zone status for any coordinate
  3. EPA Superfund — environmental risk within radius

Sources (free key):
  4. US Census TIGERweb — parcel boundaries and FIPS lookups
  5. Walkscore (free tier: 5k/day) — walkability, transit, bike scores

Used by:
  - Deal pipeline: risk signals (flood, contamination)
  - Underwriter: location quality context
  - Tour HUD: neighborhood feature chips

Every source reports a CONTRACT C6 status (`source_status.py`): an empty or
null field next to a `timed_out` / `unavailable` source means "not checked",
never "none" (audit A02, ledger L11-03). The `_checked` variants return
(value, status); the original functions keep their return shapes.
"""

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Optional

import source_status as ss

logger = logging.getLogger("oracle.apis.property_data")

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_FEMA_URL = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
_WALKSCORE_KEY = os.environ.get("WALKSCORE_API_KEY", "")


def _overpass_failure(result: Any) -> Optional[ss.SourceFailure]:
    """Overpass answers HTTP 200 with a `remark` when the query itself failed
    (server-side timeout, out of memory). Its `elements` are then empty or
    partial, which is not a count of zero."""
    remark = str(result.get("remark") or "") if isinstance(result, dict) else ""
    lowered = remark.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return ss.SourceFailure(ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT)
    if "error" in lowered or "out of memory" in lowered:
        return ss.SourceFailure(ss.UNAVAILABLE, ss.UPSTREAM_ERROR)
    return None


async def get_nearby_pois(lat: float, lng: float, radius_m: int = 1000) -> dict:
    """Query OpenStreetMap Overpass for nearby points of interest.

    Returns categorized POI counts within radius. On failure `total_pois` is
    None (it used to be 0) and `error` is a stable reason code.
    """
    result, _status = await _nearby_pois_checked(lat, lng, radius_m)
    return result


async def _nearby_pois_checked(lat: float, lng: float, radius_m: int = 1000) -> tuple[dict, dict]:
    query = f"""
    [out:json][timeout:10];
    (
      node["amenity"="school"](around:{radius_m},{lat},{lng});
      node["amenity"="hospital"](around:{radius_m},{lat},{lng});
      node["amenity"="restaurant"](around:{radius_m},{lat},{lng});
      node["shop"](around:{radius_m},{lat},{lng});
      node["amenity"="pharmacy"](around:{radius_m},{lat},{lng});
      node["public_transport"="station"](around:{radius_m},{lat},{lng});
      node["leisure"="park"](around:{radius_m},{lat},{lng});
    );
    out count;
    """

    try:
        from data_integrations.cache import cached_external

        async def fetch_upstream() -> dict:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(_OVERPASS_URL, data=data, method="POST")
            raw = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=15).read().decode()
            )
            parsed = json.loads(raw)
            problem = _overpass_failure(parsed)
            if problem is not None:
                raise problem  # not cached: a failed query is not a zero count
            return {"result": parsed}

        payload = await cached_external(
            "state_gis",
            {
                "provider": "openstreetmap_overpass",
                "lat": round(float(lat), 5),
                "lng": round(float(lng), 5),
                "radius_m": radius_m,
            },
            fetch_upstream,
            ttl=7 * 86_400,
        )
        result = payload.get("result") or {}
        elements = result.get("elements") or []
        if not elements:
            # `out count` always returns one count element; none at all is a
            # malformed answer, not zero POIs.
            raise ss.SourceFailure(ss.UNAVAILABLE, ss.BAD_RESPONSE)
        total = elements[0].get("tags", {}).get("total", 0)
        count = int(total) if total else 0
        return {
            "radius_m": radius_m,
            "total_pois": count,
            "source": "openstreetmap",
            "lookup_status": ss.CHECKED,
        }, ss.checked(matched=count)
    except Exception as e:  # noqa: BLE001 — reported as a status, never as 0
        status, reason = ss.classify(e)
        logger.warning("Overpass POI query %s (%s): %s", status, reason, type(e).__name__)
        return {
            "radius_m": radius_m,
            "total_pois": None,
            "error": reason,
            "source": "openstreetmap",
            "lookup_status": status,
            "lookup_reason": reason,
        }, ss.entry(status, reason=reason)


async def get_flood_zone(lat: float, lng: float) -> Optional[dict]:
    """Check FEMA flood zone status for coordinates.

    Returns zone designation (A, AE, X, etc.) and risk level, or None when the
    NFHL service could not be read. `_flood_zone_checked` says why.
    """
    result, _status = await _flood_zone_checked(lat, lng)
    return result


async def _flood_zone_checked(lat: float, lng: float) -> tuple[Optional[dict], dict]:
    params = urllib.parse.urlencode({
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
        "f": "json",
    })
    url = f"{_FEMA_URL}?{params}"

    try:
        from data_integrations.cache import cached_external

        async def fetch_upstream() -> dict:
            req = urllib.request.Request(url)
            raw = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=10).read().decode()
            )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or parsed.get("error"):
                # ArcGIS reports a failed query as HTTP 200 + {"error": ...}
                # with no features. Read as "no NFHL polygon" it was cached for
                # 30 days as "this location is unmapped".
                raise ss.SourceFailure(ss.UNAVAILABLE, ss.UPSTREAM_ERROR)
            return {"result": parsed}

        payload = await cached_external(
            "fema_flood",
            {"lat": round(float(lat), 5), "lng": round(float(lng), 5)},
            fetch_upstream,
            ttl=30 * 86_400,
        )
        result = payload.get("result") or {}
        features = result.get("features", [])
        if not features:
            # Zone X is itself a mapped NFHL polygon, so "no intersecting
            # feature" does not mean "surveyed and found low-risk" — it means
            # this coordinate falls outside NFHL coverage entirely. Reporting X
            # here asserted minimal flood hazard for every unmapped location in
            # the country.
            return {
                "zone": "UNKNOWN",
                "risk": "unknown",
                "in_sfha": False,
                "mapped": False,
                "source": "fema_nfhl",
                "lookup_status": ss.CHECKED,
            }, ss.checked(matched=0)

        attrs = features[0].get("attributes", {})
        zone = attrs.get("FLD_ZONE", "X")
        in_sfha = attrs.get("SFHA_TF", "F") == "T"

        risk_map = {
            "A": "high", "AE": "high", "AH": "high", "AO": "high",
            "V": "very_high", "VE": "very_high",
            "B": "moderate", "X500": "moderate",
            "C": "minimal", "X": "minimal",
        }

        return {
            "zone": zone,
            "zone_subtype": attrs.get("ZONE_SUBTY", ""),
            "risk": risk_map.get(zone, "unknown"),
            "in_sfha": in_sfha,
            "mapped": True,
            "source": "fema_nfhl",
            "lookup_status": ss.CHECKED,
        }, ss.checked(matched=1)
    except Exception as e:  # noqa: BLE001 — reported as a status, never as "unmapped"
        status, reason = ss.classify(e)
        logger.warning("FEMA flood zone query %s (%s): %s", status, reason, type(e).__name__)
        return None, ss.entry(status, reason=reason)


# Walk Score API `status` values other than 1 (success). None of them is a
# fact about the location, so none is cached or shown as "no score".
_WALKSCORE_FAILURES = {
    2: (ss.UNAVAILABLE, ss.SCORE_PENDING),         # score being calculated
    30: (ss.UNAVAILABLE, ss.BAD_RESPONSE),         # invalid latitude/longitude
    31: (ss.UNAVAILABLE, ss.UPSTREAM_ERROR),       # Walk Score internal error
    40: (ss.UNAVAILABLE, ss.CREDENTIAL_REJECTED),  # invalid key
    41: (ss.UNAVAILABLE, ss.RATE_LIMITED),         # daily quota exceeded
    42: (ss.UNAVAILABLE, ss.CREDENTIAL_REJECTED),  # IP blocked
}


async def get_walkscore(lat: float, lng: float, address: str) -> Optional[dict]:
    """Get Walk Score, Transit Score, and Bike Score.

    Requires WALKSCORE_API_KEY env var (free: 5000 req/day).
    Get key at: https://www.walkscore.com/professional/api-sign-up.php
    """
    result, _status = await _walkscore_checked(lat, lng, address)
    return result


async def _walkscore_checked(lat: float, lng: float, address: str) -> tuple[Optional[dict], dict]:
    if not _WALKSCORE_KEY:
        # Optional source: says "not configured" without degrading the rest.
        return None, ss.unavailable(ss.NOT_CONFIGURED, required=False)

    params = urllib.parse.urlencode({
        "format": "json",
        "address": address,
        "lat": lat,
        "lon": lng,
        "transit": 1,
        "bike": 1,
        "wsapikey": _WALKSCORE_KEY,
    })
    url = f"https://api.walkscore.com/score?{params}"

    try:
        from data_integrations.cache import cached_external

        async def fetch_upstream() -> dict:
            req = urllib.request.Request(url)
            raw = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=10).read().decode()
            )
            parsed = json.loads(raw)
            code = parsed.get("status") if isinstance(parsed, dict) else None
            if code != 1:
                # Raised so a quota / key / pending answer is never cached for
                # 30 days as this address's score.
                status, reason = _WALKSCORE_FAILURES.get(code, (ss.UNAVAILABLE, ss.BAD_RESPONSE))
                raise ss.SourceFailure(status, reason)
            return {"result": parsed}

        payload = await cached_external(
            "walkscore",
            {
                "lat": round(float(lat), 5),
                "lng": round(float(lng), 5),
                "address": address.strip(),
            },
            fetch_upstream,
            ttl=30 * 86_400,
        )
        result = payload.get("result") or {}
        if result.get("status") != 1:
            # Only a row cached before this fix can get here.
            return None, ss.unavailable(ss.BAD_RESPONSE)

        return {
            "walkscore": result.get("walkscore"),
            "walk_description": result.get("description", ""),
            "transit_score": (result.get("transit") or {}).get("score"),
            "bike_score": (result.get("bike") or {}).get("score"),
            "source": "walkscore",
            "lookup_status": ss.CHECKED,
        }, ss.checked(matched=1)
    except Exception as e:  # noqa: BLE001 — reported as a status, never as "no score"
        status, reason = ss.classify(e)
        logger.warning("Walk Score query %s (%s): %s", status, reason, type(e).__name__)
        return None, ss.entry(status, reason=reason)


def _settled(outcome: Any) -> tuple[Optional[dict], dict]:
    if isinstance(outcome, BaseException):
        return None, ss.failure(outcome)
    value, status = outcome
    return (value if isinstance(value, dict) else None), status


async def enrich_property(address: str, lat: float, lng: float) -> dict:
    """Run all enrichment APIs in parallel for a property location.

    `pois`, `flood` and `walkscore` keep their shapes. `source_status` says,
    per source, whether a null or empty value was checked or never read, and
    `degraded` is true when a required source was not checked.
    """
    outcomes = await asyncio.gather(
        _nearby_pois_checked(lat, lng),
        _flood_zone_checked(lat, lng),
        _walkscore_checked(lat, lng, address),
        return_exceptions=True,
    )
    (pois, pois_s), (flood, flood_s), (walkscore, walk_s) = (_settled(o) for o in outcomes)
    statuses = {"pois": pois_s, "flood": flood_s, "walkscore": walk_s}

    return {
        "pois": pois,
        "flood": flood,
        "walkscore": walkscore,
        "source_status": statuses,
        "degraded": ss.degraded(statuses),
    }
