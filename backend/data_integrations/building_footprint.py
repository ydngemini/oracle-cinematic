"""Address → building-footprint polygon, from licensed and openly-licensed sources.

This is the missing input to `floorplan_pipeline.extract_from_parcel_geometry`,
which could already turn a GeoJSON footprint into an exterior-shell floor plan
but had nothing feeding it — the caller had to supply geometry by hand.

Two sources, tried in order of precision:

  1. **Regrid matched buildings** — a paid, licensed parcel dataset the platform
     already subscribes to. Its client hard-coded `return_matched_buildings=false`,
     so building geometry was being requested and thrown away.
  2. **OpenStreetMap via Overpass** — free and openly licensed (ODbL). Coverage
     is excellent in built-up areas and patchy in rural ones.

Every candidate carries its `source`, `licence` and `attribution` because those
are not decoration: ODbL requires attribution wherever the geometry is shown, and
an agent deciding whether to trust a footprint needs to know where it came from.

Deliberately NOT here: scraping listing portals. Their photos and geometry are
someone else's copyright, their terms forbid it, and the pipeline downstream is
built to refuse rather than guess — which only works if its inputs are clean.

An empty list is only "no footprint" when the sources were actually read. The
`_checked` functions return a CONTRACT C6 status per source, and
`footprint_lookup` turns them into found / none_found / incomplete /
not_checked, so an expired Regrid token or an Overpass timeout is never shown
as "this address has no building" (audit A02, ledger L11-03).
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

import source_status as ss

logger = logging.getLogger("oracle.building_footprint")

# Overall answer of a footprint lookup, derived from the per-source statuses.
LOOKUP_FOUND = "found"            # at least one candidate
LOOKUP_NONE_FOUND = "none_found"  # every source asked answered, none had one
LOOKUP_INCOMPLETE = "incomplete"  # none found, but a required source failed
LOOKUP_NOT_CHECKED = "not_checked"  # no source could be read at all

OVERPASS_URL = os.getenv("ORACLE_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
# Buildings sit within a few tens of metres of their geocoded point. Wider than
# this and a neighbour's house starts outranking the subject.
DEFAULT_RADIUS_M = 40
MAX_RADIUS_M = 150
MAX_CANDIDATES = 5
HTTP_TIMEOUT = 20.0

OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"


class FootprintError(RuntimeError):
    """No usable footprint, or the upstream source refused."""


@dataclass
class FootprintCandidate:
    """One building outline, with everything needed to judge and cite it."""

    geometry: dict[str, Any]          # GeoJSON Polygon, lon/lat
    source: str                       # 'regrid' | 'openstreetmap'
    licence: str
    attribution: str
    area_sqm: float
    # Whatever the source knew. Never inferred here — a guessed storey count
    # multiplies through every downstream rehab line item.
    building_type: Optional[str] = None
    levels: Optional[int] = None
    name: Optional[str] = None
    distance_m: Optional[float] = None
    tags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- geometry helpers ---------------------------------------------------------

def _ring_area_sqm(ring: list[list[float]], latitude: float) -> float:
    """Shoelace area of a lon/lat ring, projected to metres locally.

    Good to a fraction of a percent at building scale, which is well inside the
    accuracy of the footprints themselves."""
    if len(ring) < 3:
        return 0.0
    metres_per_deg_lat = 111_132.0
    metres_per_deg_lon = 111_320.0 * math.cos(math.radians(latitude))
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += (x1 * metres_per_deg_lon) * (y2 * metres_per_deg_lat) - (
            x2 * metres_per_deg_lon
        ) * (y1 * metres_per_deg_lat)
    return abs(total) / 2.0


def _centroid(ring: list[list[float]]) -> tuple[float, float]:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _as_polygon(geometry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalise to a single Polygon, taking the largest ring of a MultiPolygon.

    A MultiPolygon here is usually a main structure plus a detached garage or
    shed; the largest ring is the dwelling, and the rest would otherwise become
    phantom rooms."""
    kind = (geometry or {}).get("type")
    coordinates = (geometry or {}).get("coordinates")
    if kind == "Polygon" and coordinates:
        return {"type": "Polygon", "coordinates": coordinates}
    if kind == "MultiPolygon" and coordinates:
        best, best_area = None, -1.0
        for polygon in coordinates:
            if not polygon or not polygon[0]:
                continue
            _, lat = _centroid(polygon[0])
            area = _ring_area_sqm(polygon[0], lat)
            if area > best_area:
                best, best_area = polygon, area
        if best:
            return {"type": "Polygon", "coordinates": best}
    return None


# --- OpenStreetMap ------------------------------------------------------------

def _overpass_query(lat: float, lon: float, radius_m: int) -> str:
    # `out geom` returns full node coordinates inline, so no second lookup is
    # needed to resolve way members.
    return (
        f"[out:json][timeout:{int(HTTP_TIMEOUT)}];"
        f'(way["building"](around:{radius_m},{lat},{lon});'
        f'relation["building"](around:{radius_m},{lat},{lon}););'
        "out geom;"
    )


