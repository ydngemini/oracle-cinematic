"""MLS failure drill: the real RESO sync code against the mock feed.

Runs inside the perf network (performance/resilience/run_mls_drill.sh). Uses a
dedicated feed id `drillmock` and deletes its rows at the end. Phases:

  1 healthy     rows land, feed READY/STALE-free, cursor advances
  2 auth (401)  sync fails fast (no retry storm), feed AUTH_ERROR, rows kept
  3 outage 503  retried with backoff, then fails, rows kept, cursor unchanged
  4 corrupt     every 10th record refused by the database: the rest persist,
                rejections are counted (the whole page used to be lost)
  5 recovered   sync succeeds again from the cursor
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/app")
FEED = "drillmock"
MOCK = "http://oracle-perf-mock:9000"


def _mock(cfg: dict) -> None:
    import urllib.request

    req = urllib.request.Request(f"{MOCK}/config", data=json.dumps(cfg).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


async def _state(conn) -> dict:
    import mls_health

    row = await conn.fetchrow("SELECT * FROM mls_sync_status WHERE mls_id=$1", FEED)
    rows = await conn.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id=$1", FEED)
    return {"health": mls_health.compute_health(dict(row)) if row else None, "rows": rows,
            "cursor": str(row["last_success_at"]) if row else None,
            "notes": row["notes"] if row else None, "error_class": row["last_error_class"] if row else None}


async def main() -> dict:
    os.environ["ORACLE_RESO_FEEDS_JSON"] = json.dumps(
        [{"id": FEED, "name": "Drill mock MLS", "url": f"{MOCK}/reso/Property", "token": "drill-token",
          "page_size": 25, "lookback_hours": 1}])
    from data_integrations.listings_feed import RESOListingsAggregator
    from data_integrations import base
    from db import connection
    from db.connection import tenant_tx
    from tenancy import Role, TenantContext

    base.RetryConfig.base_backoff = 0.2      # keep the drill short; semantics unchanged
    ctx = TenantContext(agent_id="mls-drill", tenant_id=os.environ["ORACLE_INGEST_TENANT_ID"], role=Role.PLATFORM_ADMIN)
    await connection.init_pool(min_size=1, max_size=4)
    results = {}
    try:
        async def phase(name, cfg):
            _mock({"faults": [], "corrupt_every": 0, **cfg})
            t0 = time.monotonic()
            out = await RESOListingsAggregator().sync_once()
            async with tenant_tx(ctx) as conn:
                state = await _state(conn)
            results[name] = {"seconds": round(time.monotonic() - t0, 1),
                             "sync_state": out.get("state"), "feed": state,
                             "board": [{k: f.get(k) for k in ("state", "error", "upserted")} for f in out.get("feeds", [])]}
            print(name, json.dumps(results[name], default=str), flush=True)

        await phase("1_healthy", {"reso_listings": 120})
        await phase("2_auth_401", {"faults": [{"match": "/reso", "mode": "401"}]})
        await phase("3_outage_503", {"faults": [{"match": "/reso", "mode": "503", "retry_after": 1}]})
        await phase("4_corrupt_rows", {"corrupt_every": 10, "reso_listings": 140})
        await phase("5_recovered", {"reso_listings": 140})
    finally:
        _mock({"faults": [], "corrupt_every": 0})
        async with tenant_tx(ctx) as conn:
            await conn.execute("DELETE FROM oracle_mls_listings WHERE mls_id=$1", FEED)
            await conn.execute("DELETE FROM mls_sync_status WHERE mls_id=$1", FEED)
        await connection.close_pool()
    return results


if __name__ == "__main__":
    if os.getenv("NEOH_LOAD_TEST_ALLOWED") != "1":
        sys.exit("refusing: local perf topology only")
    out = asyncio.run(main())
    print("SUMMARY " + json.dumps(out, default=str), flush=True)
