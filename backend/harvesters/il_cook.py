"""Illinois — Cook County Assessor parcel data (Socrata, two-table join).

Cook County splits ownership and assessment into two Socrata resources that
share a 14-digit PIN key:

  3723-97qp  Assessor – Parcel Addresses
             pin, prop_address_full, prop_address_city_name,
             prop_address_zipcode_1, owner_address_name, year

  uzyt-m557  Assessor – Historic Assessed Values (also current year)
             pin, certified_tot (assessed value, 1/10th of market),
             board_tot (post-appeals assessed value), year

Strategy: page through Parcel Addresses (primary loop, SocrataHarvester
machinery), prefetch assessed values for each page of PINs via an IN(...)
SoQL query on uzyt-m557, then join in map_record via a per-page _val_cache.

Absentee detection: if the mailing address state (mail_address_state) differs
from "IL", or the mailing city differs from the property city, the owner is
likely not living on-site.

Cook County IL assessed value note: Cook County assesses at 10% of estimated
market value (class 2xx residential), so we multiply certified_tot by 10 to
produce an estimated_value comparable to other states' full market value.
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

from .base import SocrataHarvester, classify_owner, to_float
from .property_adapter import PropertyRecord

# ── dataset constants ────────────────────────────────────────────────────────
_ADDR_URL   = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"
_VALUE_URL  = "https://datacatalog.cookcountyil.gov/resource/uzyt-m557.json"
_HARVEST_YEAR = "2024"            # most recent complete assessment year

# Cook County residential classes start with "2"; multiplier converts the
# 10%-of-market assessed value to an approximate full market value.
_ASSESS_MULTIPLIER = 10.0

# Max PINs per assessed-value IN(...) query. A full 1000-PIN page builds a
# ~17 KB URL and Socrata returns HTTP 414; 150 keeps the query string well under
# the ~8 KB limit while still amortizing the request across the page.
_VALUE_CHUNK = 150


class IllinoisCookHarvester(SocrataHarvester):
    """Cook County, IL parcel + assessment data via two Socrata endpoints."""

    STATE        = "IL"
    SOURCE_LABEL = "Cook County Assessor – Parcel Addresses + Assessed Values (Socrata)"
    RESOURCE_URL = _ADDR_URL
    SOQL_WHERE   = f"year='{_HARVEST_YEAR}'"
    SOQL_ORDER   = "pin"          # stable pagination by PIN (14-digit string)

    # Populated per page by fetch_raw override; consumed in map_record.
    _val_cache: dict[str, float]

    # ── fetch_raw: extend SocrataHarvester to prefetch values per page ───────
    async def fetch_raw(self, max_records: Optional[int]) -> list[dict]:
        """Page through address records; for each page also fetch assessed
        values so map_record can do an O(1) dict lookup instead of N queries."""
        import os

        url = os.getenv("IL_SOURCE_URL", self.RESOURCE_URL)
        out: list[dict] = []
        self._val_cache = {}

        from .base import PAGE_SIZE

        offset = 0
        while True:
            page_size = min(PAGE_SIZE, (max_records - len(out)) if max_records else PAGE_SIZE)
            if page_size <= 0:
                break

            params = {
                "$limit":  page_size,
                "$offset": offset,
                "$order":  self.SOQL_ORDER,
                "$where":  self.SOQL_WHERE,
            }
            rows: list[dict] = await self._get_json(
                f"{url}?{urllib.parse.urlencode(params)}"
            )
            if not rows:
                break

            # Prefetch assessed values for this page's PINs. A single IN(...) over
            # a full 1000-PIN page builds a ~17 KB query string and Socrata answers
            # HTTP 414 (Request-URI Too Large), which previously killed the entire
            # IL harvest on the first page — that's why IL was stuck at 131 stale
            # rows. Chunk the IN(...) into small batches (URL stays well under 8 KB),
            # and treat the value lookup as best-effort: if a chunk fails we log and
            # continue with estimated_value=0 rather than dropping the whole batch.
            pins = [r["pin"] for r in rows if r.get("pin")]
            await self._prefetch_values(pins)

            out.extend(rows)
            offset += len(rows)
            if len(rows) < page_size or (max_records and len(out) >= max_records):
                break

        return out

    async def _prefetch_values(self, pins: list[str]) -> None:
        """Populate ``self._val_cache`` for ``pins`` via chunked IN(...) queries.

        Best-effort: a failed chunk (e.g. a transient portal error) logs and is
        skipped, leaving those parcels at estimated_value=0 — the harvest still
        persists every address, which is the data that matters for lead coverage.
        """
        from .base import logger

        for start in range(0, len(pins), _VALUE_CHUNK):
            chunk = pins[start:start + _VALUE_CHUNK]
            if not chunk:
                continue
            pin_list = ",".join(f"'{p}'" for p in chunk)
            val_where = f"pin in({pin_list}) AND year='{_HARVEST_YEAR}'"
            val_params = {
                "$where":  val_where,
                "$select": "pin,certified_tot,board_tot",
                "$limit":  len(chunk),
            }
            try:
                val_rows = await self._get_json(
                    f"{_VALUE_URL}?{urllib.parse.urlencode(val_params)}"
                )
            except Exception as exc:  # noqa: BLE001 — value lookup is best-effort
                logger.warning("[IL] assessed-value prefetch chunk failed (%d pins): %s",
                               len(chunk), exc)
                continue
            if isinstance(val_rows, list):
                for vr in val_rows:
                    pin = vr.get("pin", "")
                    # Prefer board_tot (post-appeals), fall back to certified_tot
                    raw = vr.get("board_tot") or vr.get("certified_tot") or 0
                    self._val_cache[pin] = to_float(raw) * _ASSESS_MULTIPLIER

    # ── map_record ────────────────────────────────────────────────────────────
    def map_record(self, row: dict) -> Optional[PropertyRecord]:
        pin   = str(row.get("pin") or "").strip()
        addr  = str(row.get("prop_address_full") or "").strip()
        owner = str(row.get("owner_address_name") or "").strip()

        if not pin or not addr or not owner or owner.upper() == "CURRENT OWNER":
            return None

        city    = str(row.get("prop_address_city_name") or "").strip()
        zipcode = str(row.get("prop_address_zipcode_1") or "").strip()[:10]

        # Assessed value (market value proxy) from pre-fetched cache.
        est_value = self._val_cache.get(pin, 0.0)

        # Absentee detection: mail state ≠ IL  OR  mail city ≠ property city
        mail_state = str(row.get("mail_address_state") or "").strip().upper()
        mail_city  = str(row.get("mail_address_city_name") or "").strip().upper()
        is_absentee = (mail_state not in ("", "IL")) or (
            mail_city and city and mail_city != city.upper()
        )

        distress_flags: list[str] = []
        if est_value == 0.0:
            distress_flags.append("no_assessed_value")
        if is_absentee:
            distress_flags.append("absentee_owner")

        return PropertyRecord(
            parcel_id       = pin,
            address         = addr,
            city            = city,
            state           = self.STATE,
            zip_code        = zipcode,
            owner_name      = owner,
            owner_type      = classify_owner(owner),
            estimated_value = est_value,
            equity_percent  = 0.0,      # not derivable from assessment data alone
            is_absentee_owner = is_absentee,
            distress_flags  = distress_flags,
            last_sale_date  = None,
        )
