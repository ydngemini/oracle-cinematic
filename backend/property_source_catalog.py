"""The authorized property and market data sources, in one place (prompt 11,
T11.02; ledger L11-04).

Each source names where its data comes from, how it is configured and
refreshed, which identifiers it produces, where it writes, and what licence
or terms cover it. Two rules keep this honest:

* **Licence is evidence, not an assertion.** `licence` quotes what the repo
  itself states and where (`licence_ref`). Where the repo states nothing, the
  status is `owner_to_supply` — never a guessed licence. A harvester's
  `LICENSE_NAME` default ("municipal-open-data") is the code's own claim that
  nobody verified per portal, so it is reported as `default_unverified`.
* **Identifiers stay distinct** (T11.02): `property_id` (Neoh's own row),
  `parcel_id` (the jurisdiction's), `vendor_record_id` (the provider's) and
  `address_match` (a match by normalized address) are separate flags, never
  one "id".

The parcel harvesters are read from `harvesters.firehose.REGISTRY`, and the
market datasets from `market_research_agent._default_specs()`, so a new
source cannot be added without appearing here (tests/test_property_source_catalog.py).
MLS / IDX / RESO sources are out of scope (D-2026-10-12-01) and are not
catalogued as sources.

    python property_source_catalog.py --write-doc   # regenerates docs/property-source-inventory.md
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("public_records_harvester", "valuation_api", "market_data", "geocoder",
         "hazard_environment", "neighbourhood", "footprint", "broker_owned", "court_records",
         "licensed_parcel_api", "media")
LICENCE_STATUSES = ("stated_in_repo", "default_unverified", "owner_to_supply")
DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "property-source-inventory.md"


@dataclass(frozen=True)
class Identifiers:
    property_id: bool = False       # a Neoh row (leads / listings / public_property_records)
    parcel_id: bool = False         # the jurisdiction's parcel number
    vendor_record_id: bool = False  # the provider's own record id
    address_match: bool = False     # matched by normalized address only


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    fetch: str                      # module:line -> host or dataset
    env: tuple[str, ...]
    default_on: str                 # "yes" | "no" | "needs key" | "needs scheduler" (+ detail)
    freshness: str
    identifiers: Identifiers
    writes: str
    licence: str
    licence_ref: str
    licence_status: str
    notes: str = ""


_ID_NONE = Identifiers()

STATIC: tuple[Source, ...] = (
    Source("nyc_hpd_violations", "public_records_harvester",
           "harvesters/ny_hpd_violations.py:25 -> data.cityofnewyork.us wvxf-dwi5",
           ("ORACLE_DISTRESS_MAX_PER_SOURCE", "ORACLE_DISTRESS_INTERVAL_MIN"), "needs scheduler",
           "distress_scrape every 30 min (data_integrations/periodic.py:1055)",
           Identifiers(property_id=True, parcel_id=True, vendor_record_id=True),
           "leads, public_property_records, source_records", "municipal-open-data (base default)",
           "harvesters/base.py:1190", "default_unverified"),
    Source("chicago_building_violations", "public_records_harvester",
           "harvesters/il_chicago_violations.py:24 -> data.cityofchicago.org 22u3-xenr",
           ("ORACLE_DISTRESS_MAX_PER_SOURCE", "ORACLE_DISTRESS_INTERVAL_MIN"), "needs scheduler",
           "distress_scrape every 30 min", Identifiers(property_id=True, parcel_id=True, vendor_record_id=True),
           "leads, public_property_records, source_records", "municipal-open-data (base default)",
           "harvesters/base.py:1190", "default_unverified"),
    Source("county_assessor_de", "public_records_harvester",
           "harvesters/county_assessor.py:118 -> newcastlede.gov parcelview, recorder.delaware.gov",
           ("COUNTY_HARVEST_ENABLED",), "no", "polls every 300 s when on; cache 7 d",
           Identifiers(parcel_id=True), "in-memory PropertyGraph only (no table)",
           "", "docs/data-access-tiers.md:55", "owner_to_supply"),
    Source("rentcast_avm", "valuation_api", "avm_client.py:136 -> api.rentcast.io/v1",
           ("RENTCAST_API_KEY",), "needs key", "AVM cache 7 d (avm_client.py:357); outcome in provider_health",
           Identifiers(address_match=True), "di_cache (cache only)",
           "Redistribution of AVM values not granted", "docs/data-access-tiers.md:50", "stated_in_repo"),
    Source("attom_avm", "valuation_api", "avm_client.py:205 -> api.gateway.attomdata.com",
           ("ATTOM_API_KEY",), "needs key", "AVM cache 7 d; outcome in provider_health",
           Identifiers(address_match=True), "di_cache (cache only)",
           "Not yet live", "docs/data-access-tiers.md:51", "owner_to_supply"),
    Source("fred_rates", "market_data", "apis/market_data.py:58 -> api.stlouisfed.org", ("FRED_API_KEY",),
           "needs key", "cache 24 h", _ID_NONE, "none (served live)", "", "", "owner_to_supply"),
    Source("bls_laus", "market_data", "apis/market_data.py:196 -> api.bls.gov", ("BLS_API_KEY",),
           "yes (v1 keyless)", "cache 24 h", _ID_NONE, "none (served live)", "", "", "owner_to_supply"),
    Source("treasury_yields", "market_data", "apis/market_data.py:297 -> home.treasury.gov", (),
           "yes", "cache 24 h", _ID_NONE, "none (served live)", "", "", "owner_to_supply"),
    Source("state_market_projection", "market_data",
           "state_market_projection.py:68 <- public_market_metrics (redfin_market_tracker)", (),
           "with market_research", "inside the market_research task", _ID_NONE, "state_market_stats",
           "derived from redfin_market_tracker", "state_market_projection.py:65", "owner_to_supply"),
    Source("census_geocoder", "geocoder", "data_integrations/census_geocoder.py:205 -> geocoding.geo.census.gov",
           ("ORACLE_GEOCODE_*",), "yes", "property_coordinate_backfill hourly; cache 90 d",
           Identifiers(address_match=True), "public_property_records lat/lng",
           "public domain (US Census)", "docs/data-access-tiers.md:41", "stated_in_repo"),
    Source("nominatim_geocode", "geocoder", "apis/geocoding.py:40 -> nominatim.openstreetmap.org", (),
           "yes", "cache 90 d", Identifiers(address_match=True), "none",
           "~1 req/s, ODbL share-alike", "docs/data-access-tiers.md:52", "stated_in_repo"),
    Source("census_acs", "neighbourhood", "apis/census.py:53 -> api.census.gov", ("CENSUS_API_KEY",),
           "yes", "cache 365 d", _ID_NONE, "none", "public domain (US Census)",
           "docs/data-access-tiers.md:41", "stated_in_repo"),
    Source("enrich_property_overpass", "neighbourhood", "apis/property_data.py:36 -> Overpass API (OSM)", (),
           "yes", "cache 7 d (under the state_gis cache name)", _ID_NONE, "none", "", "", "owner_to_supply",
           notes="cached under the state_gis source name (apis/property_data.py:95)"),
    Source("fema_nfhl", "hazard_environment", "apis/property_data.py:37 -> FEMA NFHL", (), "yes",
           "cache 30 d", _ID_NONE, "none", "", "", "owner_to_supply"),
    Source("walk_score", "neighbourhood", "apis/property_data.py:255 -> Walk Score", ("WALKSCORE_API_KEY",),
           "needs key", "cache 30 d", _ID_NONE, "none", "", "", "owner_to_supply"),
    Source("openfema_disasters", "hazard_environment", "data_integrations/openfema.py:39 -> fema.gov/api/open",
           (), "yes", "cache 7 d", _ID_NONE, "none", "public (OpenFEMA)", "docs/data-access-tiers.md:42",
           "stated_in_repo"),
    Source("epa_envirofacts", "hazard_environment", "data_integrations/epa_envirofacts.py:72 -> data.epa.gov",
           (), "yes", "cache 7 d", _ID_NONE, "none", "public (EPA)", "docs/data-access-tiers.md:43",
           "stated_in_repo"),
    Source("fbi_crime", "neighbourhood", "data_integrations/fbi_crime.py:38 -> api.usa.gov/crime",
           ("DATA_GOV_API_KEY",), "yes (DEMO_KEY fallback)", "cache 7 d", _ID_NONE, "none",
           "Aggregate only", "docs/data-access-tiers.md:44", "stated_in_repo"),
    Source("eviction_lab", "neighbourhood", "data_integrations/eviction_lab.py:109 -> Eviction Lab S3", (),
           "yes", "cache 30 d", _ID_NONE, "none",
           "ODC-BY 1.0, commercial use permitted with attribution", "data_integrations/eviction_lab.py:6",
           "stated_in_repo"),
    Source("courtlistener", "court_records", "data_integrations/courtlistener.py:39 -> courtlistener.com v4",
           ("COURTLISTENER_TOKEN",), "needs key", "cache 6 h", _ID_NONE, "none", "",
           "docs/data-access-tiers.md:46", "owner_to_supply"),
    Source("regrid_parcel", "licensed_parcel_api", "data_integrations/regrid.py:75 -> app.regrid.com",
           ("REGRID_API_TOKEN",), "needs key", "cache 7 d", Identifiers(parcel_id=True, vendor_record_id=True),
           "none (query time)", "Query-time use only; model training and data sharing off",
           "data_integrations/regrid.py:9", "stated_in_repo"),
    Source("building_footprint", "footprint",
           "data_integrations/building_footprint.py:355 -> Regrid buildings, then OSM Overpass",
           ("REGRID_API_TOKEN", "ORACLE_OVERPASS_URL"), "yes (OSM)", "no cache found",
           Identifiers(address_match=True), "none (returned to caller)",
           "OSM: (c) OpenStreetMap contributors (ODbL); Regrid: licensed subscription",
           "data_integrations/building_footprint.py:59", "stated_in_repo"),
    Source("nces_school_district", "neighbourhood", "data_integrations/school_districts.py:17 -> NCES ArcGIS",
           (), "yes", "cache 180 d", _ID_NONE, "none", "public (NCES)", "docs/data-access-tiers.md:47",
           "stated_in_repo"),
    Source("property_imagery", "media", "data_integrations/property_imagery.py:99 -> Street View, Mapillary",
           ("GOOGLE_STREETVIEW_API_KEY", "MAPILLARY_TOKEN"), "needs key", "cache 30 d", _ID_NONE, "none",
           "attribution mandatory; Mapillary CC-BY-SA", "data_integrations/property_imagery.py:66",
           "stated_in_repo"),
    Source("crm_listings", "broker_owned", "crm.py:545 POST /api/crm/listings", (), "yes",
           "written by users", Identifiers(property_id=True, address_match=True),
           "listings, leads (parcel_id crm:<address>)", "the brokerage's own records", "crm.py:602",
           "stated_in_repo"),
    Source("property_media", "broker_owned", "media_api.py:283 POST /api/crm/{leads,listings}/{id}/media",
           ("ORACLE_STORAGE_BACKEND",), "yes", "uploads", Identifiers(property_id=True),
           "property_media + object storage", "the brokerage's own uploads", "media_api.py:226",
           "stated_in_repo"),
)


def _harvesters() -> list[Source]:
    from harvesters.base import BaseHarvester
    from harvesters.firehose import REGISTRY

    out = []
    for state, cls in sorted(REGISTRY.items()):
        licence = getattr(cls, "LICENSE_NAME", BaseHarvester.LICENSE_NAME)
        own = licence != BaseHarvester.LICENSE_NAME
        out.append(Source(
            f"state_parcels_{state.lower()}", "public_records_harvester",
            f"harvesters/firehose.py REGISTRY[{state!r}] -> {cls.__module__}.{cls.__name__}"
            f" ({getattr(cls, 'SOURCE_LABEL', '') or 'no label'})",
            ("ORACLE_INGEST_TENANT_ID", f"{state}_SOURCE_URL", "ORACLE_PARCEL_HARVEST_ENABLED"),
            "no (parcel_harvest off by default)", "parcel_harvest every 24 h when on; probe daily",
            Identifiers(property_id=True, parcel_id=True, vendor_record_id=True),
            "leads, public_property_records, harvest_sources", licence,
            f"{cls.__module__.replace('.', '/')}.py" if own else "harvesters/base.py:1190",
            "stated_in_repo" if own else "default_unverified",
            notes="outreach use not allowed" if not getattr(cls, "OUTREACH_USE_ALLOWED", False) else ""))
    return out


def _market_datasets() -> list[Source]:
    from market_research_agent import _default_specs

    return [Source(
        f"market_research_{spec.key}", "market_data", f"market_research_agent.py _default_specs: {spec.key}",
        ("ORACLE_MARKET_RESEARCH_ENABLED",), "yes", "market_research every 24 h",
        _ID_NONE, "public_market_metrics",
        f"attribution only: {getattr(spec, 'attribution', '') or 'none'}", "market_research_agent.py:432",
        # An attribution string is not a licence. The repo forbids scraping
        # Zillow/Redfin consumer pages (docs/data-access-tiers.md:32), which
        # this agent does not do (market_research_agent.py:3); the terms of
        # the published research files themselves are not recorded anywhere.
        "owner_to_supply",
        notes="published research file, not page scraping; file terms not recorded in the repo",
    ) for spec in _default_specs()]


def catalog() -> list[Source]:
    return sorted([*STATIC, *_harvesters(), *_market_datasets()], key=lambda s: (s.kind, s.id))


def to_markdown(sources: list[Source]) -> str:
    def ids(i: Identifiers) -> str:
        flags = [n for n in ("property_id", "parcel_id", "vendor_record_id", "address_match") if getattr(i, n)]
        return ", ".join(flags) or "none"

    owed = sum(1 for s in sources if s.licence_status != "stated_in_repo")
    lines = [
        "# Property and market data sources (T11.02)",
        "",
        "Generated by `python backend/property_source_catalog.py --write-doc`; do not edit by hand. "
        "Ledger: L11-04. MLS / IDX / RESO are out of scope (D-2026-10-12-01) and not listed.",
        "",
        f"{len(sources)} sources; licence or terms stated in the repo for {len(sources) - owed}, "
        f"**owed by the owner or legal for {owed}** (`default_unverified` = the harvester code's "
        "default 'municipal-open-data' claim, unchecked per portal; `owner_to_supply` = nothing in the repo).",
        "",
        "| id | kind | fetch | default | freshness | identifiers | writes | licence / terms | status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in sources:
        lic = f"{s.licence} ({s.licence_ref})" if s.licence_ref else (s.licence or "—")
        lines.append(f"| `{s.id}` | {s.kind} | {s.fetch} | {s.default_on} | {s.freshness} | {ids(s.identifiers)} "
                     f"| {s.writes} | {lic} | {s.licence_status} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--write-doc" in sys.argv:
        DOC_PATH.write_text(to_markdown(catalog()))
        print(f"wrote {DOC_PATH}")
    else:
        print(to_markdown(catalog()))
