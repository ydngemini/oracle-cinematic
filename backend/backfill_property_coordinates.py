#!/usr/bin/env python3
"""Geocode public_property_records that have an address but no coordinates.

8.59M public records carry an address (93.8%) and an owner (89.3%), but only
4.3% carry latitude/longitude. Without coordinates the map view has nothing to
plot, and `list_comparable_sales` / `estimate_arv` — whose radius search
migration 0076 added an index for — can only consider the 4% that happen to
have them. The data to fix that is already in the row; nobody ever resolved it.

The Census batch geocoder is keyless, free, and already wired
(data_integrations/census_geocoder.py), which is why this is a backfill script
rather than a purchase.

Resumable and idempotent. Progress is the id cursor, so an interrupted run
restarts where it stopped rather than at the beginning, and a row that already
has coordinates is never re-fetched.

    ORACLE_DB_* set as usual:
      python backfill_property_coordinates.py            # every state
      python backfill_property_coordinates.py --state DE # one state first
      python backfill_property_coordinates.py --dry-run  # match rate only

Census asks for courtesy on the batch endpoint, so the default batch is 5,000
(the API allows 10,000) with a pause between calls. Raise deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from data_integrations.census_geocoder import CensusGeocoder
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

# public_property_records has no tenant_id — it is platform-wide public data —
# but tenant_tx still needs a context to set the session GUCs.
_CTX = TenantContext(
    agent_id="coordinate-backfill",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)

_DEFAULT_BATCH = 5_000
_MAX_BATCH = 10_000
# A page this size resolving NOTHING is the endpoint, not the data.
_OUTAGE_MIN_BATCH = 50
_BATCH_ATTEMPTS = 3
_BATCH_BACKOFF_SECONDS = 30


def _preflight(timeout: float = 20.0) -> str | None:
    """Why the geocoder is unusable from this host, or None if it answers.

    Without this, an unreachable endpoint is indistinguishable from a genuine
    zero-match run: `geocode_batch` never raises, it degrades every row to
    unmatched. An operator would watch "0/5000 matched" scroll past for hours
    and conclude the addresses were bad.

    The specific failure worth naming: the Census geocoder closes the connection
    on datacenter and cloud egress ranges — TCP connects, TLS or HTTP then dies —
    while the rest of the internet is reachable. It is not a firewall on this
    side and not a fault in the addresses.
    """
    probe = "https://geocoding.geo.census.gov/geocoder/benchmarks"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as response:
            if response.status == 200:
                return None
            return f"the geocoder answered HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # A response at all means the host is reachable; the batch endpoint may
        # still work, so this is not disqualifying.
        return None if exc.code < 500 else f"the geocoder answered HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same answer here
        return f"{type(exc).__name__}: {exc}"


# An address the Census geocoder is known to resolve, used to tell a dead service
# apart from an unmatchable page. Deliberately famous and deliberately fixed:
# its only job is to answer "is the batch endpoint returning matches right now".
_CANARY_ADDRESS = "1600 Pennsylvania Ave NW, Washington, DC, 20500"


async def _batch_endpoint_is_answering(geocoder) -> bool:
    """Does the BATCH endpoint resolve a known-good address right now?

    Posted directly rather than through geocode_batch, because that path is
    cached: a cached hit would report the service healthy during an outage,
    which is the one moment this needs to be believed.

    _preflight() is not a substitute. It probes /geocoder/benchmarks, a different
    endpoint that stays up while the batch endpoint is failing — good enough to
    catch "this host cannot reach Census at all", useless for "the batch endpoint
    stopped returning matches".
    """
    buf = io.StringIO()
    csv.writer(buf).writerow([0, _CANARY_ADDRESS, "", "", ""])
    try:
        text = await geocoder._post_batch(buf.getvalue().encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any transport failure means "no"
        print(f"  canary POST failed ({type(exc).__name__}): treating as an outage",
              flush=True)
        return False
    return '"Match"' in text and "No_Match" not in text


async def _claim(conn, *, after_id: str | None, state: str | None, limit: int) -> list[Any]:
    """The next page of ungeocoded rows, ordered by id so the cursor is stable."""
    return await conn.fetch(
        """
        SELECT id, address, city, state, zip_code
          FROM public_property_records
         WHERE latitude IS NULL
           AND address IS NOT NULL
           AND btrim(address) <> ''
           AND ($1::uuid IS NULL OR id > $1::uuid)
           AND ($2::text IS NULL OR state = $2)
         ORDER BY id
         LIMIT $3
        """,
        after_id,
        state,
        limit,
    )


def _one_line(row: Any) -> str:
    """Census matches far better with city/state/zip than with a bare street."""
    parts = [str(row["address"] or "").strip()]
    for key in ("city", "state", "zip_code"):
        value = str(row[key] or "").strip()
        if value:
            parts.append(value)
    return ", ".join(parts)


async def _write(conn, matched: list[tuple[str, float, float]]) -> int:
    if not matched:
        return 0
    # One statement per page rather than per row: at 8M rows the round trips
    # dominate everything else.
    await conn.executemany(
        """
        UPDATE public_property_records
           SET latitude = $2, longitude = $3, updated_at = now()
         WHERE id = $1::uuid AND latitude IS NULL
        """,
        matched,
    )
    return len(matched)


async def run(
    *,
    state: str | None,
    batch_size: int,
    dry_run: bool,
    limit_batches: int | None,
    skip_preflight: bool = False,
) -> int:
    if not skip_preflight:
        unreachable = await asyncio.to_thread(_preflight)
        if unreachable:
            print(f"!! the geocoder is not reachable from this host: {unreachable}", file=sys.stderr)
            # Printed for every reason, not just the ones we recognise: the
            # operator cannot tell a transient outage from a refused egress
            # range, and both look like a TCP connection that dies.
            print(
                "   Census refuses some datacenter and cloud egress ranges, and it "
                "also has outages. Retry, or run from a residential connection or "
                "an HTTPS_PROXY pointing at one.",
                file=sys.stderr,
            )
            print("   Nothing was read or written. --skip-preflight to try anyway.", file=sys.stderr)
            return 2

    geocoder = CensusGeocoder()
    cursor: str | None = None
    seen = written = unmatched = batches = 0
    stopped_on_outage = False

    while True:
        async with tenant_tx(_CTX) as conn:
            rows = await _claim(conn, after_id=cursor, state=state, limit=batch_size)
        if not rows:
            break

        addresses = [_one_line(r) for r in rows]

        # A zero-match page is ambiguous: either the service is down — in which
        # case advancing would walk the cursor through millions of rows asking
        # nothing and marking them seen — or these particular addresses simply
        # cannot be resolved. Both used to be answered by retrying with backoff
        # and then inferring an outage, on the reasoning that real data matches
        # ~90%.
        #
        # That premise has expired. It described the corpus as a whole; what is
        # LEFT is the residue after every easily-matched row was geocoded out of
        # this queue. Measured: 92-100% for rows already done, 0-26% for the
        # residue. A zero page is now an ORDINARY result, so inferring an outage
        # from one stopped the job at the first hard patch — permanently, since
        # the cursor never advanced past it. That is what turned 17 runs
        # `partial` while the service was healthy throughout.
        #
        # So ask the service, and ask it FIRST. The canary is one address and
        # about half a second; the retry ladder is 90. When a zero page is the
        # common case, paying the ladder before asking the question costs ~17
        # days of sleeping across the 8.2M rows still queued, to learn something
        # one request answers immediately. Retry only once the canary says there
        # is something worth waiting for.
        matched: list[tuple[str, float, float]] = []
        for attempt in range(1, _BATCH_ATTEMPTS + 1):
            results = await geocoder.geocode_batch(addresses)
            matched = [
                (str(row["id"]), float(result["lat"]), float(result["lng"]))
                for row, result in zip(rows, results)
                if result.get("matched")
                and result.get("lat") is not None
                and result.get("lng") is not None
            ]
            if matched or len(rows) < _OUTAGE_MIN_BATCH:
                break

            if await _batch_endpoint_is_answering(geocoder):
                print(
                    f"  batch {batches + 1}: 0 of {len(rows)} resolved, and a "
                    f"known-good address matched — these addresses are "
                    f"unmatchable, not an outage. Advancing.",
                    flush=True,
                )
                break

            if attempt < _BATCH_ATTEMPTS:
                backoff = _BATCH_BACKOFF_SECONDS * attempt
                print(
                    f"  batch {batches + 1}: 0 of {len(rows)} resolved and a "
                    f"known-good address failed too — retrying the same page in "
                    f"{backoff}s ({attempt}/{_BATCH_ATTEMPTS - 1})",
                    flush=True,
                )
                await asyncio.sleep(backoff)
            else:
                print(
                    f"!! {len(rows)} consecutive addresses resolved to nothing and "
                    f"a known-good address also failed after {_BATCH_ATTEMPTS} "
                    f"attempts. The geocoder is not answering; stopping so the "
                    f"remaining rows stay queued. Re-run to resume from here.",
                    file=sys.stderr,
                )
                stopped_on_outage = True

        if stopped_on_outage:
            break

        cursor = str(rows[-1]["id"])
        seen += len(rows)
        unmatched += len(rows) - len(matched)

        if not dry_run:
            async with tenant_tx(_CTX) as conn:
                written += await _write(conn, matched)
        else:
            written += len(matched)

        batches += 1
        rate = (100.0 * written / seen) if seen else 0.0
        print(
            f"  batch {batches}: {len(matched)}/{len(rows)} matched "
            f"({rate:.1f}% cumulative, {seen:,} seen)",
            flush=True,
        )

        if limit_batches and batches >= limit_batches:
            break
        # Courtesy pause — this is a free public endpoint doing real work.
        await asyncio.sleep(1.0)

    print(
        f"{'(dry run) ' if dry_run else ''}geocoded {written:,} of {seen:,} "
        f"({unmatched:,} unmatched)",
        flush=True,
    )
    # Unmatched rows are not failures — a demolished parcel or a rural route has
    # no coordinate to find. Stopping early on an outage IS a failure, and it has
    # to be reported as one even when it happened on the first page: `not seen`
    # would otherwise read an outage that resolved nothing as a clean no-op run.
    if stopped_on_outage:
        return 1
    return 0 if (written or not seen) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="two-letter code; omit for every state")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH)
    parser.add_argument("--batches", type=int, help="stop after N batches (sampling)")
    parser.add_argument("--dry-run", action="store_true", help="report match rate, write nothing")
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help="run even if the geocoder probe fails (it probes a different endpoint)",
    )
    args = parser.parse_args()

    if not (1 <= args.batch_size <= _MAX_BATCH):
        print(f"--batch-size must be 1..{_MAX_BATCH}", file=sys.stderr)
        return 2

    async def _main() -> int:
        await init_pool()
        try:
            return await run(
                state=(args.state or "").strip().upper() or None,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                limit_batches=args.batches,
                skip_preflight=args.skip_preflight,
            )
        finally:
            await close_pool()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
