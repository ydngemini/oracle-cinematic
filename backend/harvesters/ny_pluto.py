"""New York — NYC PLUTO tax-lot extract (Socrata JSON).

NYC Open Data publishes PLUTO (Primary Land Use Tax Lot Output) as a Socrata
resource. Residential lots are filtered by building class (A/B/C/D/R...).
"""
from __future__ import annotations

import os
from typing import Optional

from .base import SocrataHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord

_RESIDENTIAL_CLASS_PREFIXES = ("A", "B", "C", "D", "R", "S")


class NewYorkPlutoHarvester(SocrataHarvester):
    STATE = "NY"
    SOURCE_LABEL = "NYC PLUTO (Socrata)"
    RESOURCE_URL = "https://data.cityofnewyork.us/resource/64uk-42ks.json"

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel = str(row.get("bbl") or row.get("borough_block_lot") or "").strip()
        addr = str(row.get("address") or "").strip()
        if not parcel or not addr:
            return None
        bldg = str(row.get("bldgclass") or "").strip().upper()
        if bldg and bldg[0] not in _RESIDENTIAL_CLASS_PREFIXES:
            return None  # filter to residential building classes
        owner = str(row.get("ownername") or "").strip()
        lot_area = to_float(row.get("lotarea"))
        building_area = to_float(row.get("bldgarea"))
        far_candidates = [
            to_float(row.get("residfar")),
            to_float(row.get("commfar")),
            to_float(row.get("facilfar")),
        ]
        max_far = max(far_candidates) if any(far_candidates) else 0.0
        buildable = lot_area * max_far
        latitude = to_float(row.get("latitude"))
        longitude = to_float(row.get("longitude"))
        districts = [
            str(row.get(key) or "").strip()
            for key in ("zonedist1", "zonedist2", "zonedist3", "zonedist4")
            if str(row.get(key) or "").strip()
        ]
        dataset_version = str(
            row.get("version")
            or row.get("plutoversion")
            or os.getenv("NY_PLUTO_VERSION", "current-socrata")
        )
        return PropertyRecord(
            parcel_id=parcel,
            address=addr,
            city=str(row.get("borough") or "New York").strip(),
            state=self.STATE,
            zip_code=str(row.get("zipcode") or "").strip()[:10],
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("assesstot")),
            equity_percent=0.0,
            # PLUTO carries no owner mailing address — absentee not derivable.
            is_absentee_owner=False,
            distress_flags=[],
            last_sale_date=None,
            zoning_district=districts[0] if districts else None,
            max_far=max_far or None,
            lot_area_sqft=lot_area or None,
            building_area_sqft=building_area or None,
            land_use=str(row.get("landuse") or "").strip() or None,
            air_rights_indicator=(buildable > building_area) if lot_area and max_far else None,
            latitude=latitude or None,
            longitude=longitude or None,
            dataset_version=dataset_version,
            source_metadata={
                "zoning_districts": districts,
                "built_far": to_float(row.get("builtfar")) or None,
                "residential_far": far_candidates[0] or None,
                "commercial_far": far_candidates[1] or None,
                "facility_far": far_candidates[2] or None,
                "lot_coverage": to_float(row.get("lotarea")) and (
                    to_float(row.get("bldgfront")) * to_float(row.get("bldgdepth")) / lot_area
                ) or None,
                "borough_block_lot": parcel,
                "effective_dataset_version": dataset_version,
            },
        )
