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
nothing, and it never lifts an opt-out. Soft-deleted contacts are included.
A sealed contact that cannot be opened is skipped (its clients' details are
still written) and listed by id, exit 1; five in a row stop the run (the key
is wrong), exit 2. Bounded and resumable: keyset batches
of ORACLE_CRM_SUPPRESSION_BATCH (default 200) contacts per tenant, each in one
short transaction in the platform context of that tenant. Prints counts and
tenant ids only, never phones or emails.

    python crm_suppression_backfill.py            # write every flagged contact
    python crm_suppression_backfill.py --count    # flagged contacts only, no writes
    python crm_suppression_backfill.py --tenant <brokerage id>   # one brokerage (repeatable)

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

# Literal statements only (bandit B608). A candidate is any contact with a
# flag that is not false/null (the CRM mapping then decides, as on an edit).
# Soft-deleted contacts are included: restoring one does not rewrite its
# flags, and its linked client stays reachable — insert-only, so this only
# blocks more.
_FLAGGED = """
    SELECT ac.id, ac.suppression, ac.pii_ciphertext,
           COALESCE((SELECT array_agg(ARRAY[c.phone, c.email]) FROM clients c
                      WHERE c.tenant_id = ac.tenant_id
                        AND (c.contact_id = ac.id OR c.id = ac.legacy_client_id)), '{}') AS client_details
      FROM agent_contacts ac
     WHERE ac.tenant_id = $1::uuid
       AND ac.id > $2::uuid
       AND EXISTS (SELECT 1 FROM jsonb_each(ac.suppression) f WHERE f.value NOT IN ('false'::jsonb, 'null'::jsonb))
     ORDER BY ac.id
     LIMIT $3
"""
_TENANTS = """
    SELECT DISTINCT tenant_id AS t FROM agent_contacts
     WHERE EXISTS (SELECT 1 FROM jsonb_each(suppression) f WHERE f.value NOT IN ('false'::jsonb, 'null'::jsonb))
     ORDER BY 1
"""
_COUNT = """
    SELECT count(*) FROM agent_contacts
     WHERE EXISTS (SELECT 1 FROM jsonb_each(suppression) f WHERE f.value NOT IN ('false'::jsonb, 'null'::jsonb))
"""
# Stop early when this many sealed contacts in a row cannot be opened: the
# key is wrong, not one record.
_UNREADABLE_IN_A_ROW = 5
_FIRST_ID = "00000000-0000-0000-0000-000000000000"


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


def _flags(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    return dict(raw or {})


class WrongKey(RuntimeError):
    """Sealed contacts in a row could not be opened: the master key is wrong."""


async def _batch(tenant_id: str, after: str, size: int, state: dict) -> tuple[Optional[str], int, int]:
    """(last id or None when done, contacts seen, ledger rows added). A sealed
    contact that cannot be opened still has its linked clients' details
    written; its id is recorded in state["unreadable"]."""
    from contact_truth import ContactTruthConfigError

    added = 0
    async with tenant_tx(_ctx(tenant_id)) as conn:
        rows = await conn.fetch(_FLAGGED, tenant_id, after, size)
        for row in rows:
            flags = _flags(row["suppression"])
            destinations = {(d[0], d[1]) for d in (row["client_details"] or [])}
            if row["pii_ciphertext"]:
                try:
                    async with conn.transaction():          # a savepoint: one bad record, not the batch
                        pii = await open_json(conn, tenant_id, row["pii_ciphertext"])
                    destinations.add((pii.get("phone"), pii.get("email")))
                    state["unreadable_in_a_row"] = 0
                except ContactTruthConfigError:
                    state["unreadable"].append(str(row["id"]))
                    state["unreadable_in_a_row"] += 1
                    if state["unreadable_in_a_row"] >= _UNREADABLE_IN_A_ROW:
                        raise WrongKey(f"{_UNREADABLE_IN_A_ROW} sealed contacts in a row could not be opened: "
                                       "check ORACLE_ENCRYPTION_MASTER_KEY") from None
            for phone, email in destinations:
                added += await outreach_compliance.record_crm_suppression(
                    conn, tenant_id, phone=phone, email=email, flags=flags,
                    source="CRM flag backfill (26.1)")
    return (str(rows[-1]["id"]) if rows else None), len(rows), added


async def backfill(*, batch_size: int, tenant_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """Every tenant with flagged contacts, or only `tenant_ids` (a staged run)."""
    started = time.monotonic()
    totals: dict[str, Any] = {"contacts": 0, "rows_added": 0}
    state: dict[str, Any] = {"unreadable": [], "unreadable_in_a_row": 0}
    if tenant_ids is None:
        async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
            tenant_ids = [str(r["t"]) for r in await conn.fetch(_TENANTS)]
    for tenant_id in tenant_ids:
        after: Optional[str] = _FIRST_ID
        while after is not None:
            last, seen, added = await _batch(tenant_id, after, batch_size, state)
            totals["contacts"] += seen
            totals["rows_added"] += added
            if seen:
                print(f"tenant {tenant_id}: {seen} flagged contact(s), {added} ledger row(s) added", flush=True)
            after = last if seen == batch_size else None
    if state["unreadable"]:
        print(f"{len(state['unreadable'])} sealed contact(s) could not be opened (their clients' details were "
              f"written): {', '.join(state['unreadable'])}", flush=True)
    return {**totals, "unreadable": len(state["unreadable"]), "seconds": round(time.monotonic() - started, 2)}


async def flagged_count() -> int:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return int(await conn.fetchval(_COUNT))


def batch_size_from_env() -> int:
    return max(1, min(2_000, int(os.getenv("ORACLE_CRM_SUPPRESSION_BATCH", "200"))))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--count", action="store_true", help="print flagged contacts; change nothing")
    parser.add_argument("--tenant", action="append", help="only this brokerage id (repeatable)")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    await init_pool(min_size=1, max_size=2)
    try:
        print(f"{await flagged_count()} flagged contact(s)", flush=True)
        if args.count:
            return 0
        try:
            out = await backfill(batch_size=batch_size_from_env(), tenant_ids=args.tenant)
        except WrongKey as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(out, flush=True)
    finally:
        await close_pool()
    return 1 if out["unreadable"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
