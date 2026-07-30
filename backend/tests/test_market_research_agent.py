from __future__ import annotations

import gzip
from datetime import date

import pytest

from market_research_agent import (
    DatasetSpec,
    Download,
    MarketObservation,
    NationwideMarketResearchAgent,
    _download_sync,
    _download_hud_arcgis_sync,
    _latest_observations,
    parse_fhfa_state_hpi,
    parse_hud_fmr,
    parse_redfin_tracker,
    parse_zillow_zhvi,
)


def _download(body: bytes, url: str = "https://files.zillowstatic.com/test.csv") -> Download:
    return Download(url=url, body=body, sha256="a" * 64, updated_at=None)


def test_zillow_wide_parser_covers_state_and_dc_without_inventing_blanks():
    body = (
        b"RegionID,SizeRank,RegionName,RegionType,StateName,2026-05-31,2026-06-30\n"
        b"9,0,Illinois,state,,300000,301500\n"
        b"10,1,District of Columbia,state,,700000,\n"
    )
    spec = DatasetSpec("zillow_research_zhvi", "https://files.zillowstatic.com/test.csv",
                       parse_zillow_zhvi, "Zillow Research")
    rows = parse_zillow_zhvi(_download(body), spec)

    assert [(row.state_code, row.period_end.isoformat(), row.value) for row in rows] == [
        ("IL", "2026-05-31", 300000.0),
        ("IL", "2026-06-30", 301500.0),
        ("DC", "2026-05-31", 700000.0),
    ]
    assert all(row.metadata["classification"] == "aggregate_market_research" for row in rows)


def test_redfin_parser_keeps_only_state_all_residential_rows():
    body = (
        '"PERIOD_END"\t"REGION_TYPE"\t"STATE_CODE"\t"PROPERTY_TYPE"\t'
        '"MEDIAN_SALE_PRICE"\t"INVENTORY"\t"IS_SEASONALLY_ADJUSTED"\n'
        '"2026-06-30"\t"state"\t"IL"\t"All Residential"\t300000\t12000\ttrue\n'
        '"2026-06-30"\t"state"\t"IL"\t"Condo/Co-op"\t200000\t4000\ttrue\n'
    ).encode()
    spec = DatasetSpec("redfin_market_tracker", "https://redfin-public-data.s3.us-west-2.amazonaws.com/x.gz",
                       parse_redfin_tracker, "Redfin Data Center")
    rows = parse_redfin_tracker(
        _download(gzip.compress(body), spec.url),
        spec,
    )

    assert {(row.metric_key, row.value) for row in rows} == {
        ("median_sale_price", 300000.0),
        ("active_inventory", 12000.0),
    }


def test_fhfa_four_column_state_file_is_normalized_to_quarter_end():
    spec = DatasetSpec("fhfa_state_hpi", "https://www.fhfa.gov/test.csv",
                       parse_fhfa_state_hpi, "FHFA")
    rows = parse_fhfa_state_hpi(_download(b"IL,2026,1,321.5\nDC,2026,4,401.25\n", spec.url), spec)
    assert [(row.state_code, row.period_end.isoformat(), row.value) for row in rows] == [
        ("IL", "2026-03-31", 321.5),
        ("DC", "2026-12-31", 401.25),
    ]


def test_hud_feature_service_parser_covers_multistate_areas_and_dc():
    body = b"""{
      "features": [
        {"attributes": {"FMR_AREANAME": "Chicago-Joliet-Naperville, IL HUD Metro FMR Area", "FMR_2BDR": 1900}},
        {"attributes": {"FMR_AREANAME": "Washington-Arlington-Alexandria, DC-VA-MD HUD Metro FMR Area", "FMR_2BDR": 2246}},
        {"attributes": {"FMR_AREANAME": "Cook County, IL", "FMR_2BDR": 2100}}
      ]
    }"""
    spec = DatasetSpec(
        "hud_fair_market_rents",
        "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
        "Fair_Market_Rents/FeatureServer/0/query",
        parse_hud_fmr,
        "HUD",
        _download_hud_arcgis_sync,
    )

    rows = parse_hud_fmr(_download(body, spec.url), spec)

    assert {row.state_code for row in rows} == {"IL", "DC", "VA", "MD"}
    illinois = next(row for row in rows if row.state_code == "IL")
    assert illinois.value == 2000
    assert illinois.metadata["fiscal_year"] == 2026


def test_downloader_rejects_non_publisher_hosts_before_network_io():
    with pytest.raises(ValueError, match="allow-listed"):
        _download_sync("https://example.com/copied-zillow.csv")


def test_market_upsert_batch_size_is_bounded_for_command_timeout_safety(monkeypatch):
    monkeypatch.setenv("ORACLE_MARKET_DATA_UPSERT_BATCH_SIZE", "5000")
    assert NationwideMarketResearchAgent([])._upsert_batch_size == 500

    monkeypatch.setenv("ORACLE_MARKET_DATA_UPSERT_BATCH_SIZE", "invalid")
    assert NationwideMarketResearchAgent([])._upsert_batch_size == 100


def test_only_latest_observation_per_source_metric_and_state_is_persisted():
    older = MarketObservation(
        source_key="zillow_research_zhvi",
        metric_key="home_value_index",
        state_code="IL",
        geography_name="Illinois",
        period_end=date(2026, 5, 31),
        value=300_000,
        unit="usd",
        source_url="https://files.zillowstatic.com/test.csv",
        dataset_sha256="a" * 64,
        dataset_updated_at=None,
        metadata={},
    )
    newer = MarketObservation(
        **{
            **older.__dict__,
            "period_end": date(2026, 6, 30),
            "value": 301_500,
        }
    )
    other_state = MarketObservation(
        **{
            **newer.__dict__,
            "state_code": "DC",
            "geography_name": "District of Columbia",
            "value": 700_000,
        }
    )

    rows = _latest_observations([newer, older, other_state])

    assert {(row.state_code, row.period_end, row.value) for row in rows} == {
        ("IL", newer.period_end, 301_500),
        ("DC", other_state.period_end, 700_000),
    }
