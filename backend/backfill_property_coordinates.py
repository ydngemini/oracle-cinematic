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

        # Retry the SAME page rather than advancing past it. geocode_batch
        # degrades a transport failure into "every row unmatched", and Census is
        # intermittent — so an outage would otherwise walk the cursor through all
        # 8.2M rows asking nothing, marking them seen, and reporting 0% as though
        # the addresses were unmatchable. Real data matches ~90%, so a full batch
        # resolving nothing is far more likely to be the endpoint than the data.
        matched: list[tuple[str, float, float]] = []
        results: list[dict] = []
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
            if attempt < _BATCH_ATTEMPTS:
                backoff = _BATCH_BACKOFF_SECONDS * attempt
                print(
                    f"  batch {batches + 1}: 0 of {len(rows)} resolved — "
                    f"retrying the same page in {backoff}s ({attempt}/{_BATCH_ATTEMPTS - 1})",
                    flush=True,
                )
                await asyncio.sleep(backoff)

        if not matched and len(rows) >= _OUTAGE_MIN_BATCH:
            print(
                f"!! {len(rows)} consecutive addresses resolved to nothing after "
                f"{_BATCH_ATTEMPTS} attempts. Treating this as a geocoder outage "
                f"rather than unmatchable data, and stopping so the remaining rows "
                f"stay queued. Re-run to resume from here.",
                file=sys.stderr,
            )
            stopped_on_outage = True
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
