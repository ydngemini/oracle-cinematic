"""
data_coverage.py — National data-coverage map across the three data planes.

Single source of truth for the question "what data do we hold for every US
jurisdiction?" The platform pulls data on three planes, and only two of them
are per-jurisdiction (and therefore have a coverage gap worth tracking):

  1. Property / parcel   — per-state scrapers in ``harvesters/`` (the firehose).
                           Live for 50/51 jurisdictions (WY has no public REST
                           API); a few are county/city-anchored or geometry-only.
  2. Compliance          — per-state statutory rules in
                           ``compliance_engine/seed_data/``. Live for 50 states
                           + DC + federal.
  3. Market / demographic — national API integrations in ``data_integrations/``
                           (Census ACS, FEMA flood, school districts, USPS).
                           These are keyed by FIPS / address, so they already
                           cover every state and city; there is no per-state gap.

This module deliberately tracks only *honest, verifiable* status:
  * Property status is derived from a curated registry of harvesters that
    actually exist (reconciled against ``harvesters.firehose.REGISTRY`` by a
    unit test), plus the geographic SCOPE of each (some are city-only).
  * Compliance status is computed live by counting rules in the seed JSON.

It does NOT invent endpoint URLs for states we have not built yet — a missing
state is reported as MISSING with its portal platform noted only where that is
a matter of public record, so the next harvest batch knows where to look.

CLI:
    python -m backend.data_coverage            # human-readable report
    python -m backend.data_coverage --json     # machine-readable JSON
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_SEED_DIR = Path(__file__).parent / "compliance_engine" / "seed_data"

# --------------------------------------------------------------------------- #
# Jurisdictions — 50 states + DC. (Federal compliance is tracked separately.)
# --------------------------------------------------------------------------- #
US_JURISDICTIONS: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# --------------------------------------------------------------------------- #
# Property plane — the curated registry of harvesters that ACTUALLY exist.
# Reconciled against harvesters.firehose.REGISTRY by test_data_coverage.
#   (source_label, portal_type, scope)
# scope: "statewide" | "city:<name>" | "county:<name>" — the real geographic
# reach of the dataset, so "every city" gaps inside a live state stay visible.
# --------------------------------------------------------------------------- #
LIVE_PROPERTY: dict[str, tuple[str, str, str]] = {
    "DE": ("New Castle County parcels + ownership", "arcgis", "county:New Castle"),
    "MD": ("MD SDAT real property", "playwright", "statewide"),
    "PA": ("Philadelphia OPA assessments", "carto", "city:Philadelphia"),
    "NJ": ("NJ MOD-IV tax list", "arcgis", "statewide"),
    "NY": ("NYC PLUTO", "socrata", "city:New York City"),
    "VA": ("Virginia VGIN parcels (geometry only)", "arcgis", "statewide"),
    "WV": ("WV statewide parcels", "arcgis", "statewide"),
    "CT": ("CT real estate sales", "socrata", "statewide"),
    "MA": ("MassGIS standardized assessors", "arcgis", "statewide"),
    "NC": ("NC OneMap parcels", "arcgis", "statewide"),
    # National expansion — 40 jurisdictions added in the harvest-states program.
    "AK": ("FNSB tax parcels", "arcgis", "county:Fairbanks North Star Borough"),
    "AL": ("Jefferson County BOE parcels", "arcgis", "county:Jefferson"),
    "AR": ("AR GIS Office statewide parcels", "arcgis", "statewide"),
    "AZ": ("Maricopa County Assessor parcels", "arcgis", "county:Maricopa"),
    "CA": ("San Diego County parcels", "arcgis", "county:San Diego"),
    "CO": ("Colorado OIT public parcels", "arcgis", "statewide"),
    "DC": ("DC RPTA owner polygons", "arcgis", "statewide"),
    "FL": ("FL FDOR cadastral parcels", "arcgis", "statewide"),
    "GA": ("Fulton County tax parcels", "arcgis", "county:Fulton"),
    "HI": ("Honolulu HOLIS parcels", "arcgis", "county:Honolulu"),
    "IA": ("Linn County real estate parcels", "arcgis", "county:Linn"),
    "ID": ("Idaho IDL WhiteStar parcels", "arcgis", "statewide"),
    "IL": ("Cook County Assessor parcels", "socrata", "county:Cook"),
    "IN": ("IndyGIS Marion County parcels", "arcgis", "county:Marion"),
    "KS": ("Shawnee County Appraiser parcels", "arcgis", "county:Shawnee"),
    "KY": ("Boone County PVA tax parcels", "arcgis", "county:Boone"),
    "LA": ("East Baton Rouge assessor parcels", "arcgis", "county:East Baton Rouge"),
    "ME": ("Maine GeoLibrary parcels", "arcgis", "statewide"),
    "MI": ("Kent County equalization parcels", "arcgis", "county:Kent"),
    "MN": ("MN Geospatial Commons parcels", "arcgis", "statewide"),
    "MO": ("Jackson County parcels", "arcgis", "county:Jackson"),
    "MS": ("MARIS/MDEQ statewide parcels", "arcgis", "statewide"),
    "MT": ("MT DNRC cadastral parcels", "arcgis", "statewide"),
    "ND": ("NDGISHUB tax-roll parcels", "arcgis", "statewide"),
    "NE": ("Lancaster County tax parcels", "arcgis", "county:Lancaster"),
    "NH": ("NH GRANIT/DRA parcel mosaic", "arcgis", "statewide"),
    "NM": ("Bernalillo County assessor parcels", "arcgis", "county:Bernalillo"),
    "NV": ("Washoe County assessor parcels", "arcgis", "county:Washoe"),
    "OH": ("Franklin County Auditor CAMA", "arcgis", "county:Franklin"),
    "OK": ("Oklahoma County assessor parcels", "arcgis", "county:Oklahoma"),
    "OR": ("Marion County tax parcels", "arcgis", "county:Marion"),
    "RI": ("Providence CAMA parcels", "arcgis", "city:Providence"),
    "SC": ("Horry County GIS parcels", "arcgis", "county:Horry"),
    "SD": ("Pennington County tax parcels", "arcgis", "county:Pennington"),
    "TN": ("Shelby County assessor (Memphis)", "arcgis", "county:Shelby"),
    "TX": ("Bexar CAD parcels (San Antonio)", "arcgis", "county:Bexar"),
    "UT": ("Utah County assessor parcels", "socrata", "county:Utah"),
    "VT": ("VCGI statewide parcels", "arcgis", "statewide"),
    "WA": ("Snohomish County assessor parcels", "arcgis", "county:Snohomish"),
    "WI": ("WI SCO statewide parcels", "arcgis", "statewide"),
    "WY": ("WY statewide parcels (Property Tax Division)", "arcgis", "statewide"),
}

# Live harvesters whose SOURCE publishes parcel geometry + IDs but no owner,
# no assessed value, and no real street address (synthetic "PIN <id>" only).
# These run and count as "live" (a working harvester exists), but they yield
# nothing usable for lead-gen, so the coverage map flags them rather than
# implying owner/value completeness. VA's statewide VGIN service is the lone
# case after the harvest-states program — VA owner data is published only at
# the county/city level, which the user opted to leave unharvested for now.
GEOMETRY_ONLY: set[str] = {"VA"}

# Portal platform hints for states we have NOT built yet — public record only,
# used to plan the next harvest batch. Absence here means "research needed",
# never "no portal exists". Endpoints are intentionally omitted (unverified).
# As of 2026-06-27 there are NO remaining gaps: the harvest-states program plus
# the Wyoming harvester (WY Property Tax Division statewide-parcels ArcGIS
# FeatureServer — the earlier "web-app only, no REST API" note was wrong) bring
# coverage to all 50 states + DC. Kept as an empty extension point for new gaps.
PORTAL_HINTS: dict[str, str] = {}

# Market/demographic plane — national API integrations (no per-state gap).
NATIONAL_INTEGRATIONS: list[tuple[str, str]] = [
    ("Census ACS 5-yr", "demographics, median home value, income (FIPS-keyed)"),
    ("FEMA NFHL", "flood-zone designation by location"),
    ("School districts", "district boundaries + ratings by address"),
    ("USPS", "address standardization / ZIP+4"),
    ("Geocoder", "address ↔ lat/long ↔ FIPS"),
]


@dataclass
class JurisdictionCoverage:
    code: str
    name: str
    property_status: str          # "live" | "missing"
    property_source: Optional[str]
    property_scope: Optional[str]
    portal_hint: Optional[str]
    compliance_status: str        # "live" | "missing"
    compliance_rule_count: int
    property_geometry_only: bool = False  # live harvester but no owner/value/address

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "property": {
                "status": self.property_status,
                "source": self.property_source,
                "scope": self.property_scope,
                "geometry_only": self.property_geometry_only,
                "portal_hint": self.portal_hint,
            },
            "compliance": {
                "status": self.compliance_status,
                "rule_count": self.compliance_rule_count,
            },
        }


def _compliance_rule_counts() -> dict[str, int]:
    """Count rules per jurisdiction by reading the seed JSON live."""
    counts: dict[str, int] = {}
    if not _SEED_DIR.is_dir():
        return counts
    for path in _SEED_DIR.glob("*.json"):
        code = path.stem.upper()
        if code == "FEDERAL_RULES":
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            counts[code] = len(data) if isinstance(data, list) else 0
        except (json.JSONDecodeError, OSError):
            counts[code] = 0
    return counts


def national_status() -> list[JurisdictionCoverage]:
    """The full per-jurisdiction coverage matrix, sorted by state code."""
    compliance = _compliance_rule_counts()
    rows: list[JurisdictionCoverage] = []
    for code in sorted(US_JURISDICTIONS):
        name = US_JURISDICTIONS[code]
        prop = LIVE_PROPERTY.get(code)
        rule_count = compliance.get(code, 0)
        rows.append(JurisdictionCoverage(
            code=code,
            name=name,
            property_status="live" if prop else "missing",
            property_source=prop[0] if prop else None,
            property_scope=prop[2] if prop else None,
            portal_hint=None if prop else PORTAL_HINTS.get(code),
            compliance_status="live" if rule_count > 0 else "missing",
            compliance_rule_count=rule_count,
            property_geometry_only=bool(prop) and code in GEOMETRY_ONLY,
        ))
    return rows


def summary() -> dict:
    rows = national_status()
    total = len(rows)
    prop_live = sum(1 for r in rows if r.property_status == "live")
    prop_statewide = sum(
        1 for r in rows if r.property_status == "live" and r.property_scope == "statewide"
    )
    prop_geometry_only = sum(1 for r in rows if r.property_geometry_only)
    comp_live = sum(1 for r in rows if r.compliance_status == "live")
    comp_rules = sum(r.compliance_rule_count for r in rows)
    return {
        "jurisdictions": total,
        "property": {
            "live": prop_live,
            "live_statewide": prop_statewide,
            "city_scoped": prop_live - prop_statewide,
            "geometry_only": prop_geometry_only,
            "lead_usable": prop_live - prop_geometry_only,
            "missing": total - prop_live,
            "pct": round(100 * prop_live / total, 1),
        },
        "compliance": {
            "live": comp_live,
            "missing": total - comp_live,
            "total_rules": comp_rules,
            "pct": round(100 * comp_live / total, 1),
        },
        "market": {
            "plane": "national API (no per-state gap)",
            "integrations": len(NATIONAL_INTEGRATIONS),
        },
    }


def report_json() -> dict:
    return {
        "summary": summary(),
        "jurisdictions": [r.to_dict() for r in national_status()],
        "national_integrations": [
            {"name": n, "detail": d} for n, d in NATIONAL_INTEGRATIONS
        ],
    }


def report_markdown() -> str:
    rows = national_status()
    s = summary()
    out: list[str] = []
    out.append("# Oracle National Data Coverage\n")
    out.append(
        f"- **Property/parcel:** {s['property']['live']}/{s['jurisdictions']} live "
        f"({s['property']['live_statewide']} statewide, "
        f"{s['property']['city_scoped']} county/city-scoped; "
        f"{s['property']['lead_usable']} lead-usable, "
        f"{s['property']['geometry_only']} geometry-only) — {s['property']['pct']}%"
    )
    out.append(
        f"- **Compliance:** {s['compliance']['live']}/{s['jurisdictions']} live, "
        f"{s['compliance']['total_rules']} rules — {s['compliance']['pct']}%"
    )
    out.append(
        f"- **Market/demographic:** national API plane — "
        f"{s['market']['integrations']} integrations, no per-state gap\n"
    )
    out.append("| St | Jurisdiction | Property | Scope | Compliance | Next: portal hint |")
    out.append("|----|--------------|----------|-------|-----------|-------------------|")
    for r in rows:
        if r.property_status == "live":
            prop = "✅ " + (r.property_source or "")
            if r.property_geometry_only:
                prop = "⚠️ " + (r.property_source or "") + " — no owner/value"
        else:
            prop = "❌ missing"
        scope = r.property_scope or ""
        comp = f"✅ {r.compliance_rule_count}" if r.compliance_status == "live" else "❌"
        hint = r.portal_hint or ""
        out.append(f"| {r.code} | {r.name} | {prop} | {scope} | {comp} | {hint} |")
    out.append("\n## Market/demographic integrations (national)\n")
    for n, d in NATIONAL_INTEGRATIONS:
        out.append(f"- **{n}** — {d}")
    out.append("\n## Property gap (next harvest batches)\n")
    missing = [r for r in rows if r.property_status == "missing"]
    with_hint = [r for r in missing if r.portal_hint]
    out.append(
        f"{len(missing)} states need a property harvester. "
        f"{len(with_hint)} have a known portal to start from:"
    )
    for r in with_hint:
        out.append(f"  - {r.code} {r.name}: {r.portal_hint}")
    no_hint = [r.code for r in missing if not r.portal_hint]
    if no_hint:
        out.append(f"\nResearch needed (no portal noted yet): {', '.join(no_hint)}")
    # City-scoped live states still need statewide coverage.
    partial = [r for r in rows if r.property_status == "live" and r.property_scope != "statewide"]
    if partial:
        out.append("\n## City-scoped — statewide coverage still pending\n")
        for r in partial:
            out.append(f"  - {r.code}: currently {r.property_scope} ({r.property_source})")
    return "\n".join(out)


def _main() -> None:
    import sys
    if "--json" in sys.argv[1:]:
        print(json.dumps(report_json(), indent=2))
    else:
        print(report_markdown())


if __name__ == "__main__":
    _main()
