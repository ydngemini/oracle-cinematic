#!/usr/bin/env python3
"""Put the demo brokerage back exactly as the seed leaves it.

    # dry run (default): counts what would be removed, touches nothing
    python scripts/reset-demo-tenant.py --tenant-id <uuid> --base-url https://…
    # do it
    python scripts/reset-demo-tenant.py --tenant-id <uuid> --base-url https://… --execute

What it restores: Sarah and every other demo contact, the properties, the
timeline (activities, notes, showings), Neoh's conversation and action state
(proposals, approvals, command executions, receipts), messages and call
records, and the demo billing/integration rows — by deleting the demo
tenant's mutable CRM rows and re-running scripts/seed-demo-tenant.py, which
recreates them through the product's own API.

What it refuses, before writing anything:
  * ORACLE_ENV=prod|production, or a production hostname;
  * any tenant that is not ALL of: the --tenant-id given, tenants.is_demo,
    and the fixed demo slug (demo_tenant_common.assert_demo_tenant);
  * a demo tenant with a provider call/text still executing (it would be
    orphaned mid-flight).

What it never touches: other tenants (every DELETE is `WHERE tenant_id = $1`
for the verified id, in one transaction), the users and their logins, the
audit ledger (its hash chain is evidence and must stay intact), privacy and
legal-hold records, the outreach attempt log of real sends, and staging
infrastructure (no app, database or bucket settings change).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_tenant_common as common  # noqa: E402

#: Tenant rows a reset keeps. Identity and access, evidence, and the demo's
#: billing/integration configuration (which the seed re-asserts).
KEEP = frozenset({
    "users", "user_profiles", "team_memberships", "user_policy_acceptances",
    "account_security_acceptances", "brokerage_invitations", "brokerage_setup_progress",
    "password_reset_tokens", "oauth_authorization_states",
    "audit_ledger", "audit_anomaly_alerts", "erasure_ledger", "privacy_operations",
    "legal_holds", "outreach_attempt_log", "suppression_tombstones",
    "subscriptions", "telephony_routes", "messaging_routes", "provider_credentials",
    "tenant_messaging_brands", "tenant_messaging_campaigns", "messaging_hosted_documents",
    "provider_purchases", "mls_feed_entitlements", "source_licenses",
    "lead_pipeline_counts", "tenant_action_budgets", "autonomy_preferences",
    "agent_ai_settings", "agent_licenses", "agent_ce_log",
    "model_registry", "model_training_runs", "model_evaluations",
    "tenant_contract_template_registrations", "contract_templates",
})

#: Refuse while any of these is in flight: a provider may be mid-request.
IN_FLIGHT_STATES = ("executing",)

#: Why a table could not be cleared, for the refusal message.
LAST_ERRORS: dict[str, str] = {}


async def tenant_tables(conn) -> dict[str, str]:
    """{table: tenant_id data type} for every tenant-scoped table not kept."""
    rows = await conn.fetch(
        """
        SELECT c.table_name, c.data_type FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
           AND t.table_type = 'BASE TABLE'
         ORDER BY 1
        """
    )
    return {r["table_name"]: r["data_type"] for r in rows if r["table_name"] not in KEEP}


def tenant_predicate(data_type: str) -> str:
    """Most tables key tenant_id as uuid; a few older ones as text."""
    return "tenant_id = $1::uuid" if data_type == "uuid" else "tenant_id::text = $1"


async def count_rows(conn, tables: dict[str, str], tenant_id: str) -> dict[str, int]:
    out = {}
    for table, dtype in tables.items():
        n = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE {tenant_predicate(dtype)}', tenant_id)
        if n:
            out[table] = int(n)
    return out


async def purge(conn, tables: dict[str, str], tenant_id: str) -> tuple[dict[str, int], list[str]]:
    """Delete in FK-safe order by repeated passes, each table in a savepoint.

    A table whose rows are still referenced fails its savepoint and is retried
    on the next pass, after its referrers are gone. Whatever still fails after
    the passes is reported, not forced.
    """
    deleted: dict[str, int] = {}
    pending = list(tables)
    for _ in range(12):
        failed = []
        for table in pending:
            try:
                async with conn.transaction():
                    status = await conn.execute(
                        f'DELETE FROM "{table}" WHERE {tenant_predicate(tables[table])}',
                        tenant_id)
                n = int(status.split()[-1])
                if n:
                    deleted[table] = deleted.get(table, 0) + n
            except Exception as exc:  # noqa: BLE001 — retried next pass
                failed.append(table)
                LAST_ERRORS[table] = str(exc).splitlines()[0][:200]
        if not failed or failed == pending:
            pending = failed
            break
        pending = failed
    return deleted, pending


def _load_seed():
    path = Path(__file__).resolve().parent / "seed-demo-tenant.py"
    spec = importlib.util.spec_from_file_location("seed_demo_tenant", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def reset(tenant_id: str, base_url: str, execute: bool) -> dict:
    common.refuse_production(base_url)
    conn = await common.connect_admin()
    try:
        row = await conn.fetchrow(
            "SELECT id::text AS id, slug, is_demo FROM tenants WHERE id = $1::uuid", tenant_id)
        common.assert_demo_tenant(dict(row) if row else None, tenant_id)
        busy = await conn.fetchval(
            "SELECT count(*) FROM command_executions WHERE tenant_id = $1::uuid AND state = ANY($2::text[])",
            tenant_id, list(IN_FLIGHT_STATES))
        if busy:
            raise common.DemoSafetyError(
                f"{busy} provider action(s) are executing right now; wait for them to settle")
        tables = await tenant_tables(conn)
        before = await count_rows(conn, tables, tenant_id)
        print(f"demo tenant {tenant_id} ({row['slug']}): {sum(before.values())} mutable rows "
              f"in {len(before)} tables")
        for table, n in sorted(before.items()):
            print(f"  {'delete' if execute else '[dry-run] would delete'} {n:>5}  {table}")
        if not execute:
            return {"dry_run": True, "would_delete": before}

        keys = [r["s3_key"] for r in await conn.fetch(
            "SELECT s3_key FROM property_media WHERE tenant_id = $1::uuid AND s3_key IS NOT NULL",
            tenant_id)]
        async with conn.transaction():
            # Re-check inside the transaction: the guard and the deletes see
            # the same snapshot of the tenant row.
            locked = await conn.fetchrow(
                "SELECT id::text AS id, slug, is_demo FROM tenants WHERE id = $1::uuid FOR UPDATE",
                tenant_id)
            common.assert_demo_tenant(dict(locked) if locked else None, tenant_id)
            # clients ⇄ agent_contacts reference each other (0054, both
            # RESTRICT), so neither can go first; unlink the pair, then purge.
            await conn.execute(
                "UPDATE clients SET contact_id = NULL WHERE tenant_id = $1::uuid", tenant_id)
            await conn.execute(
                "UPDATE agent_contacts SET legacy_client_id = NULL WHERE tenant_id = $1::uuid",
                tenant_id)
            deleted, stuck = await purge(conn, tables, tenant_id)
            if stuck:
                raise common.DemoSafetyError(
                    f"could not clear {stuck} ({ {t: LAST_ERRORS.get(t) for t in stuck} }); "
                    "nothing was changed (transaction rolled back)")
    finally:
        await conn.close()

    # Stored media of the deleted rows (the seed writes a fresh space).
    removed_objects = 0
    if keys:
        common.backend_on_path()
        import object_storage

        for key in keys:
            if not key.startswith((f"splats/{tenant_id}/", f"property-media/{tenant_id}/")):
                continue  # never outside this tenant's own prefixes
            for suffix in ("", ".json", ".scene.json", ".cameras.json", ".points.ply"):
                try:
                    removed_objects += bool(object_storage.delete_object(
                        key if not suffix else key.rsplit(".", 1)[0] + suffix))
                except Exception:  # noqa: BLE001 — an orphaned object is not a failed reset
                    pass

    seed = _load_seed()
    state = await seed.seed(base_url, True)
    print(f"reset complete: {sum(deleted.values())} rows removed, {removed_objects} stored "
          f"objects removed, demo data re-seeded")
    return {"deleted": deleted, "objects_removed": removed_objects, "state": state}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant-id", required=True, help="the demo tenant's id (required)")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(reset(args.tenant_id, args.base_url, args.execute))
    except common.DemoSafetyError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
