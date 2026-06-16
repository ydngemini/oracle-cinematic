"""Hawaii (Honolulu / Oahu) — HOLIS CadastralTables ArcGIS FeatureServer.

Scope: City & County of Honolulu (Oahu) — the largest county in Hawaii.

Primary table — OWNDAT (layer 6):
    URL: https://services.arcgis.com/tNJpAOha4mODLkXz/arcgis/rest/services/CadastralTables/FeatureServer/6/query
    Key fields: parid, taxbillowner, taxbilladdress, taxbillcity, taxbillstate,
                taxbillzip5, owntype1
    Record count: ~326,000

Enrichment table — ASMTGIS (layer 9):
    URL: https://services.arcgis.com/tNJpAOha4mODLkXz/arcgis/rest/services/CadastralTables/FeatureServer/9/query
    Key fields: parid, buildingvalue, landvalue, tnettaxval
    Record count: ~326,000+

Both tables join on `parid` (12-digit string: TMK8 + 4-digit suffix).
OWNDAT provides mailing address; address on parcel is derived from TMK but not
directly in these assessment tables. Absentee-owner detection compares
taxbillstate != 'HI'.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord

# ---------------------------------------------------------------------------
# Endpoint constants
# ---------------------------------------------------------------------------
_BASE = (
    "https://services.arcgis.com/tNJpAOha4mODLkXz/arcgis/rest/services"
    "/CadastralTables/FeatureServer"
)
OWNDAT_URL = f"{_BASE}/6/query"    # owner + mailing address
ASMTGIS_URL = f"{_BASE}/9/query"  # assessed values


class HawaiiHonoluluHarvester(ArcGISHarvester):
    """Honolulu / Oahu parcel assessment harvester.

    Paginates OWNDAT for owner + address data, then batch-enriches each page
    with ASMTGIS assessed values before mapping to PropertyRecord.
    """

    STATE = "HI"
    SOURCE_LABEL = "HOLIS CadastralTables / OWNDAT + ASMTGIS (Honolulu)"

    # ArcGISHarvester.fetch_raw() uses SERVICE_URL; we override fetch_raw
    # entirely so this constant isn't used, but it's kept for env-override
    # compat ($HI_SOURCE_URL will redirect the primary pull if set).
    SERVICE_URL = OWNDAT_URL
    WHERE = "taxbillowner IS NOT NULL"
    OUT_FIELDS = "parid,taxbillowner,taxbilladdress,taxbillcity,taxbillstate,taxbillzip5,owntype1"

    # ------------------------------------------------------------------ #
    # Override fetch_raw to paginate OWNDAT + enrich with ASMTGIS values  #
    # ------------------------------------------------------------------ #
    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        import os
        from .base import PAGE_SIZE, logger

        owndat_url = os.getenv("HI_SOURCE_URL", OWNDAT_URL)
        rows_out: list[dict] = []
        offset = 0

        while True:
            page = min(PAGE_SIZE, (max_records - len(rows_out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break

            # ---- 1. Fetch OWNDAT page ----
            params = {
                "where": self.WHERE,
                "outFields": self.OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": page,
            }
            data = await self._get_json(f"{owndat_url}?{urllib.parse.urlencode(params)}")
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                logger.warning(
                    "[%s] OWNDAT ArcGIS error (code=%s): %s",
                    self.STATE, err.get("code", "?"), err.get("message", str(err))[:200],
                )
                break
            feats = data.get("features", []) if isinstance(data, dict) else []
            owndat_rows = [f.get("attributes", {}) for f in feats if isinstance(f, dict)]
            if not owndat_rows:
                break

            # ---- 2. Batch-fetch ASMTGIS for this page's parids ----
            parids = [r["parid"] for r in owndat_rows if r.get("parid")]
            asmt_map: dict[str, dict] = {}
            if parids:
                parid_list = ",".join(f"'{p}'" for p in parids)
                asmt_params = {
                    "where": f"parid IN ({parid_list})",
                    "outFields": "parid,buildingvalue,landvalue,tnettaxval",
                    "returnGeometry": "false",
                    "f": "json",
                    "resultRecordCount": len(parids) + 10,
                }
                try:
                    asmt_data = await self._get_json(
                        f"{ASMTGIS_URL}?{urllib.parse.urlencode(asmt_params)}"
                    )
                    if isinstance(asmt_data, dict) and not asmt_data.get("error"):
                        for feat in asmt_data.get("features", []):
                            attr = feat.get("attributes", {})
                            pid = attr.get("parid")
                            if pid:
                                asmt_map[pid] = attr
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] ASMTGIS enrichment failed for batch: %s", self.STATE, exc)

            # ---- 3. Merge ASMTGIS values into OWNDAT rows ----
            for row in owndat_rows:
                pid = row.get("parid")
                asmt = asmt_map.get(pid, {})
                row["_buildingvalue"] = asmt.get("buildingvalue", 0.0)
                row["_landvalue"] = asmt.get("landvalue", 0.0)
                row["_tnettaxval"] = asmt.get("tnettaxval", 0.0)
            rows_out.extend(owndat_rows)

            offset += len(owndat_rows)
            if not data.get("exceededTransferLimit") and len(owndat_rows) < page:
                break
            if max_records and len(rows_out) >= max_records:
                break

        return rows_out

    # ------------------------------------------------------------------ #
    # Column mapping                                                        #
    # ------------------------------------------------------------------ #
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = str(row.get("parid") or "").strip()
        owner = str(row.get("taxbillowner") or "").strip()
        if not parcel_id or not owner:
            return None

        address = str(row.get("taxbilladdress") or "").strip()
        city = str(row.get("taxbillcity") or "").strip()
        state_field = str(row.get("taxbillstate") or "HI").strip()
        zip_code = str(row.get("taxbillzip5") or "").strip()[:10]

        # Absentee: mailing state differs from HI
        is_absentee = bool(state_field) and state_field.upper() != "HI"

        # Assessed value: total net = building + land; fallback to tnettaxval
        bldg = to_float(row.get("_buildingvalue"))
        land = to_float(row.get("_landvalue"))
        net = to_float(row.get("_tnettaxval"))
        estimated_value = (bldg + land) if (bldg + land) > 0 else net

        distress_flags: list[str] = []
        if is_absentee:
            distress_flags.append("absentee_owner")

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=city,
            state=self.STATE,
            zip_code=zip_code,
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=estimated_value,
            equity_percent=0.0,
            is_absentee_owner=is_absentee,
            distress_flags=distress_flags,
            last_sale_date=None,
        )
