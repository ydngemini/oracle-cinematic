"""Golden MLS flow — the real stack, not mocks.

The Python suite runs on fakes (tests/conftest.py, ci.yml), which proves the
application's decisions and nothing about Postgres. This proves the path:

    provider fixture -> canonical sink -> PostgreSQL -> search -> detail
                     -> delta sync -> freshness -> buyer match -> isolation

Ten assertions, §49's list. It seeds, asserts, and cleans up after itself.
No live provider is contacted — the "provider" is a dict, so this is
deterministic and safe to run anywhere there is a database.

Run it against a database with migrations applied:

    docker exec -w /app oracle-backend-1 python tests/golden_mls_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

FEED = "golden_feed"
TENANT_A = "0e000000-0000-0000-0000-00000000000a"
TENANT_B = "0e000000-0000-0000-0000-00000000000b"

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def connect():
    return await asyncpg.connect(
        host=os.environ["ORACLE_DB_HOST"], port=int(os.environ["ORACLE_DB_PORT"]),
        user=os.environ["ORACLE_DB_USER"], password=os.environ["ORACLE_DB_PASSWORD"],
        database=os.environ["ORACLE_DB_NAME"], ssl=False,
    )


def provider_record(*, price: int, status: str, modified: datetime) -> dict:
    """One listing in the canonical shape a normalizer emits.

    Every key is present because upsert_mls_records indexes rather than .get()s
    — a normalizer that drops a field should fail loudly here, not write NULL.
    """
    return {
        "mls_id": FEED, "mls_number": "GOLD-1",
        "address": "1 Golden Way", "city": "Wilmington",
        "state_code": "DE", "zip_code": "19801", "county": "New Castle",
        "latitude": 39.7447, "longitude": -75.5484,
        "list_price": price, "orig_list_price": 485000,
        "status": status, "property_type": "Single Family",
        "beds": 4, "baths_full": 2, "baths_half": 1,
        "sqft": 2100, "lot_sqft": 6000, "year_built": 1998,
        "hoa_monthly": None, "days_on_market": 12,
        "list_date": None, "close_date": None, "close_price": None,
        "description": "A test listing.", "photos": [],
        "features": {"source_modified_at": modified.isoformat(),
                     "source_status": status,
                     "provenance": {"provider_id": FEED}},
    }


async def cleanup(conn):
    await conn.execute("DELETE FROM oracle_mls_listings WHERE mls_id=$1", FEED)
    await conn.execute("DELETE FROM mls_sync_status WHERE mls_id=$1", FEED)
    await conn.execute("DELETE FROM mls_feed_entitlements WHERE mls_id=$1", FEED)
    await conn.execute("DELETE FROM clients WHERE tenant_id=ANY($1::uuid[])", [TENANT_A, TENANT_B])
    await conn.execute("DELETE FROM tenants WHERE id=ANY($1::uuid[])", [TENANT_A, TENANT_B])


async def main() -> int:
    from data_integrations.mls_sink import upsert_mls_records_and_status
    from mls_health import compute_health, mls_capability, visible_feeds
    from buyer_matching import buyers_for_listing
    from tenancy import Role, TenantContext

    conn = await connect()
    ctx_a = TenantContext(agent_id="a@golden.test", tenant_id=TENANT_A, role=Role.BROKER_OWNER)
    ctx_b = TenantContext(agent_id="b@golden.test", tenant_id=TENANT_B, role=Role.BROKER_OWNER)

    os.environ["ORACLE_MLS_LICENSED"] = "1"
    os.environ["ORACLE_MLS_AGREEMENT_REF"] = "GOLDEN-TEST-001"

    try:
        # Ingestion runs as platform_admin — it is a system process with no
        # tenant of its own. Setting it here is what tenant_tx does in the app;
        # without it 0109's RLS on `tenants` correctly refuses the seed insert.
        await conn.execute(
            "SELECT set_config('app.current_role','platform_admin',false),"
            "       set_config('app.current_tenant','00000000-0000-0000-0000-000000000000',false)")
        await cleanup(conn)
        await conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES ($1,'golden-a','Golden A'),($2,'golden-b','Golden B')",
            TENANT_A, TENANT_B)

        # 1 ── empty
        n = await conn.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id=$1", FEED)
        check("1. database starts empty for this feed", n == 0)

        # 2 ── ingest
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        await upsert_mls_records_and_status(
            conn, records=[provider_record(price=485000, status="Active", modified=t0)],
            mls_id=FEED, mls_name="Golden MLS", feed_type="Bridge_API_v2",
            last_sync_at=t0, provider="bridge", dataset="goldenmls",
            succeeded=True, backfill_complete=True)
        row = await conn.fetchrow(
            "SELECT list_price, status, license_classification FROM oracle_mls_listings WHERE mls_id=$1", FEED)
        check("2. listing ingested through the canonical sink", row is not None and row["list_price"] == 485000)
        check("2b. it was written as licensed data", row["license_classification"] == "licensed_property_listing",
              row["license_classification"])

        # 10 ── isolation (checked early: entitlement not yet granted)
        allowed_b, _ = await visible_feeds(conn, ctx_b)
        check("10. an unentitled tenant cannot see a licensed feed", FEED not in allowed_b)

        await conn.execute(
            "INSERT INTO mls_feed_entitlements (tenant_id, mls_id, granted_by) VALUES ($1,$2,'golden-test')",
            TENANT_A, FEED)
        allowed_a, feeds_a = await visible_feeds(conn, ctx_a)
        check("10b. the entitled tenant can", FEED in allowed_a)

        # 3 ── search
        found = await conn.fetch(
            "SELECT id, address FROM oracle_mls_listings WHERE mls_id=ANY($1::text[]) AND state_code='DE'",
            allowed_a)
        check("3. search returns it", len(found) == 1 and found[0]["address"] == "1 Golden Way")

        # 4 ── detail
        detail = await conn.fetchrow(
            "SELECT * FROM oracle_mls_listings WHERE id=$1::uuid AND mls_id=ANY($2::text[])",
            found[0]["id"], allowed_a)
        check("4. detail returns it", detail is not None)

        # 5+6 ── provider changes price and status; delta sync
        t1 = datetime.now(timezone.utc)
        await upsert_mls_records_and_status(
            conn, records=[provider_record(price=460000, status="Pending", modified=t1)],
            mls_id=FEED, mls_name="Golden MLS", feed_type="Bridge_API_v2",
            last_sync_at=t1, provider="bridge", dataset="goldenmls", succeeded=True)
        after = await conn.fetch("SELECT list_price, status FROM oracle_mls_listings WHERE mls_id=$1", FEED)
        check("5/6. delta sync applied the price and status change",
              len(after) == 1 and after[0]["list_price"] == 460000 and after[0]["status"] == "Pending")

        # 7 ── no duplicate
        check("7. the listing updated rather than duplicating", len(after) == 1, f"{len(after)} rows")

        # 8 ── freshness
        status_row = dict(await conn.fetchrow(
            "SELECT last_success_at, backfill_complete, consecutive_failures, "
            "       last_error_class, stale_after_minutes FROM mls_sync_status WHERE mls_id=$1", FEED))
        check("8. feed freshness updated and reads READY",
              compute_health(status_row) == "READY", compute_health(status_row))

        # 9 ── buyer match sees the UPDATED listing
        await conn.execute(
            "INSERT INTO clients (tenant_id, full_name, client_type, preferences) "
            "VALUES ($1,'Sarah Golden','buyer',$2::jsonb)",
            TENANT_A, '{"target_zips":["19801"],"budget_max":475000,"beds":3}')
        await conn.execute("SELECT set_config('app.current_tenant',$1,true),"
                           "       set_config('app.current_role','broker_owner',true)", TENANT_A)
        buyers = await buyers_for_listing(conn, dict(detail) | {"list_price": 460000})
        sarah = next((b for b in buyers if b.name == "Sarah Golden"), None)
        # At 485k she was over budget; at 460k she is inside it.
        check("9. buyer match sees the UPDATED price", sarah is not None and sarah.verdict == "strong",
              f"{sarah.verdict if sarah else 'not found'}")
        check("9b. the match carries readable evidence", bool(sarah and sarah.evidence),
              "; ".join(sarah.evidence) if sarah else "")

        # Back to the ingestion identity for the readiness reads.
        await conn.execute(
            "SELECT set_config('app.current_role','platform_admin',false),"
            "       set_config('app.current_tenant','00000000-0000-0000-0000-000000000000',false)")

        # readiness gate
        cap = await mls_capability(conn, ctx_a)
        check("11. brokerage readiness reports READY for a licensed fresh feed",
              cap["status"] == "READY", cap["status"])
        cap_b = await mls_capability(conn, ctx_b)
        check("12. the unentitled tenant is NOT_STARTED", cap_b["status"] == "NOT_STARTED", cap_b["status"])

    finally:
        await cleanup(conn)
        await conn.close()

    print()
    if failures:
        print(f"GOLDEN FLOW FAILED — {len(failures)} assertion(s): {', '.join(failures)}")
        return 1
    print("GOLDEN FLOW PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
