"""Tenant-scoped public-property pipeline map queries.

This endpoint deliberately uses the canonical ``leads`` table and does not
expose MLS/provider terminology. Coordinates are read from source payloads;
records without a verified coordinate are excluded from map responses.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

router = APIRouter(prefix="/api/v1/pipeline", tags=["Public Property Pipeline"])
_BBOX_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    match = _BBOX_RE.match(value or "")
    if not match:
        raise HTTPException(status_code=422, detail="bbox must be min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = (float(part) for part in match.groups())
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise HTTPException(status_code=422, detail="bbox coordinates are out of range")
    if max_lon - min_lon > 180 or max_lat - min_lat > 90:
        raise HTTPException(status_code=422, detail="bbox is too large")
    return min_lon, min_lat, max_lon, max_lat


_COORDINATE_SQL = """
    CASE WHEN payload->>'latitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
              AND payload->>'longitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
              AND (payload->>'latitude')::double precision BETWEEN -90 AND 90
              AND (payload->>'longitude')::double precision BETWEEN -180 AND 180
         THEN (payload->>'latitude')::double precision END
"""
_LONGITUDE_SQL = """
    CASE WHEN payload->>'latitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
              AND payload->>'longitude' ~ '^-?[0-9]+(\\.[0-9]+)?$'
              AND (payload->>'latitude')::double precision BETWEEN -90 AND 90
              AND (payload->>'longitude')::double precision BETWEEN -180 AND 180
         THEN (payload->>'longitude')::double precision END
"""
_CONFIDENCE_SQL = f"""
    CASE WHEN ({_COORDINATE_SQL}) IS NOT NULL THEN 100
         WHEN COALESCE(payload->>'address', '') <> '' THEN 60
         ELSE 0 END
"""


@router.get("/map-clusters")
async def pipeline_map_clusters(
    bbox: str = Query(..., min_length=7, max_length=160),
    zoom: float = Query(..., ge=0, le=24),
    confidence_min: int = Query(default=0, ge=0, le=100),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Return clustered low-zoom points or bounded high-zoom lead markers."""
    min_lon, min_lat, max_lon, max_lat = _parse_bbox(bbox)
    lat_sql = _COORDINATE_SQL
    lon_sql = _LONGITUDE_SQL
    # Grid bins avoid a PostGIS dependency while remaining deterministic on the
    # existing CPU deployment. The bin is deliberately coarser at low zoom.
    grid = max(0.01, min(2.0, 360.0 / (2 ** max(1, int(zoom) + 2))))
    async with tenant_tx(ctx) as conn:
        if zoom < 12:
            rows = await conn.fetch(
                f"""
                WITH points AS (
                    SELECT motivation_score,
                           {_CONFIDENCE_SQL} AS confidence,
                           {lat_sql} AS lat,
                           {lon_sql} AS lon
                      FROM leads
                     WHERE ($1 OR tenant_id = $2::uuid)
                ), bounded AS (
                    SELECT *, floor(lon / $4::double precision) AS gx,
                              floor(lat / $4::double precision) AS gy
                      FROM points
                     WHERE confidence >= $3
                       AND lon BETWEEN $5 AND $6 AND lat BETWEEN $7 AND $8
                       AND lat IS NOT NULL AND lon IS NOT NULL
                )
                SELECT concat(gx::bigint, ':', gy::bigint) AS cluster_id,
                       avg(lat) AS lat, avg(lon) AS lon, count(*) AS count,
                       avg(motivation_score) AS avg_motivation_score
                  FROM bounded
                 GROUP BY gx, gy
                 ORDER BY count DESC
                 LIMIT 500
                """,
                ctx.is_platform_admin,
                ctx.tenant_id,
                confidence_min,
                grid,
                min_lon,
                max_lon,
                min_lat,
                max_lat,
            )
            return {
                "mode": "clusters",
                "items": [dict(row) for row in rows],
                "bounded": True,
            }

        rows = await conn.fetch(
            f"""
            SELECT id::text AS id, {lat_sql} AS lat, {lon_sql} AS lon,
                   payload->>'address' AS address,
                   CASE WHEN underwriting->>'mao' ~ '^-?[0-9.]+$'
                        THEN (underwriting->>'mao')::numeric END AS mao,
                   motivation_score AS priority
              FROM leads
             WHERE ($1 OR tenant_id = $2::uuid)
               AND ({_CONFIDENCE_SQL}) >= $3
               AND {lon_sql} BETWEEN $4 AND $5
               AND {lat_sql} BETWEEN $6 AND $7
             ORDER BY motivation_score DESC, id ASC
             LIMIT 250
            """,
            ctx.is_platform_admin,
            ctx.tenant_id,
            confidence_min,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
        )
    return {"mode": "markers", "items": [dict(row) for row in rows], "bounded": True, "limit": 250}
