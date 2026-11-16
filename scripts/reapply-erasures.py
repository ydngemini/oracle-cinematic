#!/usr/bin/env python3
"""Re-apply completed erasures after a database restore.

A managed-database backup is a point-in-time copy. Restoring one taken before
a brokerage was erased brings that brokerage's data back — silently, unless
something remembers the erasure outside the database. privacy_lifecycle
writes a directive (tenant id, operation id, time; no customer data) to
object storage under privacy/erasure-directives/ when an erasure completes,
and privacy_requests writes one per person deleted by request (row ids and
keyed hashes) under privacy/subject-erasure-directives/ before deleting.
This script reads those directives and erases again whatever the restore
resurrected. Run it after every restore, BEFORE the service takes traffic
(docs/disaster-recovery.md, docs/privacy-request-runbook.md §Restore).

    python scripts/reapply-erasures.py            # dry run: what would be erased
    python scripts/reapply-erasures.py --apply    # erase it

Needs the backend's normal environment (ORACLE_DB_*, ORACLE_DB_PLATFORM_PASSWORD,
storage settings, ORACLE_ENCRYPTION_MASTER_KEY).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))


async def main(apply: bool) -> int:
    from db import connection
    from privacy_lifecycle import reapply_erasures
    from privacy_requests import reapply_subject_erasures

    await connection.init_pool(min_size=1, max_size=2)
    try:
        # Brokerages first, then people deleted by request (audit 29.4).
        report = await reapply_erasures(apply=apply)
        try:
            report += await reapply_subject_erasures(apply=apply)
        except Exception as exc:  # noqa: BLE001 - still print the brokerage pass
            report.append({"kind": "subject", "status": "reapply_failed", "detail": type(exc).__name__})
    finally:
        await connection.close_pool()
    for entry in report:
        print(json.dumps(entry, default=str))
    todo = [e for e in report if e["status"] == "resurrected"]
    bad = [e for e in report if e["status"] in ("reapply_failed", "skipped_legal_hold", "unreadable",
                                                 "needs_review", "tenant_erased_rows_back",
                                                 "expired_rows_back")]
    if not apply:
        print(f"\n{len(todo)} erasure(s) to re-apply. Re-run with --apply.")
        return 1 if todo else 0
    print(f"\n{sum(e['status'] == 'reapplied' for e in report)} re-applied, {len(bad)} need attention.")
    return 1 if bad else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="erase (default: dry run)")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
