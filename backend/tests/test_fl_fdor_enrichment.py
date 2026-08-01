"""Florida statewide and county public-record enrichment regressions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvesters.fl_fdor import FloridaFDORHarvester


STATEWIDE_ROW = {
    "PARCEL_ID": "18466-053-000",
    "CO_NO": 11,
    "CENSUS_BK": "120010019071",
    "OWN_NAME": "CARVER AARON ANDREW",
    "OWN_STATE": "FL",
    "PHY_ADDR1": "11380 NE 211TH TER",
    "PHY_CITY": "WALDO",
    "PHY_ZIPCD": 32694,
    "JV": 262530,
    "LND_SQFOOT": 43560,
    "EFF_YR_BLT": 2019,
    "ACT_YR_BLT": 2019,
    "TOT_LVG_AR": 1563,
    "DOR_UC": "001",
    "PA_UC": "00",
    "SALE_PRC1": 0,
    "SALE_YR1": 0,
    "SALE_MO1": " ",
}

COUNTY_ROW = {
    "parcel": "18466-053-000",
    "firstname1": "CARVER AARON ANDREW & ELIZABETH CHRISTINE",
    "address1": "11380 NE 211TH TER",
    "city": "WALDO",
    "zip": "32694-4286",
    "squarefeet": 1563,
    "heatedsquarefeet": 1563,
    "acres": 1,
    "justvalue": 262530,
    "impvalue": 228530,
    "propertyuse": "Single Family Residential",
    "p_category": "Single Family Residential",
    "puse": "0100",
    "taxyear": 2025,
    "zonedistrict": "PD",
    "zonedefin": "Planned Development (PD)",
    "zonecode": "0100PD",
    "saledate": None,
    "saleamount": None,
    "citydescription": "ALACHUA COUNTY",
}


def _harvester() -> FloridaFDORHarvester:
    return FloridaFDORHarvester(
        "11111111-1111-1111-1111-111111111111",
        agent_id="test-fl",
    )


def test_statewide_mapper_promotes_every_published_fdor_characteristic():
    record = _harvester().map_record(STATEWIDE_ROW)

    assert record is not None
    assert record.parcel_id == "18466-053-000"
    assert record.county == "Alachua"
    assert record.estimated_value == 262_530
    assert record.building_area_sqft == 1_563
    assert record.lot_area_sqft == 43_560
    assert record.year_built == 2019
    assert record.property_class == "001"
    assert record.last_sale_price is None
    assert record.last_sale_date is None
    assert record.bedrooms is None
    assert record.bathrooms is None
    assert record.rooms is None
    assert record.source_metadata["published_field_sources"] == {
        "county": "CENSUS_BK",
        "building_area_sqft": "TOT_LVG_AR",
        "lot_area_sqft": "LND_SQFOOT",
        "year_built": "ACT_YR_BLT",
        "property_class": "DOR_UC",
    }


def test_targeted_lookup_joins_official_county_detail(monkeypatch):
    harvester = _harvester()
    calls: list[tuple[str, str, str]] = []

    async def fake_query(service_url: str, *, where: str, out_fields: str):
        calls.append((service_url, where, out_fields))
        return [STATEWIDE_ROW] if "Florida_Statewide_Cadastral" in service_url else [COUNTY_ROW]

    monkeypatch.setattr(harvester, "_query_exact", fake_query)
    records = asyncio.run(harvester.lookup_parcel("18466-053-000"))

    assert len(calls) == 2
    assert calls[0][1] == "PARCEL_ID='18466-053-000'"
    assert calls[1][1] == "parcel='18466-053-000'"
    assert len(records) == 1
    record = records[0]
    assert record.owner_name == "CARVER AARON ANDREW & ELIZABETH CHRISTINE"
    assert record.county == "Alachua"
    assert record.building_area_sqft == 1_563
    assert record.lot_area_sqft == 43_560
    assert record.land_use == "Single Family Residential"
    assert record.zoning_district == "PD"
    assert record.dataset_version == "fdor:2025;county:2025"
    assert record.source_metadata["targeted_enrichment"] == {
        "completed": True,
        "statewide_checked": True,
        "county_detail_checked": True,
        "county_fips": "001",
    }
    assert record.source_metadata["county_record"]["improvement_value"] == 228_530


def test_lookup_escapes_a_single_quote_in_parcel_id(monkeypatch):
    harvester = _harvester()
    observed_where: list[str] = []

    async def fake_query(_service_url: str, *, where: str, out_fields: str):
        observed_where.append(where)
        return []

    monkeypatch.setattr(harvester, "_query_exact", fake_query)
    assert asyncio.run(harvester.lookup_parcel("ABC'123")) == []
    assert observed_where == ["PARCEL_ID='ABC''123'"]
