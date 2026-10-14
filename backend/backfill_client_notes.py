#!/usr/bin/env python3
"""Seal the plaintext client notes written before migration 0132 (audit A01).

Restart-safe: each batch selects only rows still in the other shape and commits
on its own, so an interrupted run loses nothing and the next run carries on.
Batches lock with SKIP LOCKED, so the app keeps writing while it runs. A
seal-only rewrite keeps the note's updated_at (0132 trigger), so no note
starts showing as "edited". Prints counts and tenant ids, never note text.

    python backfill_client_notes.py           # seal plaintext rows
    python backfill_client_notes.py --unseal  # rollback: plaintext back (see 0132)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from client_note_crypto import _key
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

_ACTOR = "client-note-seal-backfill"

_SEAL = """
    UPDATE client_notes
       SET body_ciphertext = pgp_sym_encrypt(body, $2::text), body = ''
     WHERE id IN (SELECT id FROM client_notes
                   WHERE tenant_id = $1::uuid AND body_ciphertext IS NULL AND body <> ''
                   ORDER BY id LIMIT $3
                   FOR UPDATE SKIP LOCKED)
    RETURNING id
"""

_UNSEAL = """
    UPDATE client_notes
       SET body = pgp_sym_decrypt(body_ciphertext, $2::text), body_ciphertext = NULL
     WHERE id IN (SELECT id FROM client_notes
                   WHERE tenant_id = $1::uuid AND body_ciphertext IS NOT NULL
                   ORDER BY id LIMIT $3
                   FOR UPDATE SKIP LOCKED)
    RETURNING id
"""


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


async def backfill_tenant(tenant_id: str, *, batch_size: int, unseal: bool = False,
                          max_rows: int | None = None) -> int:
    """Seal (or unseal) one tenant's notes; return how many rows changed.
    `max_rows` stops after about that many (whole batches), for the periodic pass."""
    key = _key(tenant_id)
    changed = 0
    while max_rows is None or changed < max_rows:
        async with tenant_tx(_ctx(tenant_id)) as conn:
            await conn.execute("SELECT set_config('app.client_note_reseal', 'on', true)")
            rows = await conn.fetch(_UNSEAL if unseal else _SEAL, tenant_id, key, batch_size)
        if not rows:
            return changed
        changed += len(rows)
        print(f"tenant {tenant_id}: {'unsealed' if unseal else 'sealed'} {changed} note(s)", flush=True)
    return changed


async def seal_pending(*, batch_size: int = 200, max_rows: int = 2_000,
                       only_tenants: list[str] | None = None) -> dict:
    """One bounded sealing pass over every tenant that still has plaintext notes.

    The worker runs this on a schedule (data_integrations/periodic.py,
    `client_note_seal`) because the master key lives only in the app's own
    environment: nobody has to copy it out to run the script. Returns counts and
    tenant ids, never note text. Unsealing stays a deliberate operator action.
    """
    async with tenant_tx(_ctx("00000000-0000-0000-0000-000000000000")) as conn:
        tenant_ids = [str(r["tenant_id"]) for r in await conn.fetch(
            "SELECT DISTINCT tenant_id FROM client_notes "
            "WHERE body_ciphertext IS NULL AND body <> '' ORDER BY tenant_id")]
    if only_tenants is not None:
        tenant_ids = [t for t in tenant_ids if t in set(only_tenants)]
    sealed, budget = 0, max_rows
    for tenant_id in tenant_ids:
        if budget <= 0:
            break
        done = await backfill_tenant(tenant_id, batch_size=min(batch_size, budget), max_rows=budget)
        sealed += done
        budget -= done
    left = 0
    for tenant_id in tenant_ids:
        left += await remaining(tenant_id)
    return {"sealed": sealed, "tenants": len(tenant_ids), "remaining": left}


async def remaining(tenant_id: str) -> int:
    async with tenant_tx(_ctx(tenant_id)) as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM client_notes WHERE tenant_id=$1::uuid "
            "AND body_ciphertext IS NULL AND body <> ''", tenant_id))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--unseal", action="store_true", help="rollback: write the plaintext back")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    batch_size = int(os.getenv("ORACLE_CLIENT_NOTE_BACKFILL_BATCH", "500"))
    if not 1 <= batch_size <= 5_000:
        print("ORACLE_CLIENT_NOTE_BACKFILL_BATCH must be 1..5000", file=sys.stderr)
        return 2
    await init_pool(min_size=1, max_size=2)
    total, left = 0, 0
    try:
        async with tenant_tx(_ctx("00000000-0000-0000-0000-000000000000")) as conn:
            tenant_ids = [str(r["tenant_id"]) for r in await conn.fetch(
                "SELECT DISTINCT tenant_id FROM client_notes ORDER BY tenant_id")]
        for tenant_id in tenant_ids:
            total += await backfill_tenant(tenant_id, batch_size=batch_size, unseal=args.unseal)
            if not args.unseal:
                left += await remaining(tenant_id)
    finally:
        await close_pool()
    print(f"client note {'unseal' if args.unseal else 'seal'} complete ({total} note(s))", flush=True)
    if left:
        # Rows locked by a concurrent writer were skipped; run again.
        print(f"{left} plaintext note(s) remain — run again", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