async def _fetch_json(url: str, *, data: Optional[bytes] = None) -> dict[str, Any]:
    import asyncio
    import urllib.request

    def _get() -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if data else "GET",
            headers={
                # Overpass rejects unidentified clients under load.
                "User-Agent": "Neoh-Footprint/1.0 (real-estate; contact via app)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(_get)


def _levels(tags: dict[str, Any]) -> Optional[int]:
    raw = tags.get("building:levels")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 100 else None


def _overpass_remark_failure(payload: Any) -> Optional[tuple[str, str]]:
    """Overpass reports a failed query as HTTP 200 + `remark` with no (or
    partial) elements — an outage that otherwise reads as "no buildings"."""
    remark = str(payload.get("remark") or "").lower() if isinstance(payload, dict) else ""
    if "timed out" in remark or "timeout" in remark:
        return ss.TIMED_OUT, ss.UPSTREAM_TIMEOUT
    if "error" in remark or "out of memory" in remark:
        return ss.UNAVAILABLE, ss.UPSTREAM_ERROR
    return None


async def osm_footprints(
    lat: float, lon: float, radius_m: int = DEFAULT_RADIUS_M
) -> list[FootprintCandidate]:
    """Building outlines around a point, from OpenStreetMap.

    [] for a miss AND a failure; `osm_footprints_checked` tells them apart."""
    candidates, _status = await osm_footprints_checked(lat, lon, radius_m)
    return candidates


async def osm_footprints_checked(
    lat: float, lon: float, radius_m: int = DEFAULT_RADIUS_M
) -> tuple[list[FootprintCandidate], dict]:
    radius_m = max(5, min(int(radius_m), MAX_RADIUS_M))
    query = _overpass_query(lat, lon, radius_m).encode("utf-8")
    try:
        payload = await _fetch_json(OVERPASS_URL, data=query)
    except Exception as exc:  # noqa: BLE001 — one source failing is not fatal
        status, reason = ss.classify(exc)
        logger.warning("Overpass footprint lookup %s (%s): %s", status, reason, type(exc).__name__)
        return [], ss.entry(status, reason=reason)
    if not isinstance(payload, dict):
        return [], ss.unavailable(ss.BAD_RESPONSE)
    remark_failure = _overpass_remark_failure(payload)
    if remark_failure is not None:
        status, reason = remark_failure
        logger.warning("Overpass footprint query %s (%s)", status, reason)
        return [], ss.entry(status, reason=reason)

    candidates: list[FootprintCandidate] = []
    for element in payload.get("elements", []):
        geometry = element.get("geometry") or []
        if len(geometry) < 4:
            continue
        ring = [[point["lon"], point["lat"]] for point in geometry]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        tags = element.get("tags") or {}
        centre_lon, centre_lat = _centroid(ring)
        area = _ring_area_sqm(ring, centre_lat)
        # Sheds, bin stores and bus shelters are tagged building=* too.
        if area < 15:
            continue
        candidates.append(
            FootprintCandidate(
                geometry={"type": "Polygon", "coordinates": [ring]},
                source="openstreetmap",
                licence="ODbL-1.0",
                attribution=OSM_ATTRIBUTION,
                area_sqm=round(area, 1),
                building_type=tags.get("building") if tags.get("building") != "yes" else None,
                levels=_levels(tags),
                name=tags.get("name"),
                distance_m=round(_haversine_m(lat, lon, centre_lat, centre_lon), 1),
                tags={k: v for k, v in tags.items() if k.startswith("building")},
            )
        )

    candidates.sort(key=lambda c: (c.distance_m if c.distance_m is not None else 1e9))
    candidates = candidates[:MAX_CANDIDATES]
    return candidates, ss.checked(matched=len(candidates))


# --- Regrid -------------------------------------------------------------------

async def regrid_footprints(address: str) -> list[FootprintCandidate]:
    """Matched building outlines from the licensed Regrid dataset.

    Returns [] rather than raising when unconfigured, so the open source can
    still answer. [] also covers a failure; `regrid_footprints_checked` says
    which."""
    candidates, _status = await regrid_footprints_checked(address)
    return candidates


async def regrid_footprints_checked(address: str) -> tuple[list[FootprintCandidate], dict]:
    token = os.getenv("REGRID_API_TOKEN", "").strip()
    if not token:
        # Optional licensed source: its absence does not degrade an OSM answer.
        return [], ss.not_applicable(ss.NOT_CONFIGURED, required=False)
    if not address.strip():
        return [], ss.not_applicable(ss.NO_ADDRESS, required=False)

    # An expired token is worth one loud line. Returning [] silently is right for
    # "unconfigured" — the open source still answers — but a credential that
    # lapsed looks identical to a parcel with no match, and the caller then
    # reports found:false and fills the geometry with estimates. The licensed
    # source failing and the property having no footprint are different facts.
    from .regrid import RegridParcelSource

    probe = RegridParcelSource()
    if probe.token_expired:
        logger.error(
            "Regrid footprint lookup skipped — %s. Rotate REGRID_API_TOKEN; "
            "falling back to OpenStreetMap.",
            probe.token_expiry_note(),
        )
        return [], ss.unavailable(ss.CREDENTIAL_EXPIRED)

    params = {
        "query": address,
        "limit": "1",
        "return_geometry": "true",
        "return_matched_buildings": "true",
        "return_custom": "false",
        "return_matched_addresses": "false",
        "token": token,
    }
    url = "https://app.regrid.com/api/v2/parcels/address?" + urllib.parse.urlencode(params)
    try:
        payload = await _fetch_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 402):
            logger.error(
                "Regrid footprint lookup rejected the credential (HTTP %s) — "
                "rotate REGRID_API_TOKEN; falling back to OpenStreetMap.",
                exc.code,
            )
            return [], ss.unavailable(ss.CREDENTIAL_REJECTED)
        logger.warning("Regrid footprint lookup failed (HTTP %s)", exc.code)
        return [], ss.failure(exc)
    except Exception as exc:  # noqa: BLE001
        status, reason = ss.classify(exc)
        logger.warning("Regrid footprint lookup %s (%s): %s", status, reason, type(exc).__name__)
        return [], ss.entry(status, reason=reason)
    if not isinstance(payload, dict):
        return [], ss.unavailable(ss.BAD_RESPONSE)

    candidates: list[FootprintCandidate] = []
    for feature in (payload.get("parcels") or {}).get("features", []) or []:
        properties = feature.get("properties") or {}
        for building in properties.get("matched_buildings") or []:
            polygon = _as_polygon(building.get("geometry") or {})
            if not polygon:
                continue
            ring = polygon["coordinates"][0]
            _, centre_lat = _centroid(ring)
            candidates.append(
                FootprintCandidate(
                    geometry=polygon,
                    source="regrid",
                    licence="Regrid subscription (licensed)",
                    attribution="Building footprint © Regrid",
                    area_sqm=round(_ring_area_sqm(ring, centre_lat), 1),
                    building_type=(building.get("properties") or {}).get("type"),
                    tags={},
                )
            )
    candidates = candidates[:MAX_CANDIDATES]
    return candidates, ss.checked(matched=len(candidates))


