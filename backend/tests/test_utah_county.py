"""Contracts for the official Utah County assessor connector."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvesters.base import ArcGISHarvester  # noqa: E402
from harvesters.ut_utah_county import UtahCountyHarvester  # noqa: E402

_TENANT = "00000000-0000-0000-0000-000000000000"


def _row(**overrides) -> dict:
    row = {
        "OBJECTID": 7,
        "PARCELID": "347960171",
        "OWNER_NAME": "ARROWHEAD SPRINGS DEVELOPMENT LLC",
        "ACREAGE": 0.25,
        "SITE_FULL_ADDRESS": "1282 W 1290 NORTH, SALEM, UT",
        "TAX_CITY": "Salem",
        "PROP_TYPE_DESCR": "PUD",
        "SPC_PROP_TYP_DESCR": "Townhome",
        "GLA_WEIGHTED_YRBLT": 2025,
        "ASMT_YEAR": 2026,
        "ASMT_CODE": "R",
        "ASMT_CODE_DESCR": "RESIDENTIAL",
        "MKT_LAND_VALUE": 68000,
        "MKT_IMP_VALUE": 282000,
        "MKT_CUR_VALUE": 350000,
        "TXBL_CUR_VALUE": 350000,
        "TOT_CUR_TAXES": 2100.25,
        "TOT_PRV_TAXES": 2000.75,
        "YEARBLT_RES": 2025,
        "GLA_RES": 1729,
        "BATHROOMS_RES": 2.5,
        "GLA_BEDROOMS_RES": 3,
        "REVIEWED_DATE": 1776150000000,
    }
    row.update(overrides)
    return row


@pytest.fixture
def harvester() -> UtahCountyHarvester:
    return UtahCountyHarvester(tenant_id=_TENANT)


def test_uses_official_arcgis_query_endpoint():
    assert issubclass(UtahCountyHarvester, ArcGISHarvester)
    assert UtahCountyHarvester.SERVICE_URL.startswith(
        "https://maps.utahcounty.gov/arcgis/"
    )
    assert UtahCountyHarvester.SERVICE_URL.endswith("/MapServer/0/query")
    assert "opendata.utah.gov" not in UtahCountyHarvester.SERVICE_URL


def test_requests_every_mapped_source_field():
    for field in (
        "PARCELID",
        "OWNER_NAME",
        "SITE_FULL_ADDRESS",
        "TAX_CITY",
        "MKT_CUR_VALUE",
        "ACREAGE",
        "GLA_RES",
        "ASMT_YEAR",
        "GLA_BEDROOMS_RES",
        "BATHROOMS_RES",
    ):
        assert field in UtahCountyHarvester.OUT_FIELDS


def test_maps_detailed_source_backed_property_facts(harvester):
    record = harvester.map_record(_row())

    assert record is not None
    assert record.parcel_id == "347960171"
    assert record.address == "1282 W 1290 NORTH, SALEM, UT"
    assert record.city == "Salem"
    assert record.state == "UT"
    assert record.owner_name == "ARROWHEAD SPRINGS DEVELOPMENT LLC"
    assert record.owner_type == "corporate"
    assert record.estimated_value == 350000
    assert record.lot_area_sqft == 10890
    assert record.building_area_sqft == 1729
    assert record.land_use == "Townhome"
    assert record.dataset_version == "2026"
    assert record.source_metadata["bedrooms"] == 3
    assert record.source_metadata["bathrooms"] == 2.5
    assert record.source_metadata["market_land_value"] == 68000
    assert record.source_metadata["market_improvement_value"] == 282000


def test_missing_fields_remain_unknown_instead_of_inferred(harvester):
    record = harvester.map_record(_row())

    assert record is not None
    assert record.zip_code == ""
    assert record.equity_percent == 0.0
    assert record.is_absentee_owner is False
    assert record.last_sale_date is None
    assert record.distress_flags == []


def test_only_explicit_vacant_classification_adds_flag(harvester):
    record = harvester.map_record(
        _row(ASMT_CODE_DESCR="VACANT", SPC_PROP_TYP_DESCR="Vacant Land")
    )
    assert record is not None
    assert record.distress_flags == ["vacant_land"]


@pytest.mark.parametrize("field", ("PARCELID", "SITE_FULL_ADDRESS", "OWNER_NAME"))
def test_skips_records_missing_required_identity(field, harvester):
    assert harvester.map_record(_row(**{field: ""})) is None


def test_fetch_uses_bounded_arcgis_query(harvester):
    captured: list[str] = []

    async def fake_get_json(url: str, headers=None):
        captured.append(url)
        if not url.rstrip("/").split("?", 1)[0].endswith("/query"):
            return {"fields": []}
        return {
            "features": [{"attributes": _row()}],
            "exceededTransferLimit": True,
        }

    harvester._get_json = fake_get_json
    rows = asyncio.run(harvester.fetch_raw(max_records=1))

    assert rows == [_row()]
    assert len(captured) == 2
    query = parse_qs(urlparse(captured[1]).query)
    assert query["where"] == [UtahCountyHarvester.WHERE]
    assert query["outFields"] == [UtahCountyHarvester.OUT_FIELDS]
    assert query["returnGeometry"] == ["false"]
    assert query["resultRecordCount"] == ["1"]
