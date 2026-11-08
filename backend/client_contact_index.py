#!/usr/bin/env python3
"""Fill client_contact_index (migration 0156, audit A01, ledger L17-23): the
keyed email / phone lookup hashes and word-prefix name tokens for every client,
computed with the same functions as agent_contacts (contact_truth), so a
client and a contact with the same email produce the same hash.

The engine of the periodic client_contact_index task (internal-only) and an
operator script. A client needs filling when it has no index row or its row
is stale (the 0156 trigger marks it on every name / email / phone change).

Bounded, resumable, idempotent: each batch takes at most
ORACLE_CLIENT_CONTACT_INDEX_BATCH (default 500) clients of one tenant, locks
them FOR SHARE SKIP LOCKED, computes the values in the app (the key never
reaches the database) and writes them, all in one short transaction. FOR
SHARE makes a concurrent edit of the same client wait until the batch commits;
the edit's trigger then marks the row stale again, so old values are never
recorded as current. Runs in the platform context of each row's own tenant.
Prints counts, tenant ids and timings, never names, emails or phones.

    python client_contact_index.py               # fill everything pending
    python client_contact_index.py --count       # pending only, no writes
    python client_contact_index.py --max-rows N  # stop after about N clients

Exit 1 = clients remain (locked by a writer); run again.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Optional

import contact_truth
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

_ACTOR = "client-contact-index"
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"

# Literal statements only (bandit B608).
_PENDING = """
    SELECT c.id, c.full_name, c.email, c.phone
      FROM clients c
      LEFT JOIN client_contact_index i ON i.client_id = c.id
     WHERE c.tenant_id = $1::uuid AND (i.client_id IS NULL OR i.stale)
     ORDER BY c.id
     LIMIT $2
       FOR SHARE OF c SKIP LOCKED
"""
_UPSERT = """
    INSERT INTO client_contact_index AS i
           (client_id, tenant_id, email_lookup_hash, phone_lookup_hash, name_search_tokens,
            email_unusable, phone_unusable, stale, indexed_at)
    SELECT x.client_id, $1::uuid, x.email_hash, x.phone_hash, t.tokens,
           x.email_unusable, x.phone_unusable, false, now()
      FROM unnest($2::uuid[], $3::text[], $4::text[], $5::text[], $6::boolean[], $7::boolean[])
           AS x(client_id, email_hash, phone_hash, tokens_csv, email_unusable, phone_unusable)
     CROSS JOIN LATERAL (SELECT COALESCE(string_to_array(NULLIF(x.tokens_csv, ''), ','),
                                         ARRAY[]::text[]) AS tokens) t
    ON CONFLICT (client_id) DO UPDATE
       SET email_lookup_hash = EXCLUDED.email_lookup_hash,
           phone_lookup_hash = EXCLUDED.phone_lookup_hash,
           name_search_tokens = EXCLUDED.name_search_tokens,
           email_unusable = EXCLUDED.email_unusable,
           phone_unusable = EXCLUDED.phone_unusable,
           stale = false,
           indexed_at = now()
"""
_TENANTS = """
    SELECT DISTINCT c.tenant_id AS t
      FROM clients c
      LEFT JOIN client_contact_index i ON i.client_id = c.id
     WHERE i.client_id IS NULL OR i.stale
"""
_REMAINING = """
    SELECT count(*)
      FROM clients c
      LEFT JOIN client_contact_index i ON i.client_id = c.id
     WHERE i.client_id IS NULL OR i.stale
"""


def index_values(tenant_id: str, full_name: Optional[str], email: Optional[str],
                 phone: Optional[str]) -> dict[str, Any]:
    """The three values for one client. A value that is present but cannot be
    normalised gets no hash and is flagged unusable, never guessed at; an
    empty name has no tokens."""
    out: dict[str, Any] = {"email_hash": None, "phone_hash": None, "tokens": [],
                           "email_unusable": False, "phone_unusable": False}
    for field, raw, normalise in (("email", email, contact_truth.normalize_email),
                                  ("phone", phone, contact_truth.normalize_phone)):
        try:
            normalised = normalise(raw)
        except ValueError:
            out[f"{field}_unusable"] = True
            continue
        out[f"{field}_hash"] = contact_truth.lookup_hash(tenant_id, field, normalised)
    try:
        out["tokens"] = contact_truth.name_search_tokens(tenant_id, full_name or "")
    except ValueError:  # empty after normalisation
        out["tokens"] = []
    return out


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


async def _batch(tenant_id: str, size: int) -> dict[str, int]:
    async with tenant_tx(_ctx(tenant_id)) as conn:
        rows = await conn.fetch(_PENDING, tenant_id, size)
        if not rows:
            return {"indexed": 0, "email_unusable": 0, "phone_unusable": 0}
        values = [index_values(tenant_id, r["full_name"], r["email"], r["phone"]) for r in rows]
        await conn.execute(
            _UPSERT, tenant_id,
            [r["id"] for r in rows],
            [v["email_hash"] for v in values],
            [v["phone_hash"] for v in values],
            [",".join(v["tokens"]) for v in values],
            [v["email_unusable"] for v in values],
            [v["phone_unusable"] for v in values],
        )
    return {"indexed": len(rows),
            "email_unusable": sum(v["email_unusable"] for v in values),
            "phone_unusable": sum(v["phone_unusable"] for v in values)}


async def remaining() -> int:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return int(await conn.fetchval(_REMAINING))


async def fill(*, batch_size: int, max_rows: Optional[int] = None) -> dict[str, Any]:
    """Index pending clients across every tenant; `max_rows` stops after about
    that many (whole batches). Returns counts and the measured time."""
    started = time.monotonic()
    totals = {"indexed": 0, "email_unusable": 0, "phone_unusable": 0}
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        tenants = [str(r["t"]) for r in await conn.fetch(_TENANTS)]
    for tenant_id in tenants:
        while max_rows is None or totals["indexed"] < max_rows:
            t0 = time.monotonic()
            done = await _batch(tenant_id, batch_size)
            if not done["indexed"]:
                break
            for k in totals:
                totals[k] += done[k]
            print(f"tenant {tenant_id}: indexed {done['indexed']} client(s) in "
                  f"{(time.monotonic() - t0) * 1000:.0f} ms ({totals['indexed']} total)", flush=True)
        if max_rows is not None and totals["indexed"] >= max_rows:
            break
    seconds = time.monotonic() - started
    return {**totals, "seconds": round(seconds, 2),
            "rows_per_second": round(totals["indexed"] / seconds, 1) if seconds and totals["indexed"] else None}


def batch_size_from_env() -> int:
    return max(1, min(5_000, int(os.getenv("ORACLE_CLIENT_CONTACT_INDEX_BATCH", "500"))))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", action="store_true", help="print pending clients; change nothing")
    parser.add_argument("--max-rows", type=int, default=None, help="stop after about N clients")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    await init_pool(min_size=1, max_size=2)
    try:
        if not args.count:
            print(await fill(batch_size=batch_size_from_env(), max_rows=args.max_rows), flush=True)
        left = await remaining()
        print(f"{left} client(s) pending", flush=True)
    finally:
        await close_pool()
    return 1 if left and not args.count and args.max_rows is None else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
