#!/usr/bin/env python3
"""Seal the plaintext message bodies, call transcript excerpts and chat turns
written before migration 0152 (audit A01, ledger L17-21). Operator script.

Bounded, resumable, idempotent: each batch selects only rows still in the
plaintext shape (at most ORACLE_SEALED_TEXT_BACKFILL_BATCH, default 500) and
commits on its own, so an interrupted run loses nothing and the next run
carries on; a second run over a sealed table changes nothing. Batches lock with
SKIP LOCKED, so the app keeps writing while it runs. negotiation_events and
user_interactions are append-only to oracle_app, so their batches go through
the 0152 SECURITY DEFINER functions, which can only move a row's own text
between its two columns. Prints counts, tenant ids and timings, never text.

    python backfill_sealed_text.py                    # all three fields
    python backfill_sealed_text.py --field sms        # one field
    python backfill_sealed_text.py --count            # remaining only, no writes
    python backfill_sealed_text.py --max-rows 10000   # stop after ~N rows per field
    python backfill_sealed_text.py --unseal           # rollback: plaintext back (see 0152)

Not a periodic job: staging runs ORACLE_RECOVERY_MODE, where no periodic job
runs (L17-03). Run it where ORACLE_ENCRYPTION_MASTER_KEY is already set (the
app's own environment), after the 0152 code serves. Exit 1 = rows remain
(locked by a writer, or a tenant id that is not a UUID); run again.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

import sealed_text
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

_ACTOR = "sealed-text-backfill"
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"

# Literal statements only (bandit B608). $1 tenant, $2 key, $3 batch size.
_SMS_SEAL = """
    UPDATE sms_messages
       SET body_ciphertext = pgp_sym_encrypt(body, $2::text, 's2k-mode=1'), body = NULL
     WHERE id IN (SELECT id FROM sms_messages
                   WHERE tenant_id = $1::uuid AND body_ciphertext IS NULL
                     AND COALESCE(body, '') <> ''
                   ORDER BY id LIMIT $3
                   FOR UPDATE SKIP LOCKED)
    RETURNING id
"""
_SMS_UNSEAL = """
    UPDATE sms_messages
       SET body = pgp_sym_decrypt(body_ciphertext, $2::text), body_ciphertext = NULL
     WHERE id IN (SELECT id FROM sms_messages
                   WHERE tenant_id = $1::uuid AND body_ciphertext IS NOT NULL
                   ORDER BY id LIMIT $3
                   FOR UPDATE SKIP LOCKED)
    RETURNING id
"""
_TRANSCRIPT = "SELECT seal_negotiation_transcripts($1::uuid, $2::text, $3::int, $4::boolean)"
_INTERACTION = "SELECT seal_user_interactions($1::text, $2::text, $3::int, $4::boolean)"

# Tenants with rows to seal / to unseal.
_SEAL_TENANTS = {
    "sms": "SELECT DISTINCT tenant_id::text AS t FROM sms_messages "
           "WHERE body_ciphertext IS NULL AND COALESCE(body, '') <> '' ORDER BY 1",
    "transcript": "SELECT DISTINCT tenant_id::text AS t FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NULL AND COALESCE(transcript_excerpt, '') <> '' ORDER BY 1",
    "interaction": "SELECT DISTINCT tenant_id AS t FROM user_interactions "
                   "WHERE content_ciphertext IS NULL AND content <> '' ORDER BY 1",
}
_UNSEAL_TENANTS = {
    "sms": "SELECT DISTINCT tenant_id::text AS t FROM sms_messages WHERE body_ciphertext IS NOT NULL ORDER BY 1",
    "transcript": "SELECT DISTINCT tenant_id::text AS t FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NOT NULL ORDER BY 1",
    "interaction": "SELECT DISTINCT tenant_id AS t FROM user_interactions "
                   "WHERE content_ciphertext IS NOT NULL ORDER BY 1",
}
_REMAINING = {
    "sms": "SELECT count(*) FROM sms_messages WHERE body_ciphertext IS NULL AND COALESCE(body, '') <> ''",
    "transcript": "SELECT count(*) FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NULL AND COALESCE(transcript_excerpt, '') <> ''",
    "interaction": "SELECT count(*) FROM user_interactions WHERE content_ciphertext IS NULL AND content <> ''",
}
FIELDS = tuple(_REMAINING)


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


async def _batch(field: str, tenant_id: str, key: str, size: int, unseal: bool) -> int:
    async with tenant_tx(_ctx(tenant_id)) as conn:
        if field == "sms":
            return len(await conn.fetch(_SMS_UNSEAL if unseal else _SMS_SEAL, tenant_id, key, size))
        sql = _TRANSCRIPT if field == "transcript" else _INTERACTION
        return int(await conn.fetchval(sql, tenant_id, key, size, unseal) or 0)


async def tenants_pending(field: str, *, unseal: bool = False) -> list[str]:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return [str(r["t"]) for r in await conn.fetch((_UNSEAL_TENANTS if unseal else _SEAL_TENANTS)[field])]


async def remaining(field: str) -> int:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return int(await conn.fetchval(_REMAINING[field]))


async def backfill_field(field: str, *, batch_size: int, unseal: bool = False,
                         max_rows: int | None = None) -> dict:
    """Seal (or unseal) one field across every tenant. Returns counts and the
    measured time; `max_rows` stops after about that many (whole batches)."""
    started = time.monotonic()
    changed, skipped = 0, []
    for tenant_id in await tenants_pending(field, unseal=unseal):
        if not _is_uuid(tenant_id):
            skipped.append(tenant_id)  # text tenant id (user_interactions): no context can own it
            continue
        key = sealed_text.key_for(tenant_id)
        while max_rows is None or changed < max_rows:
            t0 = time.monotonic()
            done = await _batch(field, tenant_id, key, batch_size, unseal)
            if not done:
                break
            changed += done
            ms = (time.monotonic() - t0) * 1000
            print(f"{field} tenant {tenant_id}: {'unsealed' if unseal else 'sealed'} {done} row(s) "
                  f"in {ms:.0f} ms ({changed} total)", flush=True)
        if max_rows is not None and changed >= max_rows:
            break
    seconds = time.monotonic() - started
    return {"field": field, "changed": changed, "seconds": round(seconds, 2),
            "rows_per_second": round(changed / seconds, 1) if seconds and changed else None,
            "skipped_tenants": len(skipped)}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--field", choices=FIELDS + ("all",), default="all")
    parser.add_argument("--unseal", action="store_true", help="rollback: write the plaintext back")
    parser.add_argument("--count", action="store_true", help="print remaining plaintext rows; change nothing")
    parser.add_argument("--max-rows", type=int, default=None, help="stop after about N rows per field")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    batch_size = int(os.getenv("ORACLE_SEALED_TEXT_BACKFILL_BATCH", "500"))
    if not 1 <= batch_size <= 5_000:
        print("ORACLE_SEALED_TEXT_BACKFILL_BATCH must be 1..5000", file=sys.stderr)
        return 2
    fields = FIELDS if args.field == "all" else (args.field,)
    await init_pool(min_size=1, max_size=2)
    left = 0
    try:
        for field in fields:
            if not args.count:
                print(await backfill_field(field, batch_size=batch_size, unseal=args.unseal,
                                           max_rows=args.max_rows), flush=True)
            if not args.unseal:
                n = await remaining(field)
                left += n
                print(f"{field}: {n} plaintext row(s) remain", flush=True)
    finally:
        await close_pool()
    if left and not args.count and args.max_rows is None:
        print(f"{left} plaintext row(s) remain — run again", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
