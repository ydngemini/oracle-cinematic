"""Mississippi — MDEQ/MARIS statewide cadastral parcels (ArcGIS MapServer).

Source: Mississippi Automated Resource Information System (MARIS) / MDEQ
        2024 statewide parcel dataset covering all 82 counties.
        ~1.96M records (East layer: ~1.08M, West layer: ~879K).

Service URL (MapServer — FeatureServer not enabled):
  https://gis.mississippi.edu/server/rest/services/Cadastral/MS_Parcels_August_2024/MapServer

Layer 1: MS_West_MDEQPARCELS_2024
Layer 2: MS_East_MDEQPARCELS_2024

Pagination note: resultOffset is NOT supported (supportsPagination=false).
We use FID-range batching (FID >= N AND FID < N+PAGE_SIZE) instead.
FID is a sequential integer starting at 0 within each layer.

Key field map (exact column names from service):
  PARNO     → parcel_id
  SITEADD   → address
  SCITY     → city
  SZIP      → zip_code
  OWNNAME   → owner_name
  MAILADD1  → mailing street (absentee check)
  MCITY1    → mailing city
  MSTATE1   → mailing state
  MZIP1     → mailing zip
  TOTVAL    → estimated_value  (LANDVAL + IMPVAL1 as fallback)
  LANDVAL   → land component
  DEEDDATE  → last_sale_date
  CNTYNAME  → county (used as city fallback when SCITY blank)
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Optional

from .base import BaseHarvester, RateLimiter, PAGE_SIZE, to_float, classify_owner, norm
from .property_adapter import PropertyRecord

logger = logging.getLogger("oracle.harvester")

_MS_BASE = (
    "https://gis.mississippi.edu/server/rest/services/Cadastral/"
    "MS_Parcels_August_2024/MapServer"
)
# East and West are two separate layers covering the full state.
_LAYERS = [
    (1, "MS_West_MDEQPARCELS_2024"),
    (2, "MS_East_MDEQPARCELS_2024"),
]
_OUT_FIELDS = (
    "FID,PARNO,OWNNAME,SITEADD,SCITY,SSTATE,SZIP,"
    "MAILADD1,MCITY1,MSTATE1,MZIP1,"
    "TOTVAL,LANDVAL,DEEDDATE,CNTYNAME"
)
# Each layer has ~1M records; FID is contiguous starting at 0.
# We fetch FID ranges to work around the service's lack of resultOffset.
_FID_STEP = 2000   # max the service will return per call (maxRecordCount=2000)


class MississippiMDEQHarvester(BaseHarvester):
    """Statewide Mississippi parcel/assessment data from MARIS/MDEQ 2024."""

    STATE = "MS"
    SOURCE_LABEL = "MARIS/MDEQ MS_Parcels_August_2024 (MapServer FID-range)"

    # ------------------------------------------------------------------
    # fetch_raw — FID-range pagination across both East + West layers
    # ------------------------------------------------------------------
    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        out: list[dict] = []
        for layer_id, layer_name in _LAYERS:
            if max_records and len(out) >= max_records:
                break
            layer_out = await self._fetch_layer(
                layer_id,
                layer_name,
                remaining=None if max_records is None else (max_records - len(out)),
            )
            out.extend(layer_out)
        return out

    async def _fetch_layer(
        self, layer_id: int, layer_name: str, *, remaining: Optional[int]
    ) -> list[dict]:
        base_url = f"{_MS_BASE}/{layer_id}/query"
        out: list[dict] = []
        fid_start = 0

        # Step 1: determine the max FID so we know when to stop.
        max_fid = await self._get_max_fid(layer_id, layer_name)
        if max_fid is None:
            logger.warning("[MS] Could not determine max FID for layer %s — skipping", layer_name)
            return []

        logger.info("[MS] Layer %s: FID range 0..%d", layer_name, max_fid)

        while fid_start <= max_fid:
            if remaining is not None and len(out) >= remaining:
                break
            fid_end = fid_start + _FID_STEP
            page_limit = (
                min(_FID_STEP, remaining - len(out))
                if remaining is not None
                else _FID_STEP
            )
            params = {
                "where": (
                    f"FID >= {fid_start} AND FID < {fid_end} "
                    "AND OWNNAME IS NOT NULL AND TOTVAL >= 0"
                ),
                "outFields": _OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            }
            url = f"{base_url}?{urllib.parse.urlencode(params)}"
            data = await self._get_json(url)
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                logger.warning(
                    "[MS] ArcGIS error layer %s FID %d-%d (code=%s): %s",
                    layer_name, fid_start, fid_end,
                    err.get("code", "?"), err.get("message", "")[:200],
                )
                fid_start = fid_end
                continue

            feats = (data.get("features", []) if isinstance(data, dict) else [])
            rows = [f.get("attributes", {}) for f in feats if isinstance(f, dict)]
            out.extend(rows[:page_limit] if remaining else rows)
            fid_start = fid_end

        logger.info("[MS] Layer %s: fetched %d rows", layer_name, len(out))
        return out

    async def _get_max_fid(self, layer_id: int, layer_name: str) -> Optional[int]:
        """Return the maximum FID in the layer (used to bound the range loop)."""
        url = (
            f"{_MS_BASE}/{layer_id}/query?"
            "where=1%3D1&returnIdsOnly=true&f=json"
        )
        try:
            data = await self._get_json(url)
            ids = data.get("objectIds") if isinstance(data, dict) else None
            if ids:
                return max(ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MS] Failed to get IDs for layer %s: %s", layer_name, exc)
        # Fallback: query count and assume FIDs are 0-based contiguous.
        count_url = (
            f"{_MS_BASE}/{layer_id}/query?"
            "where=1%3D1&returnCountOnly=true&f=json"
        )
        try:
            data = await self._get_json(count_url)
            count = data.get("count") if isinstance(data, dict) else None
            if count:
                return int(count) - 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MS] Count fallback also failed for layer %s: %s", layer_name, exc)
        return None

    # ------------------------------------------------------------------
    # map_record — MDEQ column names → PropertyRecord
    # ------------------------------------------------------------------
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARNO") or "").strip()
        if not parcel:
            return None

        owner = str(row.get("OWNNAME") or "").strip()
        if not owner:
            return None

        site_addr = str(row.get("SITEADD") or "").strip()
        # Some records store "0" as a placeholder address — treat as blank.
        if site_addr in ("0", "0 ", ""):
            site_addr = ""

        city = str(row.get("SCITY") or "").strip()
        if not city:
            # Fall back to county name when site city is blank.
            city = str(row.get("CNTYNAME") or "").strip().title()

        zip_code = str(row.get("SZIP") or "").strip()[:10]

        # Mailing address for absentee detection.
        mail_street = str(row.get("MAILADD1") or "").strip()
        mail_city = str(row.get("MCITY1") or "").strip()
        mail_state = str(row.get("MSTATE1") or "").strip()
        mail_zip = str(row.get("MZIP1") or "").strip()

        # Absentee: mailing address is out-of-state OR normalised != site address.
        prop_state = str(row.get("SSTATE") or "MS").strip()
        if mail_state and mail_state.upper() != prop_state.upper():
            absentee = True
        elif mail_street:
            mail_full = norm(f"{mail_street}{mail_zip}")
            site_full = norm(f"{site_addr}{zip_code}")
            absentee = bool(mail_full) and mail_full != site_full
        else:
            absentee = False

        total_val = to_float(row.get("TOTVAL"))
        if total_val == 0.0:
            total_val = to_float(row.get("LANDVAL"))

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")
        if total_val == 0.0:
            distress.append("zero_assessed_value")

        return PropertyRecord(
            parcel_id=parcel,
            address=site_addr,
            city=city,
            state="MS",
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=total_val,
            equity_percent=0.0,
            is_absentee_owner=absentee,
            distress_flags=distress,
            last_sale_date=str(row.get("DEEDDATE") or "").strip()[:10] or None,
        )
