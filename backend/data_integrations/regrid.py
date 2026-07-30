"""Nationwide Regrid parcel lookups, kept server-side and quota-safe.

Regrid's MCP is a translation layer over its REST API.  The product backend
uses the REST surface directly so the Regrid token remains in the server
environment and never becomes browser-visible.  This connector intentionally
limits requests to a narrow, address-driven public-property lookup and stores
only a normalized, allow-listed result in the integration cache.

Provider terms require model-training/data-sharing to be disabled when Regrid
data is used with an AI or LLM.  This connector does not send parcel records to
an AI service; callers receive a source marker so downstream workflows can
enforce that restriction as well.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
from typing import Any, Optional

from .base import DataIntegrationError, DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache


_ADDRESS_URL = "https://app.regrid.com/api/v2/parcels/address"
_TTL_REGRID_PARCEL = 7 * 86_400
_MAX_LIMIT = 5


class RegridConfigurationError(DataIntegrationError):
    """Raised before any network request when the provider token is missing."""


class RegridUpstreamError(DataIntegrationError):
    """A provider failure with deliberately redacted transport details."""


class RegridCoverageError(DataIntegrationError):
    """The configured token is valid but lacks access to the requested area."""


def _value(fields: dict[str, Any], *keys: str) -> Any:
    """Return the first present standard-schema field without coercing it."""
    for key in keys:
        value = fields.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_count(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class RegridParcelSource(DataSource):
    """Small, cached Regrid address-lookup surface for public parcel facts."""

    source_name = "regrid_parcel"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            # Regrid documents roughly 200 requests/minute.  A 0.5 second
            # interval leaves headroom for other server processes and retries.
            rate_limiter=RateLimiter(min_interval=0.5, jitter=0.1),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )
        self._token = (os.environ.get("REGRID_API_TOKEN") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _cache_ttl(self) -> int:
        return _TTL_REGRID_PARCEL

    async def fetch(
        self,
        *,
        address: str,
        path: Optional[str] = None,
        limit: int = 1,
        include_geometry: bool = False,
    ) -> dict:
        if not self.configured:
            raise RegridConfigurationError("REGRID_API_TOKEN is not configured")

        params = {
            "query": address,
            "limit": max(1, min(int(limit), _MAX_LIMIT)),
            "return_count": "true",
            "return_geometry": str(bool(include_geometry)).lower(),
            # Keep optional enrichments intentionally narrow.  The normalized
            # response does not expose mailing addresses or custom fields.
            "return_custom": "false",
            "return_matched_buildings": "false",
            "return_matched_addresses": "false",
            "return_enhanced_ownership": "false",
            "return_zoning": "true",
            "token": self._token,
        }
        if path:
            params["path"] = path

        try:
            raw = await self._get_json(
                f"{_ADDRESS_URL}?{urllib.parse.urlencode(params)}", timeout=20
            )
        except urllib.error.HTTPError as exc:
            # Regrid uses 403 when a valid trial/account token does not include
            # the queried geography.  Preserve that distinction for the UI
            # without logging the URL, query, or token.
            if exc.code == 403:
                self._log.info("Regrid parcel lookup denied by provider coverage")
                raise RegridCoverageError(
                    "Regrid access does not include the requested location"
                ) from exc
            self._log.warning("Regrid parcel lookup failed (HTTP %s)", exc.code)
            raise RegridUpstreamError("Regrid parcel lookup is unavailable") from exc
        except Exception as exc:  # noqa: BLE001 - redact provider/URL details
            self._log.warning("Regrid parcel lookup failed (%s)", type(exc).__name__)
            raise RegridUpstreamError("Regrid parcel lookup is unavailable") from exc

        if not isinstance(raw, dict):
            raise RegridUpstreamError("Regrid returned an invalid parcel response")

        # These request fields are non-secret and enable a stable normalized
        # envelope after cache replay.  Never add the token to this payload.
        raw["_input_address"] = address
        raw["_input_path"] = path or None
        raw["_include_geometry"] = bool(include_geometry)
        return raw

    def normalize(self, raw: dict) -> dict:
        parcel_collection = raw.get("parcels") or {}
        features = parcel_collection.get("features") or []
        if not isinstance(features, list):
            features = []

        include_geometry = bool(raw.get("_include_geometry"))
        parcels: list[dict[str, Any]] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            if not isinstance(properties, dict):
                properties = {}
            fields = properties.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}

            parcel = {
                "regrid_id": feature.get("id") or _value(fields, "ogc_fid"),
                "regrid_uuid": properties.get("ll_uuid") or _value(fields, "ll_uuid"),
                "headline": properties.get("headline") or _value(fields, "address"),
                "address": _value(fields, "address") or properties.get("headline"),
                "parcel_number": _value(fields, "parcelnumb", "parcelnumb_no_formatting"),
                "owner_name": _value(fields, "owner"),
                "state": _value(fields, "state2", "admin1"),
                "county": _value(fields, "county", "admin2"),
                "city": _value(fields, "city", "admin3"),
                "path": properties.get("path") or _value(fields, "path"),
                "latitude": _value(fields, "lat"),
                "longitude": _value(fields, "lon"),
                "land_use_code": _value(fields, "usecode"),
                "land_use": _value(fields, "usedesc"),
                "zoning": _value(fields, "zoning"),
                "zoning_description": _value(fields, "zoning_description"),
                "year_built": _value(fields, "yearbuilt"),
                "building_count": _value(fields, "ll_bldg_count", "structno"),
                "building_sqft": _value(fields, "ll_bldg_footprint_sqft", "bldgsqft"),
                "parcel_area_sq_meters": _value(fields, "ll_gissqm"),
                "assessed_value": _value(fields, "parval"),
                "assessment_type": _value(fields, "parvaltype"),
                "land_value": _value(fields, "landval"),
                "improvement_value": _value(fields, "improvval"),
                "market_value": _value(fields, "marketval"),
                "tax_amount": _value(fields, "taxamt"),
                "tax_year": _value(fields, "taxyear"),
                "last_sale_price": _value(fields, "saleprice"),
                "last_sale_date": _value(fields, "saledate"),
                "flood_zone": _value(fields, "fema_flood_zone"),
                "last_refreshed": _value(fields, "ll_last_refresh"),
            }
            if include_geometry and isinstance(feature.get("geometry"), dict):
                parcel["geometry"] = feature["geometry"]
            parcels.append(parcel)

        raw_count = raw.get("count", parcel_collection.get("count", len(parcels)))
        return {
            "matched": bool(parcels),
            "input_address": raw.get("_input_address", ""),
            "input_path": raw.get("_input_path"),
            "count": _as_count(raw_count, len(parcels)),
            "returned": len(parcels),
            "parcels": parcels,
            "source": "regrid",
            "source_scope": "public_property_record",
            "model_training_prohibited": True,
        }

    async def lookup_address(
        self,
        address: str,
        *,
        path: Optional[str] = None,
        limit: int = 1,
        include_geometry: bool = False,
    ) -> dict:
        address_normalized = (address or "").strip()
        if not address_normalized:
            raise ValueError("address is required")
        if not self.configured:
            raise RegridConfigurationError("REGRID_API_TOKEN is not configured")

        path_normalized = (path or "").strip() or None
        limit_normalized = max(1, min(int(limit), _MAX_LIMIT))
        # IntegrationCache hashes this logical key before persistence.  It is
        # still useful for in-process request de-duplication and does not carry
        # provider credentials.
        cache_key = "regrid:address:{}:path={}:limit={}:geometry={}".format(
            urllib.parse.quote(address_normalized.upper(), safe=""),
            urllib.parse.quote(path_normalized or "", safe=""),
            limit_normalized,
            int(bool(include_geometry)),
        )
        return await self.get(
            cache_key,
            address=address_normalized,
            path=path_normalized,
            limit=limit_normalized,
            include_geometry=bool(include_geometry),
        ) or {
            "matched": False,
            "input_address": address_normalized,
            "input_path": path_normalized,
            "count": 0,
            "returned": 0,
            "parcels": [],
            "source": "regrid",
            "source_scope": "public_property_record",
            "model_training_prohibited": True,
        }
