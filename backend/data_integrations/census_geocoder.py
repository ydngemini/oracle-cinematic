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
multipart POST (stdlib urllib, run in a thread) cached as one canonical request.

Endpoints (geographies returntype carries tract/block; locations does not):
  GET  /geocoder/geographies/onelineaddress
  POST /geocoder/geographies/addressbatch
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
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


# ZIP+4 arrives three ways in this corpus: "98203-6505", the bare "05072", and
# — from the WY harvester — an unhyphenated "827188362". A \b anchor silently
# rejects that last form, because there is no word boundary inside a digit run.
# It then fell through to the state test, which also failed, and the entire
# "WY, 827188362" tail was swallowed into the city field. Measured: 29 of 200
# sampled rows parsed wrong, all of them this shape. A negative lookbehind
# anchors on "not preceded by a digit" instead, which is what was meant.
# Only the leading five are kept either way — Census scores worse with the +4.
# ZIP+4 arrives three ways in this corpus: "98203-6505", the bare "05072", and
# — from the WY harvester — an unhyphenated "827188362". A \b anchor silently
# rejects that last form, because there is no word boundary inside a digit run,
# so the whole "WY, 827188362" tail was swallowed into the city field (29 of 200
# sampled rows). A negative lookbehind anchors on "not preceded by a digit",
# which is what was meant. Only the leading five are kept: measured 15 matches
# on 5-digit versus 6 on the full ZIP+4, same rows.
_ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-?\d{4})?\s*$")

# Matched against the real set rather than r"[A-Za-z]{2}$". A bare two-letter
# pattern happily reads the "St" in "10 Downing St" as a state and eats it,
# which costs the street. Territories are included because the harvesters cover
# them and a missing code degrades exactly like a wrong one.
_STATE_CODES = frozenset("""
AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
AS GU MP PR VI
""".split())

# A secondary-address designator: the one thing after a comma that genuinely
# belongs to the street. Everything else between the street and the locality is,
# in this corpus, the city repeated into the address column — measured over
# 20,000 ungeocoded rows: 564 have a comma in `address`, 462 of them (82%) are a
# duplicated city, and only 12 (2%) are a unit. So extras are DROPPED by default
# and kept only when they match this.
_UNIT_RE = re.compile(
    r"^(?:#|(?:UNIT|APT|APARTMENT|STE|SUITE|BLDG|BUILDING|LOT|TRLR|TRAILER"
    r"|FL|FLOOR|RM|ROOM|SPC|SPACE|BSMT|PH)\b)",
    re.IGNORECASE,
)


