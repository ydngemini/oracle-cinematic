"""Utah — Utah County Assessor parcel and assessment records.

Official source:
https://maps.utahcounty.gov/arcgis/rest/services/Assessor/
TaxParcelAll_NoLabel/MapServer/0

Scope: county:Utah (Provo, Orem, Lehi, Spanish Fork, American Fork, etc.).

The former ``opendata.utah.gov`` Socrata domain was decommissioned.  This
connector uses Utah County's own ArcGIS service and carries only source-backed
facts into the lead envelope.  The layer does not publish a situs ZIP, owner
mailing address, mortgage balance, or sale date, so those values remain
unknown instead of being inferred.
"""
from __future__ import annotations

from typing import Optional

from .base import ArcGISHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord


def _text(row: dict, field: str) -> str:
    return str(row.get(field) or "").strip()


class UtahCountyHarvester(ArcGISHarvester):
    STATE = "UT"
    SOURCE_KEY = "regional_parcels_ut"
    SOURCE_LABEL = "Utah County Assessor — TaxParcelAll"
    SERVICE_URL = (
        "https://maps.utahcounty.gov/arcgis/rest/services/"
        "Assessor/TaxParcelAll_NoLabel/MapServer/0/query"
    )
    WHERE = (
        "PARCELID IS NOT NULL AND SITE_FULL_ADDRESS IS NOT NULL "
        "AND OWNER_NAME IS NOT NULL"
    )
    OUT_FIELDS = ",".join(
        (
            "OBJECTID",
            "PARCELID",
            "OWNER_NAME",
            "ACREAGE",
            "SITE_FULL_ADDRESS",
            "TAX_CITY",
            "PROP_TYPE_DESCR",
            "SPC_PROP_TYP_DESCR",
            "GLA_WEIGHTED_YRBLT",
            "ASMT_YEAR",
            "ASMT_CODE",
            "ASMT_CODE_DESCR",
            "MKT_LAND_VALUE",
            "MKT_IMP_VALUE",
            "MKT_CUR_VALUE",
            "TXBL_CUR_VALUE",
            "TOT_CUR_TAXES",
            "TOT_PRV_TAXES",
            "YEARBLT_RES",
            "GLA_RES",
            "BATHROOMS_RES",
            "GLA_BEDROOMS_RES",
            "REVIEWED_DATE",
        )
    )

    def raw_property_key(self, row: dict) -> str:
        return _text(row, "PARCELID")

    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        parcel_id = _text(row, "PARCELID")
        address = _text(row, "SITE_FULL_ADDRESS")
        owner = _text(row, "OWNER_NAME")
        if not parcel_id or not address or not owner:
            return None

        acreage = to_float(row.get("ACREAGE"))
        building_area = to_float(row.get("GLA_RES"))
        if building_area <= 0:
            building_area = 0.0

        general_use = _text(row, "PROP_TYPE_DESCR")
        specific_use = _text(row, "SPC_PROP_TYP_DESCR")
        assessment_description = _text(row, "ASMT_CODE_DESCR")
        land_use = specific_use or general_use or None

        # This is a literal source classification, not a distress prediction.
        explicit_classifications = " ".join(
            (general_use, specific_use, assessment_description)
        ).upper()
        flags = ["vacant_land"] if "VACANT" in explicit_classifications else []

        metadata = {
            "assessment_year": row.get("ASMT_YEAR"),
            "assessment_code": _text(row, "ASMT_CODE") or None,
            "assessment_description": assessment_description or None,
            "market_land_value": row.get("MKT_LAND_VALUE"),
            "market_improvement_value": row.get("MKT_IMP_VALUE"),
            "taxable_current_value": row.get("TXBL_CUR_VALUE"),
            "current_taxes": row.get("TOT_CUR_TAXES"),
            "previous_taxes": row.get("TOT_PRV_TAXES"),
            "year_built": row.get("YEARBLT_RES"),
            "weighted_year_built": row.get("GLA_WEIGHTED_YRBLT"),
            "bedrooms": row.get("GLA_BEDROOMS_RES"),
            "bathrooms": row.get("BATHROOMS_RES"),
            "reviewed_date": row.get("REVIEWED_DATE"),
        }

        return PropertyRecord(
            parcel_id=parcel_id,
            address=address,
            city=_text(row, "TAX_CITY").title(),
            state=self.STATE,
            zip_code="",  # The official layer does not publish situs ZIP.
            owner_name=owner,
            owner_type=classify_owner(owner),
            estimated_value=to_float(row.get("MKT_CUR_VALUE")),
            equity_percent=0.0,  # No mortgage/debt balance is published.
            is_absentee_owner=False,  # No owner mailing address is published.
            distress_flags=flags,
            last_sale_date=None,
            bedrooms=to_float(row.get("GLA_BEDROOMS_RES")) or None,
            bathrooms=to_float(row.get("BATHROOMS_RES")) or None,
            year_built=(
                int(year)
                if (year := to_float(row.get("YEARBLT_RES"))) >= 1600
                else None
            ),
            lot_area_sqft=(acreage * 43_560) if acreage > 0 else None,
            building_area_sqft=building_area or None,
            land_use=land_use,
            dataset_version=_text(row, "ASMT_YEAR") or None,
            source_metadata=metadata,
        )
