"""Florida — FDOR Cadastral 2025 statewide parcels (ArcGIS FeatureServer).

Source : Florida Geospatial Open Data Portal (FGIO)
         https://geodata.floridagio.gov/datasets/FGIO::florida-statewide-parcels
Service: https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/
         Florida_Statewide_Cadastral/FeatureServer/0/query
Scope  : statewide — all 67 Florida counties, ~10.8 M parcels
Updated: annually (August) by Florida Department of Revenue

Column map (source → PropertyRecord):
  PARCEL_ID  → parcel_id
  PHY_ADDR1  → address          (physical/situs street address)
  PHY_CITY   → city
  PHY_ZIPCD  → zip_code
  OWN_NAME   → owner_name
  OWN_STATE  → absentee check   (owner state != "FL")
  JV         → estimated_value  (Just Value / market value)
  CENSUS_BK  → county           (Census state/county FIPS prefix)
  TOT_LVG_AR → building_area_sqft
  LND_SQFOOT → lot_area_sqft
  ACT_YR_BLT / EFF_YR_BLT → year_built
  DOR_UC     → property_class
  SALE_YR1 + SALE_MO1 → last_sale_date

Exact parcel lookups can also join an allow-listed county FeatureServer.  The
county registry starts with Alachua County and is intentionally explicit: a
county endpoint is only queried when its official parcel schema is mapped and
tested.  No portal HTML or anti-bot page is bypassed.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

from .base import (
    ArcGISHarvester,
    classify_owner,
    promote_public_characteristics,
    to_float,
)
from .property_adapter import PropertyRecord


_FL_COUNTIES_BY_FIPS = {
    "001": "Alachua", "003": "Baker", "005": "Bay", "007": "Bradford",
    "009": "Brevard", "011": "Broward", "013": "Calhoun", "015": "Charlotte",
    "017": "Citrus", "019": "Clay", "021": "Collier", "023": "Columbia",
    "027": "DeSoto", "029": "Dixie", "031": "Duval", "033": "Escambia",
    "035": "Flagler", "037": "Franklin", "039": "Gadsden", "041": "Gilchrist",
    "043": "Glades", "045": "Gulf", "047": "Hamilton", "049": "Hardee",
    "051": "Hendry", "053": "Hernando", "055": "Highlands", "057": "Hillsborough",
    "059": "Holmes", "061": "Indian River", "063": "Jackson", "065": "Jefferson",
    "067": "Lafayette", "069": "Lake", "071": "Lee", "073": "Leon",
    "075": "Levy", "077": "Liberty", "079": "Madison", "081": "Manatee",
    "083": "Marion", "085": "Martin", "086": "Miami-Dade", "087": "Monroe",
    "089": "Nassau", "091": "Okaloosa", "093": "Okeechobee", "095": "Orange",
    "097": "Osceola", "099": "Palm Beach", "101": "Pasco", "103": "Pinellas",
    "105": "Polk", "107": "Putnam", "109": "St. Johns", "111": "St. Lucie",
    "113": "Santa Rosa", "115": "Sarasota", "117": "Seminole", "119": "Sumter",
    "121": "Suwannee", "123": "Taylor", "125": "Union", "127": "Volusia",
    "129": "Wakulla", "131": "Walton", "133": "Washington",
}

_ALACHUA_SERVICE_URL = (
    "https://maps.alachuacounty.us/server/rest/services/Hosted/"
    "ParcelsACGM/FeatureServer/0/query"
)
_ALACHUA_FIELDS = ",".join([
    "parcel", "firstname1", "address1", "city", "zip", "squarefeet",
    "heatedsquarefeet", "acres", "justvalue", "impvalue", "propertyuse",
    "p_category", "puse", "taxyear", "zonedistrict", "zonedefin",
    "zonecode", "saledate", "saleamount", "citydescription",
])
_COUNTY_DETAIL_SOURCES = {
    "001": {
        "name": "Alachua County Growth Management parcels",
        "url": _ALACHUA_SERVICE_URL,
        "parcel_field": "parcel",
        "out_fields": _ALACHUA_FIELDS,
    },
}

# Only pull the columns we need — reduces transfer size on a 10 M-row dataset.
_OUT_FIELDS = ",".join([
    "PARCEL_ID",
    "OWN_NAME",
    "OWN_STATE",
    "PHY_ADDR1",
    "PHY_CITY",
    "PHY_ZIPCD",
    "CENSUS_BK",    # Census state/county/block identifier
    "CO_NO",        # FDOR source county number retained as provenance
    "JV",           # Just Value  (market / assessed value)
    "LND_SQFOOT",   # Published land square feet
    "TOT_LVG_AR",   # Published total living area
    "ACT_YR_BLT",   # Actual construction year
    "EFF_YR_BLT",   # Effective year when actual year is absent
    "DOR_UC",       # FDOR property-use class
    "PA_UC",        # Local property appraiser use class
    "SALE_PRC1",    # Most-recent sale price
    "SALE_YR1",     # Sale year
    "SALE_MO1",     # Sale month (string "01"–"12" or " ")
])


class FloridaFDORHarvester(ArcGISHarvester):
    STATE = "FL"
    SOURCE_KEY = "firehose:FL"
    SOURCE_LABEL = "FGIO FDOR Cadastral 2025 statewide parcels"
    SERVICE_URL = (
        "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
        "Florida_Statewide_Cadastral/FeatureServer/0/query"
    )
    WHERE = "OWN_NAME IS NOT NULL AND PHY_ADDR1 IS NOT NULL"
    OUT_FIELDS = _OUT_FIELDS

    @staticmethod
    def _county_fips(row: dict[str, Any]) -> Optional[str]:
        census_block = "".join(ch for ch in str(row.get("CENSUS_BK") or "") if ch.isdigit())
        if len(census_block) >= 5 and census_block.startswith("12"):
            return census_block[2:5]
        return None

    @staticmethod
    def _positive(value: Any) -> Optional[float]:
        parsed = to_float(value)
        return parsed if parsed > 0 else None

    @staticmethod
    def _year(value: Any) -> Optional[int]:
        try:
            parsed = int(float(str(value)))
        except (TypeError, ValueError):
            return None
        return parsed if 1600 <= parsed <= datetime.now(timezone.utc).year + 1 else None

    @staticmethod
    def _zip(value: Any) -> str:
        if value in (None, ""):
            return ""
        text = str(value).strip()
        try:
            text = str(int(float(text)))
        except (TypeError, ValueError):
            pass
        return text.zfill(5)[:10]

    async def _query_exact(
        self,
        service_url: str,
        *,
        where: str,
        out_fields: str,
    ) -> list[dict[str, Any]]:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": 10,
        }
        data = await self._get_json(
            f"{service_url}?{urllib.parse.urlencode(params)}"
        )
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            raise RuntimeError(
                f"Florida public parcel service error {error.get('code', '?')}: "
                f"{str(error.get('message') or error)[:180]}"
            )
        features = data.get("features", []) if isinstance(data, dict) else []
        return [
            feature.get("attributes", {})
            for feature in features
            if isinstance(feature, dict) and isinstance(feature.get("attributes"), dict)
        ]

    async def lookup_parcel(self, parcel_id: str) -> list[PropertyRecord]:
        """Resolve one FDOR parcel and join its allow-listed county detail feed."""
        parcel = str(parcel_id or "").strip()
        if not parcel or len(parcel) > 80:
            return []
        escaped = parcel.replace("'", "''")
        rows = await self._query_exact(
            self.SERVICE_URL,
            where=f"PARCEL_ID='{escaped}'",
            out_fields=self.OUT_FIELDS,
        )
        # Kept so the caller can retain this observation on demand. A targeted
        # lookup is exactly the case where a citable source_record is worth
        # storing — somebody is researching this property right now.
        self.last_raw_rows = list(rows)
        records: list[PropertyRecord] = []
        for row in rows:
            record = self.map_record(row)
            if record is None:
                continue
            record = promote_public_characteristics(record, row)
            county_fips = self._county_fips(row)
            detail_source = _COUNTY_DETAIL_SOURCES.get(county_fips or "")
            if detail_source:
                county_rows = await self._query_exact(
                    str(detail_source["url"]),
                    where=(
                        f"{detail_source['parcel_field']}="
                        f"'{escaped}'"
                    ),
                    out_fields=str(detail_source["out_fields"]),
                )
                if county_rows:
                    self._merge_county_detail(
                        record,
                        county_rows[0],
                        county_fips=county_fips or "",
                        source_name=str(detail_source["name"]),
                    )
            metadata = dict(record.source_metadata or {})
            metadata["targeted_enrichment"] = {
                "completed": True,
                "statewide_checked": True,
                "county_detail_checked": bool(detail_source),
                "county_fips": county_fips,
            }
            record.source_metadata = metadata
            records.append(record)
        return records

    def _merge_county_detail(
        self,
        record: PropertyRecord,
        row: dict[str, Any],
        *,
        county_fips: str,
        source_name: str,
    ) -> None:
        """Merge fields published by an official county parcel service."""
        owner = str(row.get("firstname1") or "").strip()
        if owner:
            record.owner_name = owner
            record.owner_type = classify_owner(owner)
        record.county = _FL_COUNTIES_BY_FIPS.get(county_fips) or record.county
        record.address = str(row.get("address1") or record.address).strip()
        record.city = str(row.get("city") or record.city).strip()
        record.zip_code = self._zip(row.get("zip")) or record.zip_code
        record.estimated_value = self._positive(row.get("justvalue")) or record.estimated_value
        record.building_area_sqft = (
            self._positive(row.get("heatedsquarefeet"))
            or self._positive(row.get("squarefeet"))
            or record.building_area_sqft
        )
        record.lot_area_sqft = (
            self._positive(row.get("acres")) * 43_560.0
            if self._positive(row.get("acres")) is not None
            else record.lot_area_sqft
        )
        record.land_use = str(
            row.get("propertyuse") or row.get("p_category") or record.land_use or ""
        ).strip() or None
        record.zoning_district = str(
            row.get("zonedistrict") or row.get("zonecode") or ""
        ).strip() or record.zoning_district
        sale_amount = self._positive(row.get("saleamount"))
        if sale_amount is not None:
            record.last_sale_price = sale_amount

        metadata = dict(record.source_metadata or {})
        datasets = metadata.get("datasets")
        metadata["datasets"] = {
            **(datasets if isinstance(datasets, dict) else {}),
            "county_detail": source_name,
        }
        sources = metadata.get("published_field_sources")
        metadata["published_field_sources"] = {
            **(sources if isinstance(sources, dict) else {}),
            "owner_name": "firstname1",
            "county": "county registry + citydescription",
            "building_area_sqft": (
                "heatedsquarefeet" if row.get("heatedsquarefeet") not in (None, "")
                else "squarefeet"
            ),
            "lot_area_sqft": "acres",
            "land_use": "propertyuse",
            "zoning_district": "zonedistrict",
        }
        metadata["county_record"] = {
            "tax_year": self._year(row.get("taxyear")),
            "improvement_value": self._positive(row.get("impvalue")),
            "property_use_code": str(row.get("puse") or "").strip() or None,
            "zoning_definition": str(row.get("zonedefin") or "").strip() or None,
            "zone_code": str(row.get("zonecode") or "").strip() or None,
        }
        record.dataset_version = (
            f"fdor:2025;county:{self._year(row.get('taxyear')) or 'unknown'}"
        )
        record.source_metadata = metadata

    # ------------------------------------------------------------------
    # Record mapper
    # ------------------------------------------------------------------
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("PARCEL_ID") or "").strip()
        addr   = str(row.get("PHY_ADDR1") or "").strip()
        if not parcel or not addr:
            return None

        owner    = str(row.get("OWN_NAME") or "").strip()
        city     = str(row.get("PHY_CITY") or "").strip()
        zip_code = self._zip(row.get("PHY_ZIPCD"))

        # Just Value is the Florida DOR's market/assessed value.
        jv = to_float(row.get("JV"))

        # Absentee: owner's state of record is not FL.
        own_state = str(row.get("OWN_STATE") or "").strip().upper()
        absentee  = bool(own_state) and own_state != "FL"

        # FDOR only publishes sale month/year. Do not manufacture a calendar
        # day just to satisfy an ISO-date column.
        yr = self._year(row.get("SALE_YR1"))
        mo  = str(row.get("SALE_MO1") or "").strip()
        try:
            month = int(mo)
        except (TypeError, ValueError):
            month = 0
        last_sale = None

        county_fips = self._county_fips(row)
        actual_year = self._year(row.get("ACT_YR_BLT"))
        effective_year = self._year(row.get("EFF_YR_BLT"))
        property_class = str(row.get("DOR_UC") or row.get("PA_UC") or "").strip() or None
        field_sources = {
            key: source
            for key, source, value in (
                ("county", "CENSUS_BK", county_fips),
                ("building_area_sqft", "TOT_LVG_AR", row.get("TOT_LVG_AR")),
                ("lot_area_sqft", "LND_SQFOOT", row.get("LND_SQFOOT")),
                ("year_built", "ACT_YR_BLT" if actual_year else "EFF_YR_BLT", actual_year or effective_year),
                ("property_class", "DOR_UC" if row.get("DOR_UC") else "PA_UC", property_class),
                ("last_sale_price", "SALE_PRC1", self._positive(row.get("SALE_PRC1"))),
            )
            if value not in (None, "")
        }

        distress: list[str] = []
        if absentee:
            distress.append("absentee_owner")

        return PropertyRecord(
            parcel_id      = parcel,
            address        = addr,
            city           = city,
            state          = self.STATE,
            zip_code       = zip_code[:10],
            owner_name     = owner,
            owner_type     = classify_owner(owner),
            estimated_value= jv,
            equity_percent = 0.0,        # DOR data has no debt/mortgage field
            is_absentee_owner = absentee,
            distress_flags = distress,
            last_sale_date = last_sale,
            county=_FL_COUNTIES_BY_FIPS.get(county_fips or ""),
            year_built=actual_year or effective_year,
            property_class=property_class,
            last_sale_price = to_float(row.get("SALE_PRC1")) or None,
            lot_area_sqft=self._positive(row.get("LND_SQFOOT")),
            building_area_sqft=self._positive(row.get("TOT_LVG_AR")),
            dataset_version="fdor:2025",
            source_metadata={
                "datasets": {"statewide_assessment": "Florida Statewide Cadastral"},
                "fdor_county_number": row.get("CO_NO"),
                "census_block": str(row.get("CENSUS_BK") or "").strip() or None,
                "local_property_use_code": str(row.get("PA_UC") or "").strip() or None,
                "published_sale_month": month if yr and 1 <= month <= 12 else None,
                "published_sale_year": yr if yr and 1 <= month <= 12 else None,
                "published_field_sources": field_sources,
            },
        )
