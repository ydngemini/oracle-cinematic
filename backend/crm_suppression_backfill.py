#!/usr/bin/env python3
"""Copy existing CRM do-not-contact flags into the outreach ledger (external
audit 26.1, ledger L04-07).

Since 26.1 a contact's suppression flags reach outreach_suppression in the
CRM edit's own transaction. Flags set BEFORE that release live only in
agent_contacts.suppression, which no send gate reads: this script writes them,
for the contact's own phone/email (opened from its sealed record) and for every
linked client's phone/email (clients.contact_id or the contact's
legacy_client_id), with the same mapping and normaliser the CRM edit uses
(outreach_compliance.record_crm_suppression).

Idempotent and insert-only (ON CONFLICT DO NOTHING): running it again adds
nothing, and it never lifts an opt-out. Bounded and resumable: keyset batches
of ORACLE_CRM_SUPPRESSION_BATCH (default 200) contacts per tenant, each in one
short transaction in the platform context of that tenant. Prints counts and
tenant ids only, never phones or emails.

    python crm_suppression_backfill.py            # write every flagged contact
    python crm_suppression_backfill.py --count    # flagged contacts only, no writes

Needs ORACLE_ENCRYPTION_MASTER_KEY (to open sealed contacts).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Optional

import outreach_compliance
from contact_truth import open_json
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

_ACTOR = "crm-suppression-backfill"
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"

# Literal statements only (bandit B608). A flag is any true value in the JSON.
_FLAGGED = """
    SELECT ac.id, ac.suppression, ac.pii_ciphertext,
           COALESCE((SELECT array_agg(c.phone) FROM clients c
                      WHERE c.tenant_id = ac.tenant_id
                        AND (c.contact_id = ac.id OR c.id = ac.legacy_client_id)), '{}') AS client_phones,
           COALESCE((SELECT array_agg(c.email) FROM clients c
                      WHERE c.tenant_id = ac.tenant_id
                        AND (c.contact_id = ac.id OR c.id = ac.legacy_client_id)), '{}') AS client_emails
      FROM agent_contacts ac
     WHERE ac.tenant_id = $1::uuid AND ac.deleted_at IS NULL
       AND ac.id > $2::uuid
       AND EXISTS (SELECT 1 FROM jsonb_each(ac.suppression) f WHERE f.value = 'true'::jsonb)
     ORDER BY ac.id
     LIMIT $3
"""
_TENANTS = """
    SELECT DISTINCT tenant_id AS t FROM agent_contacts
     WHERE deleted_at IS NULL
       AND EXISTS (SELECT 1 FROM jsonb_each(suppression) f WHERE f.value = 'true'::jsonb)
     ORDER BY 1
"""
_COUNT = """
    SELECT count(*) FROM agent_contacts
     WHERE deleted_at IS NULL
       AND EXISTS (SELECT 1 FROM jsonb_each(suppression) f WHERE f.value = 'true'::jsonb)
"""
_FIRST_ID = "00000000-0000-0000-0000-000000000000"


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


def _flags(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    return dict(raw or {})


async def _batch(tenant_id: str, after: str, size: int) -> tuple[Optional[str], int, int]:
    """(last id or None when done, contacts seen, ledger rows added)."""
    added = 0
    async with tenant_tx(_ctx(tenant_id)) as conn:
        rows = await conn.fetch(_FLAGGED, tenant_id, after, size)
        for row in rows:
            flags = _flags(row["suppression"])
            pii = await open_json(conn, tenant_id, row["pii_ciphertext"]) if row["pii_ciphertext"] else {}
            destinations = {(pii.get("phone"), pii.get("email"))}
            destinations |= set(zip(row["client_phones"] or [], row["client_emails"] or []))
            for phone, email in destinations:
                added += await outreach_compliance.record_crm_suppression(
                    conn, tenant_id, phone=phone, email=email, flags=flags,
                    source="CRM flag backfill (26.1)")
    return (str(rows[-1]["id"]) if rows else None), len(rows), added


async def backfill(*, batch_size: int) -> dict[str, Any]:
    started = time.monotonic()
    totals = {"contacts": 0, "rows_added": 0}
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        tenants = [str(r["t"]) for r in await conn.fetch(_TENANTS)]
    for tenant_id in tenants:
        after: Optional[str] = _FIRST_ID
        while after is not None:
            last, seen, added = await _batch(tenant_id, after, batch_size)
            totals["contacts"] += seen
            totals["rows_added"] += added
            if seen:
                print(f"tenant {tenant_id}: {seen} flagged contact(s), {added} ledger row(s) added", flush=True)
            after = last if seen == batch_size else None
    return {**totals, "seconds": round(time.monotonic() - started, 2)}


async def flagged_count() -> int:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return int(await conn.fetchval(_COUNT))


def batch_size_from_env() -> int:
    return max(1, min(2_000, int(os.getenv("ORACLE_CRM_SUPPRESSION_BATCH", "200"))))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", action="store_true", help="print flagged contacts; change nothing")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    await init_pool(min_size=1, max_size=2)
    try:
        print(f"{await flagged_count()} flagged contact(s)", flush=True)
        if not args.count:
            print(await backfill(batch_size=batch_size_from_env()), flush=True)
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
