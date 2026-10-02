#!/usr/bin/env python3
"""Find stored objects no database row accounts for. Dry run only — deletes nothing.

Until 2026-10-01 nothing ever deleted an object: deleting a photo row left the
photo, and reconstruction staged copies of property photos under
recon-inputs/<uuid>/ that no row records at all. This lists, per prefix, how
many objects exist, how many a row references, and how many are orphaned —
with the tenant each orphan belongs to when the key carries one, and whether
that tenant is erased (an erased tenant's objects are always deletable).

    python scripts/privacy-orphan-audit.py            # summary
    python scripts/privacy-orphan-audit.py --keys     # also print orphan keys

Deletion of anything it finds is an operator decision, made per prefix,
recorded in the privacy runbook (docs/privacy-request-runbook.md §Orphans).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

PREFIXES = ("property-media/", "property-view/", "video-studio/", "splats/", "tenants/",
            "messaging-hosted-documents/", "recon-inputs/", "recon-outputs/", "privacy/exports/")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def main(show_keys: bool) -> int:
    import object_storage
    from db import connection
    from db.connection import tenant_tx
    from privacy_lifecycle import _platform_ctx

    if not object_storage.is_configured():
        print("object storage is not configured", file=sys.stderr)
        return 2
    await connection.init_pool(min_size=1, max_size=2)
    try:
        async with tenant_tx(_platform_ctx("orphan-audit")) as conn:
            referenced = set()
            for sql in (
                "SELECT s3_key FROM property_media WHERE s3_key IS NOT NULL",
                "SELECT storage_key FROM messaging_hosted_documents WHERE storage_key IS NOT NULL",
                "SELECT diagnostics->'storage'->>'storage_key' FROM reconstruction_jobs "
                "WHERE diagnostics->'storage'->>'storage_key' IS NOT NULL",
                "SELECT s3_key FROM contract_synthesis_artifacts WHERE s3_key IS NOT NULL",
                "SELECT artifact_key FROM privacy_operations WHERE artifact_key IS NOT NULL",
            ):
                referenced |= {r[0] for r in await conn.fetch(sql)}
            erased = {str(r[0]) for r in await conn.fetch(
                "SELECT id FROM tenants WHERE lifecycle_state='erased'")}
            known = {str(r[0]) for r in await conn.fetch("SELECT id FROM tenants")}
    finally:
        await connection.close_pool()

    total_orphans = 0
    for prefix in PREFIXES:
        try:
            keys = await asyncio.to_thread(object_storage.list_prefix, prefix)
        except Exception as exc:  # noqa: BLE001
            print(f"{prefix:30} unlistable ({type(exc).__name__})")
            continue
        # A splat's companions (manifest, poses) share the referenced key's stem.
        orphans = [k for k in keys if k not in referenced
                   and not any(k.startswith(r.rsplit(".", 1)[0]) for r in referenced if r.startswith(prefix))]
        owners = Counter()
        for key in orphans:
            match = _UUID.search(key)
            tenant = match.group(0) if match and match.group(0) in known else None
            owners["erased tenant" if tenant in erased else ("live tenant" if tenant else "no tenant in key")] += 1
        total_orphans += len(orphans)
        print(f"{prefix:30} objects={len(keys):6} orphaned={len(orphans):6} {dict(owners)}")
        if show_keys:
            for key in orphans[:500]:
                print(f"    {key}")
    print(f"\n{total_orphans} orphaned object(s). Nothing was deleted.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keys", action="store_true", help="print orphan keys (first 500 per prefix)")
    sys.exit(asyncio.run(main(parser.parse_args().keys)))
