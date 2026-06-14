"""
data_coverage.py — National data-coverage map across the three data planes.

Single source of truth for the question "what data do we hold for every US
jurisdiction?" The platform pulls data on three planes, and only two of them
are per-jurisdiction (and therefore have a coverage gap worth tracking):

  1. Property / parcel   — per-state scrapers in ``harvesters/`` (the firehose).
                           Live for 10 states today; the rest are MISSING.
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
    "DE": ("Delaware FirstMap parcels", "arcgis", "statewide"),
    "MD": ("MD SDAT real property", "playwright", "statewide"),
    "PA": ("Philadelphia OPA assessments", "carto", "city:Philadelphia"),
    "NJ": ("NJ MOD-IV tax list", "arcgis", "statewide"),
    "NY": ("NYC PLUTO", "socrata", "city:New York City"),
    "VA": ("Virginia VGIN parcels", "arcgis", "statewide"),
    "WV": ("WV statewide parcels", "arcgis", "statewide"),
    "CT": ("CT real estate sales", "socrata", "statewide"),
    "MA": ("MassGIS standardized assessors", "arcgis", "statewide"),
    "NC": ("NC OneMap parcels", "arcgis", "statewide"),
}

# Portal platform hints for states we have NOT built yet — public record only,
# used to plan the next harvest batch. Absence here means "research needed",
# never "no portal exists". Endpoints are intentionally omitted (unverified).
PORTAL_HINTS: dict[str, str] = {
    "TX": "TNRIS / Texas stratmap statewide parcels",
    "FL": "FGIO Florida statewide parcels (FGDL)",
    "CA": "county assessors (no single statewide parcel layer)",
    "WA": "WA Geospatial Open Data / county assessors",
    "AZ": "AZGeo / Maricopa County Assessor",
    "OH": "Ohio county auditors (CAMA)",
    "IL": "Cook County + Illinois county GIS",
    "GA": "Georgia GIS Clearinghouse / qPublic",
    "CO": "Colorado county assessors",
    "MI": "Michigan county equalization / GIS",
    "MN": "MN Geospatial Commons parcels",
    "WI": "WI statewide parcel initiative (V&E)",
    "OR": "Oregon statewide parcel (ORMAP)",
    "TN": "TN county assessors (CoT GIS)",
    "MO": "Missouri county assessors",
}

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

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "property": {
                "status": self.property_status,
                "source": self.property_source,
                "scope": self.property_scope,
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
        ))
    return rows


def summary() -> dict:
    rows = national_status()
    total = len(rows)
    prop_live = sum(1 for r in rows if r.property_status == "live")
    prop_statewide = sum(
        1 for r in rows if r.property_status == "live" and r.property_scope == "statewide"
    )
    comp_live = sum(1 for r in rows if r.compliance_status == "live")
    comp_rules = sum(r.compliance_rule_count for r in rows)
    return {
        "jurisdictions": total,
        "property": {
            "live": prop_live,
            "live_statewide": prop_statewide,
            "city_scoped": prop_live - prop_statewide,
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
        f"{s['property']['city_scoped']} city-scoped) — {s['property']['pct']}%"
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
        prop = "✅ " + (r.property_source or "") if r.property_status == "live" else "❌ missing"
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
