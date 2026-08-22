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
"""

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger("oracle.apis.property_data")

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_FEMA_URL = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
_WALKSCORE_KEY = os.environ.get("WALKSCORE_API_KEY", "")


async def get_nearby_pois(lat: float, lng: float, radius_m: int = 1000) -> dict:
    """Query OpenStreetMap Overpass for nearby points of interest.

    Returns categorized POI counts within radius.
    """
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
            return {"result": json.loads(raw)}

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
        total = result.get("elements", [{}])[0].get("tags", {}).get("total", 0)
        return {
            "radius_m": radius_m,
            "total_pois": int(total) if total else 0,
            "source": "openstreetmap",
        }
    except Exception as e:
        logger.warning("Overpass query failed: %s", e)
        return {"radius_m": radius_m, "total_pois": 0, "error": str(e)}


async def get_flood_zone(lat: float, lng: float) -> Optional[dict]:
    """Check FEMA flood zone status for coordinates.

    Returns zone designation (A, AE, X, etc.) and risk level.
    """
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
            return {"result": json.loads(raw)}

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
            }

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
        }
    except Exception as e:
        logger.warning("FEMA flood zone query failed: %s", e)
        return None


async def get_walkscore(lat: float, lng: float, address: str) -> Optional[dict]:
    """Get Walk Score, Transit Score, and Bike Score.

    Requires WALKSCORE_API_KEY env var (free: 5000 req/day).
    Get key at: https://www.walkscore.com/professional/api-sign-up.php
    """
    if not _WALKSCORE_KEY:
        return None

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
            return {"result": json.loads(raw)}

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
            return None

        return {
            "walkscore": result.get("walkscore"),
            "walk_description": result.get("description", ""),
            "transit_score": result.get("transit", {}).get("score"),
            "bike_score": result.get("bike", {}).get("score"),
            "source": "walkscore",
        }
    except Exception as e:
        logger.warning("Walkscore API failed: %s", e)
        return None


async def enrich_property(address: str, lat: float, lng: float) -> dict:
    """Run all enrichment APIs in parallel for a property location."""
    pois, flood, walkscore = await asyncio.gather(
        get_nearby_pois(lat, lng),
        get_flood_zone(lat, lng),
        get_walkscore(lat, lng, address),
        return_exceptions=True,
    )

    return {
        "pois": pois if isinstance(pois, dict) else None,
        "flood": flood if isinstance(flood, dict) else None,
        "walkscore": walkscore if isinstance(walkscore, dict) else None,
    }
