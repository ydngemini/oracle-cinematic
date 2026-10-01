#!/usr/bin/env python3
"""§39 MLS INGESTION — the real sink, synthetic listings, no licensed provider.

Writes N synthetic listings through data_integrations.mls_sink (the same
upsert every live feed uses) in provider-sized batches, under mls_id
`perf-mls` classified as developer data, and reports records/s per batch.
A second pass (--update) re-upserts the same keys with new prices — a delta
sync, which is the steady-state production shape.

Runs inside a backend-image container on the perf network:
  python /perf/mls_ingest.py --records 50000 --batch 500 [--update]
  python /perf/mls_ingest.py --cleanup
Output: one JSON line (records, seconds, records/s, per-batch p50/p95 ms).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import sys
import time

sys.path.insert(0, "/app")

MLS_ID = "perf-mls"
CITIES = [("Austin", "TX", "78701", 30.27, -97.74), ("Dallas", "TX", "75201", 32.78, -96.80),
          ("Houston", "TX", "77002", 29.76, -95.37), ("San Antonio", "TX", "78205", 29.42, -98.49)]
TYPES = ["Residential", "Condominium", "Townhouse", "Land"]


def record(i: int, price_bump: int) -> dict:
    rnd = random.Random(i)
    city, st, zp, lat, lon = CITIES[i % len(CITIES)]
    price = 150_000 + rnd.randrange(900_000) + price_bump
    return {
        "mls_id": MLS_ID, "mls_number": f"PERF{i:08d}", "address": f"{100 + i % 9000} Perf Load Rd",
        "city": city, "state_code": st, "zip_code": zp, "county": f"{city} County",
        "latitude": lat + rnd.uniform(-0.2, 0.2), "longitude": lon + rnd.uniform(-0.2, 0.2),
        "list_price": price, "orig_list_price": price, "status": "Active" if i % 7 else "Pending",
        "property_type": TYPES[i % len(TYPES)], "beds": 1 + i % 5, "baths_full": 1 + i % 3, "baths_half": i % 2,
        "sqft": 700 + rnd.randrange(3500), "lot_sqft": 2000 + rnd.randrange(20000), "year_built": 1950 + i % 70,
        "hoa_monthly": 0, "days_on_market": i % 120, "list_date": dt.date(2026, 1, 1) + dt.timedelta(days=i % 250),
        "close_date": None, "close_price": None, "description": "Synthetic load-test listing.", "photos": [],
        "features": {"source_modified_at": dt.datetime.now(dt.timezone.utc).isoformat(), "parcel_number": f"P-{i:08d}"},
    }


async def main(a) -> int:
    import asyncpg
    from data_integrations.mls_sink import upsert_mls_records_and_status

    conn = await asyncpg.connect(os.environ["PERF_DB_DSN"])
    try:
        if a.cleanup:
            n = await conn.execute("DELETE FROM oracle_mls_listings WHERE mls_id = $1", MLS_ID)
            await conn.execute("DELETE FROM mls_sync_status WHERE mls_id = $1", MLS_ID)
            print(json.dumps({"cleanup": n}))
            return 0
        bump = 5_000 if a.update else 0
        t0 = time.perf_counter()
        batch_ms = []
        for start in range(0, a.records, a.batch):
            recs = [record(i, bump) for i in range(start, min(a.records, start + a.batch))]
            b0 = time.perf_counter()
            async with conn.transaction():
                await upsert_mls_records_and_status(
                    conn, records=recs, mls_id=MLS_ID, mls_name="Perf synthetic feed", feed_type="perf",
                    last_sync_at=dt.datetime.now(dt.timezone.utc), provider="perf", dataset=MLS_ID,
                    succeeded=True)
            batch_ms.append((time.perf_counter() - b0) * 1000)
            if a.pause:
                await asyncio.sleep(a.pause)
        total = time.perf_counter() - t0
        s = sorted(batch_ms)
        print(json.dumps({"records": a.records, "batch": a.batch, "update": a.update,
                          "seconds": round(total, 1), "records_per_s": round(a.records / total, 1),
                          "batch_ms_p50": round(s[len(s) // 2], 1), "batch_ms_p95": round(s[int(len(s) * .95)], 1),
                          "batch_ms_max": round(s[-1], 1)}))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=10_000)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between batches (pacing)")
    ap.add_argument("--cleanup", action="store_true")
    if os.environ.get("NEOH_LOAD_TEST_ALLOWED") != "1":
        print("REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(ap.parse_args())))
