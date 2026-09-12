#!/usr/bin/env python3
"""Backfill public_property_records.owner_name_normalized in small batches.

Migration 0102 added the column as a plain nullable `text` — not
`GENERATED ALWAYS AS (...) STORED` — specifically so this could run as a
batched backfill instead of one table-rewriting transaction. The generated-
column version was tried first and reverted: adding it forces an immediate
full-table rewrite under ACCESS EXCLUSIVE (no CONCURRENTLY exists for that),
and on this table (9.8M rows, 6.7GB, actively growing from live harvesters)
the rewrite's WAL plus its second relfilenode exceeded 8.9GB of free disk
mid-run. Caught via pg_cancel_backend before disk hit zero; no data lost, but
never again on this host. See 0102's header for the full story, including why
the column exists at all (an expression index that RLS silently made
unusable — this is the fix for it).

Idempotent and resumable: each batch only touches rows where
owner_name_normalized IS NULL, uses `FOR UPDATE SKIP LOCKED` so it never
blocks on — or double-processes — a row a concurrent harvester insert is
touching, and a re-run after an interruption just picks up wherever rows are
still NULL. The trigger migration 0102 also installed keeps every future
INSERT/UPDATE correct without this script's help; this is purely for the rows
that existed before the trigger did.

Deliberately unordered (no `ORDER BY id`): sorting every still-NULL row before
LIMIT could apply turned a 3k-row batch into a 166s external disk sort.
Dropping the order makes each batch a scan that stops as soon as it has
`--batch` matches — every row still gets covered over the life of the run,
just not in id order. Measured on this table under real load (harvesters
still writing, RLS enforced, default batch 1000): ~1-3s/batch, so the full
9.8M-row backfill is a multi-hour background job, not a one-shot. That is
fine — it never blocks the fix this exists for (0102's index already serves
`_property_candidates` correctly for every row it has covered, and for every
row written after the trigger went live).

    python backfill_owner_name_normalized.py                 # until done (hours; let it run)
    python backfill_owner_name_normalized.py --batch 1000
    python backfill_owner_name_normalized.py --dry-run        # is there work left?
    python backfill_owner_name_normalized.py --pause 1.0      # seconds between batches
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("owner-name-backfill")

# public_property_records has no tenant_id — platform-wide public data — but
# tenant_tx still needs a context to set the session GUCs.
_CTX = TenantContext(
    agent_id="owner-name-backfill",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)

_DEFAULT_BATCH = 1_000
_MAX_BATCH = 100_000


async def _has_pending(conn) -> bool:
    """Cheap existence check, not a count. `count(*)` with no LIMIT over 9.8M
    rows blows the pool's command_timeout=30 even with
    idx_public_property_owner_backfill_pending in play (an aggregate still has
    to visit every matching row); this only needs a yes/no answer."""
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM public_property_records "
        "WHERE owner_name_normalized IS NULL)"
    )


async def _run_batch(conn, batch_size: int, *, timeout: float) -> int:
    """One batch. Returns rows updated (0 means the backfill is complete).

    `timeout` overrides the pool's command_timeout=30 for this call only — a
    background backfill can afford to wait out a batch that runs long under
    concurrent harvester I/O; a live request-serving query never gets this.
    """
    # No ORDER BY: order doesn't matter for a backfill, and asking for one
    # forces a full sort of every still-NULL row before LIMIT can apply
    # (measured: an 800k-buffer external disk sort, ~166s for one 3k batch).
    # Without it this is a plain scan that stops as soon as it has `batch_size`
    # matches — the same total rows get covered over the life of the backfill,
    # just not in id order.
    status = await conn.execute(
        """
        WITH todo AS (
            SELECT id FROM public_property_records
             WHERE owner_name_normalized IS NULL
             LIMIT $1
               FOR UPDATE SKIP LOCKED
        )
        UPDATE public_property_records p
           SET owner_name_normalized =
                   regexp_replace(lower(COALESCE(p.owner_name, '')), '[^a-z0-9]', '', 'g')
          FROM todo
         WHERE p.id = todo.id
        """,
        batch_size,
        timeout=timeout,
    )
    # asyncpg execute() returns a command tag like "UPDATE 20000".
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=_DEFAULT_BATCH)
    parser.add_argument("--pause", type=float, default=0.25, help="seconds between batches")
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="per-batch statement timeout in seconds (overrides the pool's command_timeout=30 "
             "for this call only — safe here because this is offline maintenance, not a "
             "request-serving query)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report remaining count and exit")
    args = parser.parse_args()

    batch_size = max(100, min(_MAX_BATCH, args.batch))

    await init_pool()
    try:
        async with tenant_tx(_CTX) as conn:
            pending = await _has_pending(conn)
        logger.info("owner_name_normalized: %s", "rows still NULL" if pending else "fully backfilled")
        if args.dry_run or not pending:
            return

        total = 0
        t0 = time.monotonic()
        while True:
            async with tenant_tx(_CTX) as conn:
                updated = await _run_batch(conn, batch_size, timeout=args.timeout)
            if updated == 0:
                break
            total += updated
            logger.info(
                "batch: %d rows (%d total, %.0fs elapsed)",
                updated, total, time.monotonic() - t0,
            )
            await asyncio.sleep(args.pause)
        logger.info("done: %d rows backfilled in %.0fs", total, time.monotonic() - t0)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