def _split_oneline(address: str) -> tuple[str, str, str, str]:
    """Recover (street, city, state, zip) from a comma-joined one-line address.

    The Census batch endpoint scores on separated columns, so a one-line string
    has to be taken apart before the split passes can use it. Callers that hold
    the components join them in this order
    (backfill_property_coordinates._one_line), which makes the recovery exact
    for them; the free-text API caller gets a best effort.

    Parsed from the RIGHT, because that is the only end whose shape is known.
    An earlier version read positionally — street = first chunk, city =
    everything between — which broke on both ends of real data:

      "123 MAIN ST, UNIT 4, EVERETT, WA, 98203"
          gave city "UNIT 4, EVERETT", a locality no gazetteer contains
      "1000 ROUTE 9, HOWELL, NJ, 07731 1234"
          gave state "" and city "HOWELL, NJ, 07731 1234"

    Taking the LAST remaining chunk as the locality and letting the street
    absorb everything before it fixes both: a unit is part of the address, and
    it is the address column that harvesters put commas in. Nothing is guessed
    — a fragment becomes the state or the ZIP only if it really is one, and an
    unparseable tail after a recognised state is dropped rather than promoted
    to a city.
    """
    raw = str(address or "").strip()
    if not raw:
        return "", "", "", ""

    chunks = [c.strip() for c in raw.split(",") if c.strip()]
    if not chunks:
        return "", "", "", ""

    postal = state = ""

    # 1. ZIP. Trailing only, and it may share its chunk with the state
    #    ("DC 20500") or stand alone.
    m = _ZIP_RE.search(chunks[-1])
    if m:
        postal = m.group(1)
        remainder = chunks[-1][: m.start()].strip(" ,")
        if remainder:
            chunks[-1] = remainder
        else:
            chunks.pop()

    # 2. State. Normally the last chunk. If that chunk is unparseable junk —
    #    a malformed ZIP the pattern above refused — look one further left and
    #    discard the junk, rather than letting it become the city.
    if chunks:
        if chunks[-1].upper() in _STATE_CODES:
            state = chunks.pop().upper()
        elif len(chunks) >= 2 and chunks[-2].upper() in _STATE_CODES:
            state = chunks[-2].upper()
            del chunks[-2:]

    # 3. What is left: the FIRST chunk is the street, the LAST is the locality,
    #    and anything between them is kept only if it is a unit designator.
    #
    #    Both halves of that are measured, not assumed. Taking every leading
    #    chunk as street corrupts the 82% of comma-bearing rows whose address
    #    column already repeats the city ("189 N WOODRUN WAY, SARATOGA SPRINGS,
    #    UT" as the address of a row whose city is "Saratoga Springs") — that
    #    reading dropped the residue match rate from 26% to 0%. Taking only
    #    chunks[0] loses the 2% that carry a real unit. Keeping the first chunk
    #    plus recognised units, and discarding the rest, serves both.
    if not chunks:
        return "", "", state, postal
    if len(chunks) == 1:
        return chunks[0], "", state, postal

    street_parts = [chunks[0]]
    for chunk in chunks[1:-1]:
        if _UNIT_RE.match(chunk):
            street_parts.append(chunk)
    return ", ".join(street_parts), chunks[-1], state, postal


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

    # ── batch (POST multipart, canonical IntegrationCache request) ──────────
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

        # Three shapes, tried in order, each running only on what the previous
        # one missed. Measured on 150 rows from each population:
        #
        #                            clean addresses      never-geocoded residue
        #   1. whole address in `street`   150/150 (100%)        0/150   (0%)
        #   2. street + city + state       136/150  (91%)       23/150  (15%)
        #   3. street + state + ZIP5       142/150  (95%)       23/150  (15%)
        #   ------------------------------------------------------------------
        #   cascade (1 -> 2 -> 3)          150/150 (100%)       39/150  (26%)
        #
        # Shape 1 first is not arbitrary and an earlier revision of this code got
        # it backwards. The one-line form is the BEST shape for an address the
        # geocoder can resolve — it matched every clean row, where both split
        # forms lost 5-9%. It only looks broken if you sample rows where
        # latitude IS NULL, because those are precisely the rows it already
        # failed on; measured against them in isolation it scores 0, which is a
        # tautology, not a defect. The ~90% live rate recorded in
        # backfill_property_coordinates is real.
        #
        # Shapes 2 and 3 exist for the residue, where the stored city or ZIP is
        # the thing blocking the match: a locality this corpus records as
        # "South Harbor Twp" or "Rural" is not in the Census gazetteer and vetoes
        # a match street+state alone would make, and the ZIP is unreliable enough
        # to do the same (see the 12,322 multi-state ZIPs in the public-records
        # defect log). They are almost disjoint — 23 and 23, union 39 — so both
        # are worth running.
        #
        # Cost is bounded by usefulness: on a healthy batch shape 1 matches
        # nearly everything and the retries carry only the leftovers.
        parts = [_split_oneline(a) for a in rows]

        results = await self._batch_pass(rows, parts, shape="oneline")
        for shape in ("city", "zip"):
            retry = [i for i, r in enumerate(results) if not r.get("matched")]
            if not retry:
                break
            recovered = await self._batch_pass(
                [rows[i] for i in retry], [parts[i] for i in retry], shape=shape,
            )
            for slot, rec in zip(retry, recovered):
                if rec.get("matched"):
                    results[slot] = rec
        return results

    async def _batch_pass(
        self,
        rows: list[str],
        parts: list[tuple[str, str, str, str]],
        *,
        shape: str,
    ) -> list[dict]:
        """One POST in a single column shape. Degrades to unmatched, never raises.

        `shape` is named rather than boolean because each form makes two
        independent decisions — whether to split at all, and which of city/ZIP
        to trust — and a flag called `use_zip` hid the fact that it also drops
        the city, which is the half that actually does the work.
        """
        buf = io.StringIO()
        w = csv.writer(buf)
        for i, (street, city, state, postal) in enumerate(parts):
            if shape == "oneline":
                w.writerow([i, rows[i], "", "", ""])
            elif shape == "city":
                w.writerow([i, street, city, state, ""])
            else:  # "zip" — city dropped on purpose; see geocode_batch
                w.writerow([i, street, "", state, postal])
        csv_bytes = buf.getvalue().encode("utf-8")

        try:
            if self._cache is None:
                from .cache import get_integration_cache

                self._cache = await get_integration_cache()

            async def fetch_batch() -> dict:
                return {"csv": await self._post_batch(csv_bytes)}

            payload = await self._cache.get_or_fetch(
                "geocode",
                # The shape is part of the cache identity, and it is load-bearing:
                # when a pass matches nothing the next pass receives a byte-
                # identical `rows` list, so without `shape` in the key it would
                # replay the previous pass's CSV from cache and silently no-op.
                {"provider": "census_batch", "addresses": rows, "shape": shape},
                fetch_batch,
                ttl=self._cache_ttl(),
            )
            text = str(payload.get("csv") or "")
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
            # The CALLER's string, never Census's echo of it (cols[1]).
            #
            # cols[1] echoes the columns we sent, so now that the passes send
            # split columns it reads back as "1216 CASCADE DR, EVERETT, WA," on
            # the city pass and "1216 CASCADE DR, , WA, 98203" on the ZIP pass —
            # neither of which is what POST /geocode/batch was handed. A client
            # joining results to its request by input_address would silently
            # lose every row, and rows missing from the CSV already fall through
            # to _empty(inputs[i]) below, so the response would not even be
            # internally consistent.
            input_addr = inputs[rid] if 0 <= rid < len(inputs) else (
                cols[1] if len(cols) > 1 else ""
            )
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
