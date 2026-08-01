"""
Harvester spine — shared machinery for the 10-state firehose around Delaware.

Every state harvester is a *real* scraper hitting that state's actual open-data
endpoint, but they all share this base: a polite rate limiter, exponential
backoff that respects HTTP 429/503, JSON fetch over stdlib urllib (run in a
thread so it never blocks the loop — no aiohttp/httpx dependency), unified
mapping helpers, and one batched INSERT into the RLS-scoped `leads` table.

Three source archetypes cover every state portal we touch:

  * SocrataHarvester — `{base}.json?$limit&$offset`            (MD-alt, NY, CT)
  * ArcGISHarvester  — `{service}/query?...&resultOffset`      (DE, NJ, VA, WV, MA, NC)
  * CartoHarvester   — `{domain}/api/v2/sql?q=SELECT ... LIMIT` (PA / Philadelphia)

A concrete state subclasses the right archetype, sets its endpoint constant(s),
and implements `map_record(row) -> PropertyRecord | None` against that dataset's
real column names. `tenancy` / `db.connection` (→ FastAPI) are imported lazily so
these modules stay importable in a bare scraper environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Optional

from .property_adapter import PropertyRecord

logger = logging.getLogger("oracle.harvester")

# Politeness / resilience defaults (env-overridable, shared across all states).
MIN_REQUEST_INTERVAL = float(os.getenv("FIREHOSE_MIN_INTERVAL", "1.5"))
REQUEST_JITTER = float(os.getenv("FIREHOSE_JITTER", "0.8"))
MAX_RETRIES = int(os.getenv("FIREHOSE_MAX_RETRIES", "5"))
BASE_BACKOFF = float(os.getenv("FIREHOSE_BASE_BACKOFF", "3.0"))
MAX_BACKOFF = float(os.getenv("FIREHOSE_MAX_BACKOFF", "120.0"))
HTTP_TIMEOUT = int(os.getenv("FIREHOSE_HTTP_TIMEOUT", "45"))
PAGE_SIZE = int(os.getenv("FIREHOSE_PAGE_SIZE", "1000"))
BATCH_SIZE = int(os.getenv("FIREHOSE_BATCH_SIZE", "500"))
USER_AGENT = os.getenv(
    "FIREHOSE_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) Oracle-Firehose/1.0 (+research)",
)

_CORP_TOKENS = ("llc", "l.l.c", "inc", "incorporated", "corp", "co.", "company",
                "lp", "l.p", "partners", "holdings", "properties", "group", "ventures",
                "associates", "realty", "investments", "capital", "enterprises")
_TRUST_TOKENS = ("trust", "trustee", "estate of", "living trust", "family trust")
_SPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

# Versioned, provenance-first envelope for public property records.  A value is
# only exposed when the source actually published it; this is deliberately not
# an enrichment or prediction schema.
LEAD_PAYLOAD_SCHEMA_VERSION = 3
_DETAIL_FIELDS = (
    "address",
    "zip_code",
    "county",
    "owner_name",
    "public_record_value",
    "last_sale_price",
    "reported_record_date",
    "bedrooms",
    "bathrooms",
    "rooms",
    "year_built",
    "property_class",
    "zoning_district",
    "land_use",
    "lot_area_sqft",
    "building_area_sqft",
    "coordinates",
)

# Normalized, unambiguous names commonly published by assessor/CAMA portals.
# ArcGIS field discovery uses this allow-list to request additional facts
# without switching every source to the much heavier ``outFields=*`` response.
_PUBLIC_CHARACTERISTIC_ALIASES: dict[str, tuple[str, ...]] = {
    "county": (
        "county", "countyname", "county_name", "cntyname", "co_name",
    ),
    "bedrooms": (
        "bedrooms", "bedroom", "beds", "bed_rooms", "bdrms",
        "total_bedrooms", "number_of_bedrooms", "bedroom_count",
        "bedrooms_res", "gla_bedrooms_res", "char_beds", "char_bedrooms",
    ),
    "bathrooms": (
        "bathrooms", "bathroom", "baths", "total_bathrooms",
        "number_of_bathrooms", "bathroom_count", "bathrooms_res",
    ),
    "bathrooms_full": (
        "full_baths", "fullbath", "full_bath", "full_bathrooms",
        "number_of_full_baths", "full_bath_count", "char_fbath",
        "char_full_baths", "fbaths",
    ),
    "bathrooms_half": (
        "half_baths", "halfbath", "half_bath", "half_bathrooms",
        "number_of_half_baths", "half_bath_count", "char_hbath",
        "char_half_baths", "hbaths",
    ),
    "rooms": (
        "rooms", "total_rooms", "number_of_rooms", "room_count", "char_rooms",
    ),
    "year_built": (
        "year_built", "yearbuilt", "yr_built", "yrbuilt", "year_bluilt",
        "act_yr_blt", "eff_yr_blt", "yearblt_res", "char_yrblt",
        "construction_year",
    ),
    "property_class": (
        "property_class", "propertyclass", "propclass", "class",
        "bldgclass", "building_class", "assessment_class", "dor_uc", "pa_uc",
    ),
    "last_sale_price": (
        "last_sale_price", "sale_price", "saleprice", "sale_amount",
        "saleamount", "consideration", "lastsaleprice", "sale_prc1",
        "sales_price", "last_sales_price",
    ),
    "building_area_sqft": (
        "building_area_sqft", "building_sqft", "buildingarea",
        "bldgarea", "bldg_sf", "living_area", "livingarea",
        "gla_res", "char_bldg_sf", "heated_area", "total_living_area",
        "total_livable_area", "gross_living_area", "finished_area",
        "square_feet", "sqft", "unit_sf", "tot_lvg_ar", "heatedsquarefeet",
    ),
    "lot_area_sqft": (
        "lot_area_sqft", "lot_sqft", "lotsqft", "lotarea",
        "land_sqft", "landarea", "char_land_sf", "lnd_sqfoot",
    ),
    "lot_acres": (
        "lot_acres", "lotacres", "acreage", "acres",
    ),
    "land_use": (
        "land_use", "landuse", "property_use", "use_description",
        "property_type", "char_use", "prop_type_descr", "p_category",
    ),
}
_PUBLIC_CHARACTERISTIC_NAMES = {
    norm_name
    for aliases in _PUBLIC_CHARACTERISTIC_ALIASES.values()
    for alias in aliases
    if (norm_name := "".join(ch for ch in alias.lower() if ch.isalnum()))
}
_MUNICIPAL_SOURCE_SCOPES = {
    "chicago_building_violations": "city:Chicago",
    "nyc_hpd_violations": "city:New York City",
}


class Retryable(Exception):
    """Explicitly-retryable condition (HTTP 429/503). Carries an optional hint."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or "urlerror" in name:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("timed out", "timeout", "reset", "temporarily", "refused"))


def to_float(value) -> float:
    if value is None:
        return 0.0
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def classify_owner(name: str) -> str:
    low = (name or "").lower()
    if any(tok in low for tok in _TRUST_TOKENS):
        return "trust"
    if any(tok in low for tok in _CORP_TOKENS):
        return "corporate"
    return "individual"


def norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _raw_field_index(row: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Index raw public fields case/punctuation-insensitively."""
    return {
        norm(key): (str(key), value)
        for key, value in row.items()
        if key is not None and value not in (None, "")
    }


def _first_raw(
    index: dict[str, tuple[str, Any]],
    aliases: tuple[str, ...],
) -> tuple[Optional[str], Any]:
    for alias in aliases:
        match = index.get(norm(alias))
        if match is not None:
            return match
    return None, None


def promote_public_characteristics(
    record: PropertyRecord,
    row: dict[str, Any],
) -> PropertyRecord:
    """Promote source-published characteristics for every jurisdiction.

    Every ArcGIS/Socrata/CARTO adapter passes its raw public row through this
    function after its source-specific mapper. Only explicit, allow-listed
    fields are promoted. Values are never inferred from address, price, class,
    or another proxy.
    """
    if not isinstance(row, dict):
        return record
    index = _raw_field_index(row)
    field_sources: dict[str, str] = {}

    def number(field: str, *, minimum: float = 0.0, maximum: Optional[float] = None):
        key, value = _first_raw(index, _PUBLIC_CHARACTERISTIC_ALIASES[field])
        cleaned = _clean_number(value, minimum=minimum, maximum=maximum)
        if cleaned is not None and key:
            field_sources[field] = key
        return cleaned

    def text_value(field: str, *, max_length: int = 160):
        key, value = _first_raw(index, _PUBLIC_CHARACTERISTIC_ALIASES[field])
        cleaned = _clean_text(value, max_length=max_length)
        if cleaned and key:
            field_sources[field] = key
        return cleaned

    bedrooms = record.bedrooms
    if bedrooms is None:
        bedrooms = number("bedrooms", maximum=100.0)

    bathrooms = record.bathrooms
    full_baths = number("bathrooms_full", maximum=100.0)
    half_baths = number("bathrooms_half", maximum=100.0)
    if bathrooms is None:
        bathrooms = number("bathrooms", maximum=100.0)
    if bathrooms is None and (full_baths is not None or half_baths is not None):
        bathrooms = (full_baths or 0.0) + (half_baths or 0.0) * 0.5
        field_sources["bathrooms"] = ",".join(
            source
            for source in (
                field_sources.get("bathrooms_full"),
                field_sources.get("bathrooms_half"),
            )
            if source
        )

    rooms = record.rooms
    if rooms is None:
        rooms = number("rooms", maximum=500.0)

    year_built = record.year_built
    if year_built is None:
        raw_year = number(
            "year_built",
            minimum=1600.0,
            maximum=float(datetime.now(timezone.utc).year + 1),
        )
        year_built = int(raw_year) if raw_year is not None else None

    last_sale_price = record.last_sale_price
    if last_sale_price is None:
        last_sale_price = number("last_sale_price", minimum=0.01)

    building_area = record.building_area_sqft
    if building_area is None:
        building_area = number("building_area_sqft", minimum=0.01)

    lot_area = record.lot_area_sqft
    if lot_area is None:
        lot_area = number("lot_area_sqft", minimum=0.01)
    if lot_area is None:
        acres = number("lot_acres", minimum=0.000001)
        if acres is not None:
            lot_area = acres * 43_560.0
            field_sources["lot_area_sqft"] = field_sources.pop("lot_acres")

    county = record.county or text_value("county")
    property_class = record.property_class or text_value("property_class", max_length=96)
    land_use = record.land_use or text_value("land_use")

    metadata = dict(record.source_metadata or {})
    if field_sources:
        prior_sources = metadata.get("published_field_sources")
        metadata["published_field_sources"] = {
            **(prior_sources if isinstance(prior_sources, dict) else {}),
            **field_sources,
        }
    if full_baths is not None:
        metadata.setdefault("bathrooms_full", full_baths)
    if half_baths is not None:
        metadata.setdefault("bathrooms_half", half_baths)

    record.county = county
    record.bedrooms = bedrooms
    record.bathrooms = bathrooms
    record.rooms = rooms
    record.year_built = year_built
    record.property_class = property_class
    record.last_sale_price = last_sale_price
    record.building_area_sqft = building_area
    record.lot_area_sqft = lot_area
    record.land_use = land_use
    record.source_metadata = metadata
    return record


def _clean_text(value: Any, *, max_length: int = 512) -> Optional[str]:
    """Normalize display data without manufacturing a replacement value."""
    if value is None:
        return None
    text = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", str(value))).strip()
    return text[:max_length] or None


def _clean_number(
    value: Any,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Optional[float]:
    """Return a finite, in-range public value or ``None`` when it is unknown."""
    if isinstance(value, bool) or value is None:
        return None
    candidate: Any = value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate or candidate.lower() in {
            "-", "--", "n/a", "na", "none", "null", "not published",
        }:
            return None
        is_parenthesized = candidate.startswith("(") and candidate.endswith(")")
        if is_parenthesized:
            candidate = candidate[1:-1]
        candidate = candidate.replace("$", "").replace(",", "").strip()
        if is_parenthesized:
            candidate = f"-{candidate}"
    try:
        number = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _clean_iso_date(value: Any) -> Optional[str]:
    """Keep only unambiguous ISO calendar dates from source records."""
    text = _clean_text(value, max_length=32)
    if not text:
        return None
    candidate = text.split("T", 1)[0]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _clean_flags(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    cleaned = {
        _clean_text(value, max_length=80).lower().replace(" ", "_")
        for value in values
        if _clean_text(value, max_length=80)
    }
    return sorted(cleaned)[:32]


def _clean_source_metadata(value: Any, *, depth: int = 0) -> Any:
    """Bound source annotations so a malformed feed cannot bloat lead payloads."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:64]:
            key = _clean_text(raw_key, max_length=96)
            if not key:
                continue
            clean = _clean_source_metadata(raw_value, depth=depth + 1)
            if clean is not None:
                out[key] = clean
        return out
    if isinstance(value, (list, tuple)):
        return [clean for item in value[:32]
                if (clean := _clean_source_metadata(item, depth=depth + 1)) is not None]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _clean_number(value)
    return _clean_text(value, max_length=512)


def _source_context(
    state: str,
    fallback_source: str,
    source_key: str,
) -> tuple[str, str]:
    """Resolve the declared public source and its honest geographic scope."""
    if source_key in _MUNICIPAL_SOURCE_SCOPES:
        return fallback_source or "Municipal public record", _MUNICIPAL_SOURCE_SCOPES[source_key]
    try:
        from data_coverage import LIVE_PROPERTY

        declared = LIVE_PROPERTY.get(state.upper())
        if declared:
            return declared[0], declared[2]
    except Exception:  # noqa: BLE001 - ingestion must not depend on the status page
        pass
    return fallback_source or "Public property record", "source scope not declared"


def _is_placeholder_address(address: Optional[str], parcel_id: Optional[str]) -> bool:
    if not address or not parcel_id:
        return False
    normalized_address = address.upper()
    normalized_parcel = parcel_id.upper()
    return normalized_address in {
        normalized_parcel,
        f"PIN {normalized_parcel}",
        f"PARCEL {normalized_parcel}",
        f"PARCEL ID {normalized_parcel}",
    }


def build_clean_lead_payload(
    record: PropertyRecord | dict[str, Any],
    *,
    state: Optional[str] = None,
    source_label: str = "",
    source_key: Optional[str] = None,
    refreshed_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the public-property lead envelope without inferring missing facts.

    The function is intentionally shared by new harvests and the one-time
    legacy cleanup task.  It carries source-provided details forward, removes
    malformed values, and makes missing fields visible to the UI instead of
    filling them with plausible-looking defaults.
    """
    raw = asdict(record) if isinstance(record, PropertyRecord) else dict(record or {})
    parcel_id = _clean_text(raw.get("parcel_id"), max_length=240)
    record_state = _clean_text(state or raw.get("state"), max_length=2)
    if not parcel_id or not record_state:
        raise ValueError("public lead payload requires parcel_id and state")
    record_state = record_state.upper()

    address = _clean_text(raw.get("address"), max_length=320)
    if _is_placeholder_address(address, parcel_id):
        address = None
    owner_name = _clean_text(raw.get("owner_name"), max_length=320)
    owner_type = _clean_text(raw.get("owner_type"), max_length=24)
    if owner_type not in {"individual", "corporate", "trust"}:
        owner_type = classify_owner(owner_name or "") if owner_name else None

    estimated_value = _clean_number(raw.get("estimated_value"), minimum=0.01)
    # Harvesters use 0.0 when a public assessor feed does not publish equity;
    # do not misrepresent that sentinel as verified zero equity.
    equity_percent = _clean_number(raw.get("equity_percent"), minimum=0.01, maximum=100.0)
    latitude = _clean_number(raw.get("latitude"), minimum=-90.0, maximum=90.0)
    longitude = _clean_number(raw.get("longitude"), minimum=-180.0, maximum=180.0)
    if latitude is None or longitude is None:
        latitude = longitude = None

    source_key = _clean_text(source_key or f"firehose:{record_state}", max_length=160)
    source_name, source_scope = _source_context(
        record_state,
        source_label,
        source_key or f"firehose:{record_state}",
    )
    refreshed = refreshed_at or datetime.now(timezone.utc)
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)

    public_fields = {
        "parcel_id": parcel_id,
        "address": address,
        "city": _clean_text(raw.get("city"), max_length=160),
        "state": record_state,
        "zip_code": _clean_text(raw.get("zip_code"), max_length=16),
        "county": (
            _clean_text(raw.get("county"), max_length=160)
            or (
                source_scope.split(":", 1)[1]
                if source_scope.startswith("county:") else None
            )
        ),
        "owner_name": owner_name,
        "owner_type": owner_type,
        "estimated_value": estimated_value,
        "last_sale_price": _clean_number(raw.get("last_sale_price"), minimum=0.01),
        "bedrooms": _clean_number(raw.get("bedrooms"), minimum=0.0, maximum=100.0),
        "bathrooms": _clean_number(raw.get("bathrooms"), minimum=0.0, maximum=100.0),
        "rooms": _clean_number(raw.get("rooms"), minimum=0.0, maximum=500.0),
        "year_built": (
            int(year)
            if (
                year := _clean_number(
                    raw.get("year_built"),
                    minimum=1600.0,
                    maximum=float(datetime.now(timezone.utc).year + 1),
                )
            ) is not None
            else None
        ),
        "property_class": _clean_text(raw.get("property_class"), max_length=96),
        "equity_percent": equity_percent,
        "is_absentee_owner": bool(raw.get("is_absentee_owner")) if raw.get("is_absentee_owner") is True else None,
        "distress_flags": _clean_flags(raw.get("distress_flags")),
        # Some portals publish a recorder/deed/sale date while others expose a
        # maintenance date.  The UI calls this a reported record date and asks
        # for verification rather than calling every value a sale date.
        "last_sale_date": _clean_iso_date(raw.get("last_sale_date")),
        "zoning_district": _clean_text(raw.get("zoning_district"), max_length=96),
        "max_far": _clean_number(raw.get("max_far"), minimum=0.0, maximum=100.0),
        "lot_area_sqft": _clean_number(raw.get("lot_area_sqft"), minimum=0.01),
        "building_area_sqft": _clean_number(raw.get("building_area_sqft"), minimum=0.01),
        "land_use": _clean_text(raw.get("land_use"), max_length=160),
        "air_rights_indicator": raw.get("air_rights_indicator")
            if isinstance(raw.get("air_rights_indicator"), bool) else None,
        "latitude": latitude,
        "longitude": longitude,
        "dataset_version": _clean_text(raw.get("dataset_version"), max_length=160),
        "source_metadata": _clean_source_metadata(raw.get("source_metadata") or {}),
    }
    observed = {"parcel_id", "state"}
    if address:
        observed.add("address")
    if public_fields["zip_code"]:
        observed.add("zip_code")
    if public_fields["county"]:
        observed.add("county")
    if owner_name:
        observed.update({"owner_name", "owner_type"})
    if estimated_value is not None:
        observed.add("public_record_value")
    if public_fields["last_sale_price"] is not None:
        observed.add("last_sale_price")
    if public_fields["last_sale_date"]:
        observed.add("reported_record_date")
    for characteristic in (
        "bedrooms",
        "bathrooms",
        "rooms",
        "year_built",
        "property_class",
    ):
        if public_fields[characteristic] is not None:
            observed.add(characteristic)
    if public_fields["zoning_district"]:
        observed.add("zoning_district")
    if public_fields["land_use"]:
        observed.add("land_use")
    if public_fields["lot_area_sqft"] is not None:
        observed.add("lot_area_sqft")
    if public_fields["building_area_sqft"] is not None:
        observed.add("building_area_sqft")
    if latitude is not None:
        observed.add("coordinates")

    optional_observed = len(observed - {"parcel_id", "state"})
    detail_level = "comprehensive" if optional_observed >= 7 else (
        "standard" if optional_observed >= 4 else "limited"
    )
    public_fields.update({
        "schema_version": LEAD_PAYLOAD_SCHEMA_VERSION,
        # Retain the legacy key so existing graph consumers remain compatible.
        "source": source_key,
        "provenance": {
            "source_key": source_key,
            "source_name": source_name,
            "coverage_scope": source_scope,
            "data_classification": "public_property_record",
            "record_refreshed_at": refreshed.astimezone(timezone.utc).isoformat(),
            "dataset_version": public_fields["dataset_version"],
        },
        "data_quality": {
            "detail_level": detail_level,
            "observed_fields": sorted(observed),
            "unavailable_fields": [field for field in _DETAIL_FIELDS if field not in observed],
            "address_was_not_published": raw.get("address") is not None and address is None,
            "public_record_only": True,
            "verification_required": True,
        },
    })
    return public_fields


def _payload_refreshed_at(payload: dict[str, Any]) -> datetime:
    """Return the provenance timestamp as an asyncpg-compatible value.

    The public payload intentionally serializes provenance as ISO text for
    JSON consumers. Catalog writes bind directly to a ``timestamptz`` column,
    however, and asyncpg requires a real ``datetime`` even when the SQL
    placeholder has an explicit cast.
    """
    raw = payload.get("provenance", {}).get("record_refreshed_at")
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise ValueError("public lead payload is missing record_refreshed_at")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _harvest_underwriting(payload: dict[str, Any], source_key: str) -> dict[str, Any]:
    """Keep public-record values distinct from calculated underwriting figures."""
    return {
        "estimated_value": payload.get("estimated_value"),
        "equity_percent": payload.get("equity_percent"),
        "source": source_key,
        "valuation_basis": "public_record_reported",
    }


def motivation_score(rec: PropertyRecord) -> int:
    """Cheap 0–100 prior from raw assessment signal. Scout's underwriter replaces
    this once ARV / rehab / MAO are computed downstream."""
    score = min(len(rec.distress_flags) * 22, 55)
    if rec.is_absentee_owner:
        score += 22
    score += int(max(0.0, min(rec.equity_percent, 100.0)) * 0.23)  # up to ~23
    if rec.owner_type in ("corporate", "trust"):
        score += 5
    return max(0, min(score, 100))


class RateLimiter:
    """Min-interval throttle with symmetric jitter, shared by a firehose run so
    10 states don't collectively hammer any one host."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL, jitter: float = REQUEST_JITTER):
        self._min_interval = min_interval
        self._jitter = jitter
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = (self._last + self._min_interval) - time.monotonic()
            wait += random.uniform(0, self._jitter)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# Leads persistence — one place, every state.
# --------------------------------------------------------------------------- #
async def persist_leads(tenant_id: str, agent_id: str, records: list[PropertyRecord], *, metrics: dict) -> int:
    if not records:
        return 0

    # Lazy: these transitively import FastAPI. tenant_tx applies the RLS GUCs.
    from tenancy import TenantContext, Role
    from db.connection import tenant_tx

    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    # Idempotent upsert: a recurring harvest re-sees the same parcels every cycle,
    # so refresh the row in place instead of accumulating a duplicate lead per run.
    # Requires UNIQUE (tenant_id, parcel_id) from migration 0018.
    sql = (
        "INSERT INTO leads (tenant_id, parcel_id, state, motivation_score, underwriting, payload) "
        "VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb) "
        "ON CONFLICT (tenant_id, parcel_id) DO UPDATE SET "
        "motivation_score = EXCLUDED.motivation_score, "
        "underwriting = EXCLUDED.underwriting, "
        "payload = EXCLUDED.payload, "
        "updated_at = now()"
    )
    public_sql = """
        INSERT INTO public_property_records (
            source_key, source_record_id, parcel_id, state, county, city,
            zip_code, address, owner_name, owner_type, public_record_value,
            last_sale_price, reported_record_date, bedrooms, bathrooms, rooms,
            year_built, property_class, zoning_district, land_use, lot_area_sqft,
            building_area_sqft, latitude, longitude, source_name,
            coverage_scope, detail_level, observed_fields,
            verification_required, record_refreshed_at, dataset_version,
            source_metadata
        )
        VALUES (
            $1, $2, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, public_record_date_or_null($12::text), $13, $14, $15,
            $16, $17, $18, $19, $20, $21, $22, $23, $24,
            $25, $26, $27::text[], true, $28::timestamptz, $29, $30::jsonb
        )
        ON CONFLICT (source_key, state, source_record_id) DO UPDATE SET
            parcel_id = COALESCE(
                NULLIF(EXCLUDED.parcel_id, ''),
                public_property_records.parcel_id
            ),
            county = COALESCE(
                NULLIF(EXCLUDED.county, ''),
                public_property_records.county
            ),
            city = COALESCE(NULLIF(EXCLUDED.city, ''), public_property_records.city),
            zip_code = COALESCE(
                NULLIF(EXCLUDED.zip_code, ''),
                public_property_records.zip_code
            ),
            address = COALESCE(
                NULLIF(EXCLUDED.address, ''),
                public_property_records.address
            ),
            owner_name = COALESCE(
                NULLIF(EXCLUDED.owner_name, ''),
                public_property_records.owner_name
            ),
            owner_type = COALESCE(
                NULLIF(EXCLUDED.owner_type, ''),
                public_property_records.owner_type
            ),
            public_record_value = COALESCE(
                EXCLUDED.public_record_value,
                public_property_records.public_record_value
            ),
            last_sale_price = COALESCE(
                EXCLUDED.last_sale_price,
                public_property_records.last_sale_price
            ),
            reported_record_date = COALESCE(
                EXCLUDED.reported_record_date,
                public_property_records.reported_record_date
            ),
            bedrooms = COALESCE(EXCLUDED.bedrooms, public_property_records.bedrooms),
            bathrooms = COALESCE(EXCLUDED.bathrooms, public_property_records.bathrooms),
            rooms = COALESCE(EXCLUDED.rooms, public_property_records.rooms),
            year_built = COALESCE(EXCLUDED.year_built, public_property_records.year_built),
            property_class = COALESCE(
                NULLIF(EXCLUDED.property_class, ''),
                public_property_records.property_class
            ),
            zoning_district = COALESCE(
                NULLIF(EXCLUDED.zoning_district, ''),
                public_property_records.zoning_district
            ),
            land_use = COALESCE(
                NULLIF(EXCLUDED.land_use, ''),
                public_property_records.land_use
            ),
            lot_area_sqft = COALESCE(
                EXCLUDED.lot_area_sqft,
                public_property_records.lot_area_sqft
            ),
            building_area_sqft = COALESCE(
                EXCLUDED.building_area_sqft,
                public_property_records.building_area_sqft
            ),
            latitude = COALESCE(EXCLUDED.latitude, public_property_records.latitude),
            longitude = COALESCE(EXCLUDED.longitude, public_property_records.longitude),
            source_name = EXCLUDED.source_name,
            coverage_scope = EXCLUDED.coverage_scope,
            detail_level = EXCLUDED.detail_level,
            observed_fields = ARRAY(
                SELECT DISTINCT unnest(
                    public_property_records.observed_fields
                    || EXCLUDED.observed_fields
                )
            ),
            verification_required = true,
            record_refreshed_at = EXCLUDED.record_refreshed_at,
            dataset_version = COALESCE(
                NULLIF(EXCLUDED.dataset_version, ''),
                public_property_records.dataset_version
            ),
            source_metadata = (
                public_property_records.source_metadata
                || EXCLUDED.source_metadata
                || jsonb_build_object(
                    'published_field_sources',
                    COALESCE(
                        public_property_records.source_metadata->'published_field_sources',
                        '{}'::jsonb
                    ) || COALESCE(
                        EXCLUDED.source_metadata->'published_field_sources',
                        '{}'::jsonb
                    )
                )
            )
    """

    total = 0
    async with tenant_tx(ctx) as conn:
        for start in range(0, len(records), BATCH_SIZE):
            batch = records[start:start + BATCH_SIZE]
            args = []
            public_args = []
            for rec in batch:
                source_key = str(metrics.get("source_key") or f"firehose:{rec.state}")
                payload = build_clean_lead_payload(
                    rec,
                    source_label=str(metrics.get("source") or ""),
                    source_key=source_key,
                )
                args.append((
                    tenant_id,
                    rec.parcel_id,
                    rec.state,
                    motivation_score(rec),
                    json.dumps(_harvest_underwriting(payload, source_key)),
                    json.dumps(payload),
                ))
                provenance = payload["provenance"]
                quality = payload["data_quality"]
                public_args.append((
                    source_key,
                    rec.parcel_id,
                    rec.state,
                    payload.get("county"),
                    payload.get("city"),
                    payload.get("zip_code"),
                    payload.get("address"),
                    payload.get("owner_name"),
                    payload.get("owner_type"),
                    payload.get("estimated_value"),
                    payload.get("last_sale_price"),
                    payload.get("last_sale_date"),
                    payload.get("bedrooms"),
                    payload.get("bathrooms"),
                    payload.get("rooms"),
                    payload.get("year_built"),
                    payload.get("property_class"),
                    payload.get("zoning_district"),
                    payload.get("land_use"),
                    payload.get("lot_area_sqft"),
                    payload.get("building_area_sqft"),
                    payload.get("latitude"),
                    payload.get("longitude"),
                    provenance.get("source_name") or source_key,
                    provenance.get("coverage_scope") or "source scope not declared",
                    quality.get("detail_level") or "limited",
                    quality.get("observed_fields") or [],
                    _payload_refreshed_at(payload),
                    payload.get("dataset_version"),
                    json.dumps(payload.get("source_metadata") or {}),
                ))
            await conn.executemany(sql, args)
            await conn.executemany(public_sql, public_args)
            total += len(batch)
            metrics["inserted"] = total
    return total


async def upsert_public_records(
    tenant_id: str,
    agent_id: str,
    records: list[PropertyRecord],
    *,
    metrics: dict[str, Any],
) -> int:
    """Persist public catalog rows without creating tenant-private CRM leads.

    Targeted source reconciliation is a read/enrichment operation. Browsing an
    address must not silently create a private lead, so this path shares the
    catalog contract but intentionally omits the ``leads`` insert.
    """
    if not records:
        return 0
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    public_sql = """
        INSERT INTO public_property_records (
            source_key, source_record_id, parcel_id, state, county, city,
            zip_code, address, owner_name, owner_type, public_record_value,
            last_sale_price, reported_record_date, bedrooms, bathrooms, rooms,
            year_built, property_class, zoning_district, land_use, lot_area_sqft,
            building_area_sqft, latitude, longitude, source_name,
            coverage_scope, detail_level, observed_fields,
            verification_required, record_refreshed_at, dataset_version,
            source_metadata
        ) VALUES (
            $1, $2, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, public_record_date_or_null($12::text), $13, $14, $15,
            $16, $17, $18, $19, $20, $21, $22, $23, $24,
            $25, $26, $27::text[], true, $28::timestamptz, $29, $30::jsonb
        )
        ON CONFLICT (source_key, state, source_record_id) DO UPDATE SET
            parcel_id=COALESCE(
                NULLIF(EXCLUDED.parcel_id, ''), public_property_records.parcel_id
            ),
            county=COALESCE(
                NULLIF(EXCLUDED.county, ''), public_property_records.county
            ),
            city=COALESCE(NULLIF(EXCLUDED.city, ''), public_property_records.city),
            zip_code=COALESCE(
                NULLIF(EXCLUDED.zip_code, ''), public_property_records.zip_code
            ),
            address=COALESCE(
                NULLIF(EXCLUDED.address, ''), public_property_records.address
            ),
            owner_name=COALESCE(
                NULLIF(EXCLUDED.owner_name, ''), public_property_records.owner_name
            ),
            owner_type=COALESCE(
                NULLIF(EXCLUDED.owner_type, ''), public_property_records.owner_type
            ),
            public_record_value=COALESCE(
                EXCLUDED.public_record_value,
                public_property_records.public_record_value
            ),
            last_sale_price=COALESCE(
                EXCLUDED.last_sale_price,
                public_property_records.last_sale_price
            ),
            reported_record_date=COALESCE(
                EXCLUDED.reported_record_date,
                public_property_records.reported_record_date
            ),
            bedrooms=COALESCE(EXCLUDED.bedrooms, public_property_records.bedrooms),
            bathrooms=COALESCE(EXCLUDED.bathrooms, public_property_records.bathrooms),
            rooms=COALESCE(EXCLUDED.rooms, public_property_records.rooms),
            year_built=COALESCE(EXCLUDED.year_built, public_property_records.year_built),
            property_class=COALESCE(
                NULLIF(EXCLUDED.property_class, ''), public_property_records.property_class
            ),
            zoning_district=COALESCE(
                NULLIF(EXCLUDED.zoning_district, ''), public_property_records.zoning_district
            ),
            land_use=COALESCE(
                NULLIF(EXCLUDED.land_use, ''), public_property_records.land_use
            ),
            lot_area_sqft=COALESCE(
                EXCLUDED.lot_area_sqft,
                public_property_records.lot_area_sqft
            ),
            building_area_sqft=COALESCE(
                EXCLUDED.building_area_sqft,
                public_property_records.building_area_sqft
            ),
            latitude=COALESCE(EXCLUDED.latitude, public_property_records.latitude),
            longitude=COALESCE(EXCLUDED.longitude, public_property_records.longitude),
            source_name=EXCLUDED.source_name,
            coverage_scope=EXCLUDED.coverage_scope,
            detail_level=EXCLUDED.detail_level,
            observed_fields=ARRAY(
                SELECT DISTINCT unnest(
                    public_property_records.observed_fields
                    || EXCLUDED.observed_fields
                )
            ),
            verification_required=true,
            record_refreshed_at=EXCLUDED.record_refreshed_at,
            dataset_version=COALESCE(
                NULLIF(EXCLUDED.dataset_version, ''),
                public_property_records.dataset_version
            ),
            source_metadata=(
                public_property_records.source_metadata
                || EXCLUDED.source_metadata
                || jsonb_build_object(
                    'published_field_sources',
                    COALESCE(
                        public_property_records.source_metadata->'published_field_sources',
                        '{}'::jsonb
                    ) || COALESCE(
                        EXCLUDED.source_metadata->'published_field_sources',
                        '{}'::jsonb
                    )
                )
            )
    """
    source_key_default = str(metrics.get("source_key") or "")
    args = []
    for record in records:
        source_key = source_key_default or f"firehose:{record.state}"
        payload = build_clean_lead_payload(
            record,
            source_label=str(metrics.get("source") or ""),
            source_key=source_key,
        )
        provenance = payload["provenance"]
        quality = payload["data_quality"]
        args.append((
            source_key,
            record.parcel_id,
            record.state,
            payload.get("county"),
            payload.get("city"),
            payload.get("zip_code"),
            payload.get("address"),
            payload.get("owner_name"),
            payload.get("owner_type"),
            payload.get("estimated_value"),
            payload.get("last_sale_price"),
            payload.get("last_sale_date"),
            payload.get("bedrooms"),
            payload.get("bathrooms"),
            payload.get("rooms"),
            payload.get("year_built"),
            payload.get("property_class"),
            payload.get("zoning_district"),
            payload.get("land_use"),
            payload.get("lot_area_sqft"),
            payload.get("building_area_sqft"),
            payload.get("latitude"),
            payload.get("longitude"),
            provenance.get("source_name") or source_key,
            provenance.get("coverage_scope") or "source scope not declared",
            quality.get("detail_level") or "limited",
            quality.get("observed_fields") or [],
            _payload_refreshed_at(payload),
            payload.get("dataset_version"),
            json.dumps(payload.get("source_metadata") or {}),
        ))
    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        for start in range(0, len(args), BATCH_SIZE):
            await conn.executemany(public_sql, args[start:start + BATCH_SIZE])
    return len(args)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def normalize_public_leads(
    tenant_id: str,
    *,
    agent_id: str = "lead-payload-normalizer",
    batch_size: int = 500,
    max_rows: int = 10_000,
) -> dict[str, int]:
    """Upgrade legacy harvested leads to the clean public-record envelope.

    This only targets rows whose underwriting source identifies the national
    public-property pipeline; manual CRM leads and calculated underwriting are
    never rewritten.  It is idempotent and bounded for safe execution by the
    durable periodic worker.
    """
    from tenancy import TenantContext, Role
    from db.connection import tenant_tx

    safe_batch_size = max(1, min(int(batch_size), 1_000))
    safe_max_rows = max(1, min(int(max_rows), 100_000))
    ctx = TenantContext(agent_id=agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)
    examined = normalized = normalized_from_fallback = 0
    async with tenant_tx(ctx) as conn:
        while examined < safe_max_rows:
            rows = await conn.fetch(
                """
                SELECT id,parcel_id,state,payload,underwriting,updated_at
                  FROM leads
                 WHERE (
                       underwriting->>'source' LIKE 'firehose:%'
                       OR underwriting->>'source' = 'md_sdat'
                 )
                   AND COALESCE(payload->>'schema_version', '') <> $1
                 ORDER BY updated_at ASC, id ASC
                 LIMIT $2
                """,
                str(LEAD_PAYLOAD_SCHEMA_VERSION),
                min(safe_batch_size, safe_max_rows - examined),
            )
            if not rows:
                break
            updates = []
            for row in rows:
                original = _json_object(row["payload"])
                # A few historical import paths stored an incomplete payload
                # even though the canonical lead columns are present.  Repair
                # those rows from the canonical columns instead of allowing one
                # malformed legacy blob to abort an entire national cleanup.
                original.setdefault("parcel_id", str(row["parcel_id"] or ""))
                original.setdefault("state", str(row["state"] or ""))
                under = _json_object(row["underwriting"])
                source_key = _clean_text(under.get("source"), max_length=160)
                try:
                    payload = build_clean_lead_payload(
                        original,
                        state=str(row["state"]),
                        source_key=source_key,
                        refreshed_at=row["updated_at"],
                    )
                except (TypeError, ValueError):
                    normalized_from_fallback += 1
                    payload = build_clean_lead_payload(
                        {
                            "parcel_id": str(row["parcel_id"] or ""),
                            "state": str(row["state"] or ""),
                        },
                        state=str(row["state"]),
                        source_key=source_key,
                        refreshed_at=row["updated_at"],
                    )
                updates.append((
                    json.dumps(payload),
                    json.dumps(_harvest_underwriting(payload, source_key or f"firehose:{row['state']}")),
                    row["id"],
                ))
            if updates:
                await conn.executemany(
                    """
                    UPDATE leads
                       SET payload=$1::jsonb,
                           underwriting=COALESCE(underwriting, '{}'::jsonb) || $2::jsonb,
                           updated_at=now()
                     WHERE id=$3::uuid
                    """,
                    updates,
                )
            count = len(updates)
            examined += count
            normalized += count
            if count < safe_batch_size:
                break
    return {
        "examined": examined,
        "normalized": normalized,
        "normalized_from_fallback": normalized_from_fallback,
    }


class BaseHarvester(ABC):
    """One state's real ingest. Subclasses set the endpoint and implement
    `fetch_raw()` (archetype mixins do this) + `map_record()`."""

    STATE: str = "??"
    SOURCE_LABEL: str = "unknown"
    SOURCE_KEY: str = ""
    RETAIN_RAW: bool = False
    SOQL_CURSOR_FIELD: str = ""

    def __init__(
        self,
        tenant_id: str,
        agent_id: Optional[str] = None,
        limiter: Optional[RateLimiter] = None,
        cache: Optional[Any] = None,
    ):
        if not tenant_id:
            raise ValueError(
                "tenant_id is required — leads.tenant_id is NOT NULL and RLS-checked. "
                "Set ORACLE_INGEST_TENANT_ID to the target tenant UUID."
            )
        self.tenant_id = tenant_id
        self.agent_id = agent_id or f"firehose-{self.STATE.lower()}"
        self.limiter = limiter or RateLimiter()
        self._cache = cache
        self.metrics = {
            "state": self.STATE,
            "source": self.SOURCE_LABEL,
            "source_key": self.SOURCE_KEY or f"firehose:{self.STATE}",
            "requests": 0, "retries": 0,
            "fetched": 0, "parsed": 0, "skipped": 0, "aggregated": 0,
            "inserted": 0, "raw_retained": 0,
            "cache_hits": 0, "cache_misses": 0,
        }
        self._cursor_start: Optional[str] = None
        self._page_checkpoint = 0
        self._checkpoint_end: Optional[int] = None
        self._checkpoint_complete = True

    # -- HTTP (stdlib urllib in a thread; rate-limited + retried) -- #
    async def _get_json(self, url: str, headers: Optional[dict] = None):
        async def _do():
            await self.limiter.acquire()
            self.metrics["requests"] += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})

            def _blocking():
                try:
                    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):
                        ra = e.headers.get("Retry-After") if e.headers else None
                        raise Retryable(f"HTTP {e.code} from {self.SOURCE_LABEL}",
                                        retry_after=_parse_retry_after(ra))
                    raise

            body = await asyncio.to_thread(_blocking)
            text = body.decode("utf-8", errors="replace").strip()
            if not text:
                logger.warning("[%s] empty response body from %s", self.STATE, url)
                return []
            return json.loads(text)

        if self._cache is None:
            from data_integrations.cache import get_integration_cache

            self._cache = await get_integration_cache()
        source = (
            "municipal_violations"
            if "violation" in (self.SOURCE_KEY or self.SOURCE_LABEL).lower()
            else "state_gis"
        )
        before = self._cache.metrics()

        async def _fetch_wrapped() -> dict:
            return {"response": await self._with_retries(_do, what=f"{self.STATE} fetch")}

        cached = await self._cache.get_or_fetch(
            source,
            {"url": url, "headers": headers or {}},
            _fetch_wrapped,
        )
        after = self._cache.metrics()
        self.metrics["cache_hits"] += max(0, after["hits"] - before["hits"])
        self.metrics["cache_misses"] += max(0, after["misses"] - before["misses"])
        return cached.get("response")

    async def _with_retries(self, coro_factory, *, what: str):
        attempt = 0
        while True:
            try:
                return await coro_factory()
            except Exception as e:  # noqa: BLE001 — classified below
                attempt += 1
                retryable = isinstance(e, Retryable) or is_transient(e)
                if not retryable or attempt > MAX_RETRIES:
                    logger.error("%s failed permanently (attempt %d): %s", what, attempt, e)
                    raise
                self.metrics["retries"] += 1
                hinted = getattr(e, "retry_after", None)
                backoff = hinted if hinted else min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
                backoff += random.uniform(0, REQUEST_JITTER)
                logger.warning("%s failed (attempt %d/%d): %s — backing off %.1fs",
                               what, attempt, MAX_RETRIES, e, backoff)
                await asyncio.sleep(backoff)

    # -- Template method -- #
    @abstractmethod
    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        """Return raw source rows (already paginated/limited)."""

    @abstractmethod
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        """Map one source row to a PropertyRecord, or None to skip."""

    def aggregate_records(self, records: list[PropertyRecord]) -> list[PropertyRecord]:
        """Hook for row-heavy feeds (violations) to collapse by property."""
        return records

    def raw_property_key(self, row: dict) -> str:
        """Property reconciliation key used by raw-source retention."""
        return ""

    async def _load_cursor(self) -> Optional[str]:
        if not self.RETAIN_RAW or not self.SOURCE_KEY:
            return None
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            return await conn.fetchval(
                """
                SELECT cursor_value FROM harvest_sources
                WHERE tenant_id=$1::uuid AND source_key=$2
                """,
                self.tenant_id,
                self.SOURCE_KEY,
            )

    @staticmethod
    def _cursor_sort_key(value: Any) -> tuple[int, Any]:
        text = str(value or "").strip()
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)

    def _cursor_end(self, rows: list[dict]) -> Optional[str]:
        if not self.SOQL_CURSOR_FIELD:
            return None
        values = [
            str(row.get(self.SOQL_CURSOR_FIELD) or "").strip()
            for row in rows
            if str(row.get(self.SOQL_CURSOR_FIELD) or "").strip()
        ]
        return max(values, key=self._cursor_sort_key) if values else self._cursor_start

    async def _retain_raw(self, rows: list[dict], cursor_end: Optional[str]) -> None:
        """Persist exact public observations and advance the cursor atomically."""
        if not self.RETAIN_RAW or not self.SOURCE_KEY:
            return
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        retrieved_at = datetime.now(timezone.utc)
        request_material = json.dumps(
            {
                "source": self.SOURCE_KEY,
                "cursor_start": self._cursor_start,
                "cursor_end": cursor_end,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request_material.encode("utf-8")).hexdigest()
        retention_days = max(
            1,
            min(3650, int(os.getenv("ORACLE_RAW_SOURCE_RETENTION_DAYS", "730"))),
        )
        async with tenant_tx(ctx) as conn:
            license_row = await conn.fetchrow(
                """
                INSERT INTO source_licenses (
                    tenant_id,source_key,source_name,source_url,license_name,
                    property_level_allowed,outreach_use_allowed,retention_days
                ) VALUES ($1::uuid,$2,$3,$4,'municipal-open-data',true,false,$5)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    source_name=EXCLUDED.source_name,source_url=EXCLUDED.source_url,
                    active=true,updated_at=now()
                RETURNING id,retention_days
                """,
                self.tenant_id,
                self.SOURCE_KEY,
                self.SOURCE_LABEL,
                getattr(self, "RESOURCE_URL", None),
                retention_days,
            )
            for row in rows:
                raw_blob = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                payload_digest = hashlib.sha256(raw_blob.encode("utf-8")).hexdigest()
                record_key = str(
                    row.get(self.SOQL_CURSOR_FIELD)
                    or row.get("id")
                    or row.get("violationid")
                    or payload_digest
                )
                await conn.execute(
                    """
                    INSERT INTO source_records (
                        tenant_id,source_license_id,source_key,record_key,
                        property_key,jurisdiction,observed_at,retrieved_at,
                        request_hash,payload_hash,raw_payload,expires_at
                    ) VALUES (
                        $1::uuid,$2,$3,$4,$5,$6,$7,$7,$8,$9,$10::jsonb,
                        $7 + ($11 || ' days')::interval
                    ) ON CONFLICT (tenant_id,source_key,record_key,observed_at) DO NOTHING
                    """,
                    self.tenant_id,
                    license_row["id"],
                    self.SOURCE_KEY,
                    record_key,
                    self.raw_property_key(row) or None,
                    self.STATE,
                    retrieved_at,
                    request_hash,
                    payload_digest,
                    raw_blob,
                    str(license_row["retention_days"]),
                )
                self.metrics["raw_retained"] += 1
            await conn.execute(
                """
                INSERT INTO harvest_sources (
                    tenant_id,source_key,display_name,jurisdiction,adapter,
                    cursor_value,cursor_observed_at,last_started_at,
                    last_succeeded_at,last_record_observed_at,coverage
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,now(),now(),now(),now(),$7::jsonb)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    cursor_value=EXCLUDED.cursor_value,
                    cursor_observed_at=EXCLUDED.cursor_observed_at,
                    last_succeeded_at=EXCLUDED.last_succeeded_at,
                    last_record_observed_at=EXCLUDED.last_record_observed_at,
                    coverage=EXCLUDED.coverage,
                    failure_count=0,circuit_state='closed',last_error=NULL,
                    updated_at=now()
                """,
                self.tenant_id,
                self.SOURCE_KEY,
                self.SOURCE_LABEL,
                self.STATE,
                type(self).__name__,
                cursor_end,
                json.dumps({"fetched": len(rows), "raw_retained": self.metrics["raw_retained"]}),
            )

    async def harvest(
        self,
        *,
        max_records: Optional[int] = None,
        persist: bool = True,
        checkpoint: int = 0,
    ) -> dict:
        t0 = time.monotonic()
        self._page_checkpoint = max(0, int(checkpoint or 0))
        self._checkpoint_end = self._page_checkpoint
        self._checkpoint_complete = True
        self.metrics["checkpoint_start"] = self._page_checkpoint
        logger.info("[%s] harvest starting (%s)", self.STATE, self.SOURCE_LABEL)
        if persist and self.RETAIN_RAW:
            self._cursor_start = await self._load_cursor()
        rows = await self.fetch_raw(max_records)

        records: list[PropertyRecord] = []
        for row in rows:
            self.metrics["fetched"] += 1
            rec = self.map_record(row)
            if rec is None:
                self.metrics["skipped"] += 1
                continue
            rec = promote_public_characteristics(rec, row)
            records.append(rec)
            self.metrics["parsed"] += 1

        records = self.aggregate_records(records)
        self.metrics["aggregated"] = len(records)

        if persist:
            await persist_leads(self.tenant_id, self.agent_id, records, metrics=self.metrics)
            if self.RETAIN_RAW:
                await self._retain_raw(rows, self._cursor_end(rows))

        elapsed = time.monotonic() - t0
        self.metrics["elapsed_s"] = round(elapsed, 2)
        self.metrics["checkpoint"] = self._checkpoint_end
        self.metrics["checkpoint_complete"] = self._checkpoint_complete
        logger.info(
            "[%s] done in %.1fs — fetched=%d parsed=%d skipped=%d inserted=%d "
            "(requests=%d retries=%d)",
            self.STATE, elapsed, self.metrics["fetched"], self.metrics["parsed"],
            self.metrics["skipped"], self.metrics["inserted"],
            self.metrics["requests"], self.metrics["retries"],
        )
        self._records = records  # exposed for fetch_by_zip / tests
        return self.metrics


# --------------------------------------------------------------------------- #
# Source archetypes — paginated fetch_raw implementations.
# --------------------------------------------------------------------------- #
class SocrataHarvester(BaseHarvester):
    """SoQL/Socrata: GET {RESOURCE_URL}?$limit&$offset → list[dict]."""

    RESOURCE_URL: str = ""          # e.g. https://data.ct.gov/resource/5mzw-sjtu.json
    SOQL_WHERE: str = ""            # optional $where clause
    SOQL_ORDER: str = ":id"         # stable pagination order; override if dataset lacks :id

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        url = os.getenv(f"{self.STATE}_SOURCE_URL", self.RESOURCE_URL)
        if not url:
            logger.warning("[%s] no Socrata RESOURCE_URL configured — skipping", self.STATE)
            return []
        out: list[dict] = []
        offset = self._page_checkpoint
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            params: dict = {"$limit": page, "$offset": offset}
            if self.SOQL_ORDER:
                params["$order"] = self.SOQL_ORDER
            if self.SOQL_WHERE:
                params["$where"] = self.SOQL_WHERE
            if self.SOQL_CURSOR_FIELD and self._cursor_start:
                cursor = str(self._cursor_start).replace("'", "''")
                cursor_clause = (
                    f"{self.SOQL_CURSOR_FIELD}>{cursor}"
                    if cursor.isdigit()
                    else f"{self.SOQL_CURSOR_FIELD}>'{cursor}'"
                )
                params["$where"] = (
                    f"({params['$where']}) AND {cursor_clause}"
                    if params.get("$where")
                    else cursor_clause
                )
            rows = await self._get_json(f"{url}?{urllib.parse.urlencode(params)}")
            if not rows:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            out.extend(rows)
            offset += len(rows)
            self._checkpoint_end = offset
            if len(rows) < page:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            if max_records and len(out) >= max_records:
                self._checkpoint_complete = False
                break
        return out


class ArcGISHarvester(BaseHarvester):
    """ArcGIS REST FeatureServer: query with resultOffset → features[].attributes."""

    SERVICE_URL: str = ""           # e.g. https://.../FeatureServer/0/query
    WHERE: str = "1=1"
    OUT_FIELDS: str = "*"

    async def _resolved_out_fields(self, query_url: str) -> str:
        """Add published characteristic columns to an adapter's base field set.

        Most adapters intentionally select a compact set of identity/value
        columns. The layer schema lets us discover only the additional
        allow-listed facts (beds, baths, year, areas, class and sale price)
        available in that jurisdiction, avoiding the cost and privacy risk of
        pulling every source column.
        """
        configured = str(self.OUT_FIELDS or "*").strip()
        if configured == "*":
            return configured
        layer_url = query_url.split("?", 1)[0].rstrip("/")
        if layer_url.lower().endswith("/query"):
            layer_url = layer_url[:-6]
        try:
            metadata = await self._get_json(
                f"{layer_url}?{urllib.parse.urlencode({'f': 'json'})}"
            )
        except Exception as exc:  # noqa: BLE001 - schema discovery is best effort
            logger.warning("[%s] ArcGIS field discovery failed: %s", self.STATE, exc)
            return configured
        fields = metadata.get("fields", []) if isinstance(metadata, dict) else []
        selected = [part.strip() for part in configured.split(",") if part.strip()]
        selected_norm = {norm(name) for name in selected}
        for field in fields:
            name = str(field.get("name") or "").strip() if isinstance(field, dict) else ""
            normalized = norm(name)
            if (
                name
                and normalized in _PUBLIC_CHARACTERISTIC_NAMES
                and normalized not in selected_norm
            ):
                selected.append(name)
                selected_norm.add(normalized)
        discovered = max(0, len(selected_norm) - len({
            norm(part) for part in configured.split(",") if part.strip()
        }))
        self.metrics["characteristic_fields_discovered"] = discovered
        return ",".join(selected)

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        url = os.getenv(f"{self.STATE}_SOURCE_URL", self.SERVICE_URL)
        if not url:
            logger.warning("[%s] no ArcGIS SERVICE_URL configured — skipping", self.STATE)
            return []
        out_fields = await self._resolved_out_fields(url)
        out: list[dict] = []
        offset = self._page_checkpoint
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            params = {
                "where": self.WHERE, "outFields": out_fields,
                "returnGeometry": "false", "f": "json",
                "resultOffset": offset, "resultRecordCount": page,
            }
            data = await self._get_json(f"{url}?{urllib.parse.urlencode(params)}")
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                logger.warning(
                    "[%s] ArcGIS error response (code=%s): %s",
                    self.STATE, err.get("code", "?"), err.get("message", str(err))[:200],
                )
                break
            feats = data.get("features", []) if isinstance(data, dict) else []
            rows = [f.get("attributes", {}) for f in feats if isinstance(f, dict)]
            if not rows:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            out.extend(rows)
            offset += len(rows)
            self._checkpoint_end = offset
            if not data.get("exceededTransferLimit") and len(rows) < page:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            if max_records and len(out) >= max_records:
                self._checkpoint_complete = bool(
                    not data.get("exceededTransferLimit") and len(rows) < page
                )
                if self._checkpoint_complete:
                    self._checkpoint_end = None
                break
        return out


class CartoHarvester(BaseHarvester):
    """CARTO SQL API: GET {DOMAIN}/api/v2/sql?q=SELECT ... LIMIT n OFFSET o."""

    CARTO_DOMAIN: str = ""          # e.g. https://phl.carto.com
    TABLE: str = ""
    SELECT: str = "*"
    WHERE: str = ""

    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        domain = os.getenv(f"{self.STATE}_SOURCE_URL", self.CARTO_DOMAIN)
        if not domain or not self.TABLE:
            logger.warning("[%s] no CARTO source configured — skipping", self.STATE)
            return []
        out: list[dict] = []
        offset = self._page_checkpoint
        where = f" WHERE {self.WHERE}" if self.WHERE else ""
        while True:
            page = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page <= 0:
                break
            sql = f"SELECT {self.SELECT} FROM {self.TABLE}{where} LIMIT {page} OFFSET {offset}"
            data = await self._get_json(f"{domain}/api/v2/sql?{urllib.parse.urlencode({'q': sql})}")
            if isinstance(data, dict) and data.get("error"):
                logger.warning(
                    "[%s] CARTO error response: %s",
                    self.STATE, str(data["error"])[:200],
                )
                break
            rows = data.get("rows", []) if isinstance(data, dict) else []
            if not rows:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            out.extend(rows)
            offset += len(rows)
            self._checkpoint_end = offset
            if len(rows) < page:
                self._checkpoint_end = None
                self._checkpoint_complete = True
                break
            if max_records and len(out) >= max_records:
                self._checkpoint_complete = False
                break
        return out


def _parse_retry_after(value) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