# --- resolver -----------------------------------------------------------------

async def resolve_footprints(
    *,
    address: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: int = DEFAULT_RADIUS_M,
) -> list[FootprintCandidate]:
    """All available footprints for a subject, best first.

    Licensed data leads because it is address-matched rather than
    proximity-matched; OSM follows and fills the gaps. Nothing is invented: an
    empty list means say so, not fall back to a guess — and anything shown to
    a person uses `resolve_footprints_checked`, because [] here also covers
    "no source could be read"."""
    candidates, _statuses = await resolve_footprints_checked(
        address=address, lat=lat, lon=lon, radius_m=radius_m,
    )
    return candidates


async def resolve_footprints_checked(
    *,
    address: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: int = DEFAULT_RADIUS_M,
) -> tuple[list[FootprintCandidate], dict[str, dict]]:
    """(candidates, {"regrid": status, "openstreetmap": status})."""
    candidates: list[FootprintCandidate] = []
    statuses: dict[str, dict] = {}

    found, statuses["regrid"] = await regrid_footprints_checked(address or "")
    candidates.extend(found)

    if lat is not None and lon is not None:
        found, statuses["openstreetmap"] = await osm_footprints_checked(lat, lon, radius_m)
        candidates.extend(found)
    else:
        # OSM matches by proximity; without coordinates it cannot be asked.
        statuses["openstreetmap"] = ss.not_applicable(ss.NO_COORDINATES)

    if not candidates:
        # Counts and statuses only: never the address (CONTRACT C5.4).
        logger.info(
            "No building footprint: lookup=%s regrid=%s osm=%s",
            footprint_lookup(candidates, statuses),
            statuses["regrid"]["status"], statuses["openstreetmap"]["status"],
        )
    return candidates[:MAX_CANDIDATES], statuses


def footprint_lookup(candidates: list, statuses: Mapping[str, Mapping[str, Any]]) -> str:
    """found | none_found | incomplete | not_checked (see the LOOKUP_* constants)."""
    if candidates:
        return LOOKUP_FOUND
    values = list((statuses or {}).values())
    if not any(v.get("status") == ss.CHECKED for v in values):
        return LOOKUP_NOT_CHECKED
    if ss.degraded(statuses):
        return LOOKUP_INCOMPLETE
    return LOOKUP_NONE_FOUND
