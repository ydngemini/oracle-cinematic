#!/usr/bin/env python3
"""Backfill tenant-keyed contact-name blind indexes without logging PII.

Run after migration 0059 and before serving name search in a production
cutover. The job is restart-safe: only rows with no tokens are selected.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from contact_truth import name_search_tokens, open_json
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext


async def _backfill_tenant(tenant_id: str, *, batch_size: int) -> tuple[int, int]:
    """Index every nameable contact in one tenant; return (indexed, unnameable).

    A contact with no canonical name cannot be tokenized. That used to raise and
    abort the whole run on the first one — and migration 0054 seeds
    agent_contacts from clients WITHOUT copying plaintext PII, so those rows
    carry pii_ciphertext=NULL and a legacy full_name that may also be NULL.
    A single such row therefore stopped every remaining contact, in every
    remaining tenant, from ever becoming searchable.

    They are now skipped and counted instead. Skipped ids are carried forward in
    the exclusion list because the batch query selects on
    ``cardinality(name_search_tokens)=0`` — a skipped row still matches that, so
    without excluding it the loop would re-select the same batch forever.
    """
    ctx = TenantContext(
        agent_id="contact-search-backfill",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    updated = 0
    unnameable: list[Any] = []
    while True:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT contact.id,contact.pii_ciphertext,
                       legacy.full_name AS legacy_full_name
                  FROM agent_contacts contact
                  LEFT JOIN LATERAL (
                      SELECT client.full_name
                        FROM clients client
                       WHERE client.contact_id=contact.id
                          OR client.id=contact.legacy_client_id
                       ORDER BY (client.id=contact.legacy_client_id) DESC,client.created_at
                       LIMIT 1
                  ) legacy ON true
                 WHERE contact.tenant_id=$1::uuid
                   AND contact.deleted_at IS NULL
                   AND cardinality(contact.name_search_tokens)=0
                   AND contact.id <> ALL($3::uuid[])
                 ORDER BY contact.id
                 LIMIT $2
                 FOR UPDATE OF contact SKIP LOCKED
                """,
                tenant_id,
                batch_size,
                unnameable,
            )
            if not rows:
                return updated, len(unnameable)
            updates: list[tuple[list[str], Any]] = []
            for row in rows:
                if row["pii_ciphertext"]:
                    payload = await open_json(conn, tenant_id, row["pii_ciphertext"])
                    full_name = payload.get("full_name")
                else:
                    full_name = row["legacy_full_name"]
                if not isinstance(full_name, str) or not full_name.strip():
                    # Report the id, never the name — this job exists precisely
                    # to avoid logging PII.
                    unnameable.append(row["id"])
                    continue
                updates.append((name_search_tokens(tenant_id, full_name), row["id"]))
            if updates:
                await conn.executemany(
                    """
                    UPDATE agent_contacts SET name_search_tokens=$1::text[]
                     WHERE tenant_id=$2::uuid AND id=$3::uuid
                    """,
                    [(tokens, tenant_id, contact_id) for tokens, contact_id in updates],
                )
                updated += len(updates)
                print(
                    f"tenant {tenant_id}: indexed {updated} contact(s)",
                    flush=True,
                )


async def main() -> int:
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    batch_size = int(os.getenv("ORACLE_CONTACT_SEARCH_BACKFILL_BATCH", "250"))
    if not 1 <= batch_size <= 2_000:
        print("ORACLE_CONTACT_SEARCH_BACKFILL_BATCH must be 1..2000", file=sys.stderr)
        return 2
    await init_pool(min_size=1, max_size=2)
    total = 0
    skipped = 0
    try:
        bootstrap = TenantContext(
            agent_id="contact-search-backfill",
            tenant_id="00000000-0000-0000-0000-000000000000",
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(bootstrap) as conn:
            tenant_ids = [str(row["id"]) for row in await conn.fetch("SELECT id FROM tenants ORDER BY id")]
        for tenant_id in tenant_ids:
            indexed, unnameable = await _backfill_tenant(tenant_id, batch_size=batch_size)
            total += indexed
            skipped += unnameable
            if unnameable:
                print(
                    f"tenant {tenant_id}: {unnameable} contact(s) have no canonical "
                    f"name and stay unsearchable by name",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        await close_pool()
    print(f"contact search backfill complete ({total} contact(s))", flush=True)
    if skipped:
        # Exit non-zero so a cutover runbook notices. The indexing that could be
        # done HAS been done and committed; this reports what could not.
        print(
            f"{skipped} contact(s) skipped for want of a name — they remain "
            f"invisible to name search until one is supplied",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
