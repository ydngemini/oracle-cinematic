"""
data_integrations/census_geocoder.py — US Census Bureau Geocoder (KEYLESS).

The commercial-safe geocoder. Unlike the public Nominatim instance (ODbL
share-alike, no bulk/systematic use — see the Free Data Sources catalog's
DO-NOT-WIRE list), the Census Geocoder is US-government public-domain with no
ToS restriction on commercial or bulk use, and it returns authoritative FIPS
geography (state / county / tract / block) for free with no API key.

Two surfaces:
  * geocode(address)                 — single one-line address → lat/lng + FIPS
  * geocode_batch(addresses, ...)    — up to 10,000 addresses in one POST

Single lookups go through the standard DataSource cache; batch is a bespoke
multipart POST (stdlib urllib, run in a thread) that bypasses the per-row cache.

Endpoints (geographies returntype carries tract/block; locations does not):
  GET  /geocoder/geographies/onelineaddress
  POST /geocoder/geographies/addressbatch
"""

from __future__ import annotations

import asyncio
import csv
import io
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

# Public_AR_Current / Current_Current = freshest published address ranges +
# current vintage geography. Override per-call if a fixed vintage is needed.
_BENCHMARK = "Public_AR_Current"
_VINTAGE = "Current_Current"

_SINGLE_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"

# 90 days — addresses + their FIPS geography are effectively static between
# decennial/vintage updates (mirrors the "geocode" TTL in cache.py).
_TTL_GEOCODE = 90 * 86_400

# Census enforces 10k rows/batch.
_MAX_BATCH = 10_000


def _empty(address: str) -> dict:
    return {
        "matched": False,
        "lat": None,
        "lng": None,
        "display_name": "",
        "address_normalized": "",
        "state_fips": "",
        "county_fips": "",
        "zip": "",
        "tract": "",
        "block": "",
        "input_address": address,
        "source": "census_geocoder",
    }


class CensusGeocoder(DataSource):
    """Keyless, commercial-safe geocoder returning lat/lng + FIPS geography."""

    source_name = "census_geocoder"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.05, jitter=0.02),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return _TTL_GEOCODE

    # ── single (GET, cached) ────────────────────────────────────────────────
    async def fetch(self, *, address: str) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "address": address,
            "benchmark": _BENCHMARK,
            "vintage": _VINTAGE,
            "format": "json",
        })
        try:
            data = await self._get_json(f"{_SINGLE_URL}?{params}", timeout=15)
            matches = (data.get("result", {}) or {}).get("addressMatches", [])
            if not matches:
                return None
            m = dict(matches[0])
            m["_input"] = address
            return m
        except Exception as e:  # noqa: BLE001 — graceful degradation, never crash
            self._log.warning("Census geocode failed for '%s': %s", address, e)
            return None

    def normalize(self, raw: dict) -> dict:
        coords = raw.get("coordinates", {}) or {}
        geo = raw.get("geographies", {}) or {}
        tracts = geo.get("Census Tracts") or [{}]
        blocks = geo.get("2020 Census Blocks") or [{}]
        t0 = tracts[0] if tracts else {}
        b0 = blocks[0] if blocks else {}
        return {
            "matched": True,
            "lat": float(coords.get("y")) if coords.get("y") is not None else None,
            "lng": float(coords.get("x")) if coords.get("x") is not None else None,
            "display_name": raw.get("matchedAddress", ""),
            "address_normalized": raw.get("matchedAddress", ""),
            "state_fips": b0.get("STATE") or t0.get("STATE") or "",
            "county_fips": b0.get("COUNTY") or t0.get("COUNTY") or "",
            "zip": (raw.get("addressComponents", {}) or {}).get("zip", ""),
            "tract": b0.get("TRACT") or t0.get("TRACT") or "",
            "block": b0.get("BLOCK") or "",
            "input_address": raw.get("_input", ""),
            "source": "census_geocoder",
        }

    async def geocode(self, address: str) -> Optional[dict]:
        addr = (address or "").strip()
        if not addr:
            return None
        key = f"geocode:census:{urllib.parse.quote(addr.upper())}"
        return await self.get(key, address=addr)

    # ── batch (POST multipart, uncached) ────────────────────────────────────
    async def geocode_batch(self, addresses: list[str]) -> list[dict]:
        """Geocode many one-line addresses in a single POST.

        Returns one result dict per input address (input order preserved),
        each shaped like normalize()'s output. Never raises — on transport
        failure every row degrades to an unmatched result.
        """
        rows = [(str(a or "").strip()) for a in (addresses or [])]
        if not rows:
            return []
        if len(rows) > _MAX_BATCH:
            raise ValueError(f"Census batch limit is {_MAX_BATCH} addresses (got {len(rows)}).")

        # CSV the geocoder expects: id,oneline-address (no header). Split the
        # one-line address into the street column; the geocoder tolerates the
        # full address in 'street' with empty city/state/zip columns.
        buf = io.StringIO()
        w = csv.writer(buf)
        for i, addr in enumerate(rows):
            w.writerow([i, addr, "", "", ""])
        csv_bytes = buf.getvalue().encode("utf-8")

        try:
            text = await self._post_batch(csv_bytes)
        except Exception as e:  # noqa: BLE001
            self._log.warning("Census batch geocode failed (%d rows): %s", len(rows), e)
            return [_empty(a) for a in rows]

        return self._parse_batch_csv(text, rows)

    async def _post_batch(self, csv_bytes: bytes) -> str:
        boundary = "----oracle" + uuid.uuid4().hex

        def part(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        body = b"".join([
            part("benchmark", _BENCHMARK),
            part("vintage", _VINTAGE),
            part("returntype", "geographies"),
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="addressFile"; '
                'filename="addresses.csv"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode(),
            csv_bytes, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])

        async def _do() -> str:
            await self._limiter.acquire()
            self._metrics["requests"] += 1
            req = urllib.request.Request(
                _BATCH_URL,
                data=body,
                headers={
                    "User-Agent": self._USER_AGENT,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )

            def _blocking() -> str:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read().decode("utf-8", errors="replace")

            return await asyncio.to_thread(_blocking)

        return await self._with_retries(_do, label=f"POST {self.source_name} batch")

    @staticmethod
    def _parse_batch_csv(text: str, inputs: list[str]) -> list[dict]:
        """Geographies CSV columns:
        id, input_address, match_status, match_type, matched_address,
        "lon,lat", tigerline_id, side, state_fips, county_fips, tract, block
        """
        by_id: dict[int, dict] = {}
        for cols in csv.reader(io.StringIO(text)):
            if not cols:
                continue
            try:
                rid = int(cols[0])
            except (ValueError, IndexError):
                continue
            input_addr = cols[1] if len(cols) > 1 else ""
            status = cols[2] if len(cols) > 2 else "No_Match"
            if status != "Match" or len(cols) < 12:
                rec = _empty(input_addr)
                by_id[rid] = rec
                continue
            lon, _, lat = (cols[5] or "").partition(",")
            by_id[rid] = {
                "matched": True,
                "lat": float(lat) if lat else None,
                "lng": float(lon) if lon else None,
                "display_name": cols[4],
                "address_normalized": cols[4],
                "state_fips": cols[8],
                "county_fips": cols[9],
                "zip": "",
                "tract": cols[10],
                "block": cols[11],
                "input_address": input_addr,
                "source": "census_geocoder",
            }

        return [by_id.get(i, _empty(inputs[i])) for i in range(len(inputs))]
