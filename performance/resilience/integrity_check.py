"""Post-drill data-integrity verification (local perf topology only).

After the failure drills, prove that nothing was left half-done or leaked:
  - tenant isolation still holds on the request pool (RLS), with and
    without a tenant context;
  - no orphaned job leases, stuck command executions or stuck chat turns;
  - no email marked sent twice, no outbox row lost between states;
  - the audit hash chain verifies end to end;
  - the MLS drill left no rows behind.
One JSON object on stdout; exit 1 if any invariant fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")


async def main() -> int:
    from audit_ledger import ledger
    from db import connection
    from db.connection import get_pool, tenant_tx
    from tenancy import Role, TenantContext

    platform = TenantContext(agent_id="integrity-check", tenant_id="00000000-0000-0000-0000-000000000000",
                             role=Role.PLATFORM_ADMIN)
    await connection.init_pool(min_size=1, max_size=3)
    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, **detail) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    try:
        async with tenant_tx(platform) as conn:
            tenants = [r["tenant_id"] for r in await conn.fetch(
                "SELECT tenant_id, count(*) FROM clients GROUP BY tenant_id ORDER BY count(*) DESC LIMIT 2")]
            orphan_leases = await conn.fetchval(
                "SELECT count(*) FROM automation_jobs WHERE state IN ('leased','running') "
                "AND lease_expires_at < now() - interval '5 minutes'")
            stuck_cmds = await conn.fetchval(
                "SELECT count(*) FROM command_executions WHERE state='executing' "
                "AND updated_at < now() - interval '15 minutes'")
            stuck_turns = await conn.fetchval(
                "SELECT count(*) FROM ai_chat_messages WHERE role='assistant' AND status IN ('pending','streaming') "
                "AND created_at < now() - interval '15 minutes'")
            outbox = {r["status"]: r["n"] for r in await conn.fetch(
                "SELECT status, count(*) AS n FROM email_outbox GROUP BY status")}
            sent_without_id = await conn.fetchval(
                "SELECT count(*) FROM email_outbox WHERE status='sent' AND provider_message_id IS NULL")
            dup_ids = await conn.fetchval(
                "SELECT count(*) FROM (SELECT provider_message_id FROM email_outbox WHERE provider_message_id IS NOT NULL "
                "GROUP BY provider_message_id HAVING count(*) > 1) d")
            drill_rows = await conn.fetchval("SELECT count(*) FROM oracle_mls_listings WHERE mls_id='drillmock'")
            open_alerts = [dict(r) for r in await conn.fetch(
                "SELECT component, state FROM ops_alerts WHERE resolved_at IS NULL")]
        check("no_orphaned_job_leases", orphan_leases == 0, count=orphan_leases)
        check("no_stuck_command_executions", stuck_cmds == 0, count=stuck_cmds)
        check("no_stuck_chat_turns", stuck_turns == 0, count=stuck_turns)
        check("email_outbox_consistent", sent_without_id == 0 and dup_ids == 0,
              by_status=outbox, sent_without_provider_id=sent_without_id, duplicate_provider_ids=dup_ids)
        check("mls_drill_cleaned_up", drill_rows == 0, rows=drill_rows)
        checks["open_alerts"] = {"ok": True, "alerts": open_alerts}

        # RLS on the request pool: a tenant sees only itself; no context sees nothing.
        if len(tenants) >= 2:
            a, b = str(tenants[0]), str(tenants[1])
            agent = TenantContext(agent_id="integrity-check", tenant_id=a, role=Role.AGENT)
            async with tenant_tx(agent) as conn:
                foreign = await conn.fetchval("SELECT count(*) FROM clients WHERE tenant_id <> $1::uuid", a)
                own = await conn.fetchval("SELECT count(*) FROM clients")
            check("rls_tenant_isolation", foreign == 0, tenant=a, own_rows=own, foreign_rows=foreign, other_tenant=b)
        else:
            check("rls_tenant_isolation", False, reason="fewer than two tenants with clients")
        async with get_pool().acquire() as raw:
            no_ctx = await raw.fetchval("SELECT count(*) FROM clients")
        check("rls_no_context_sees_nothing", no_ctx == 0, rows=no_ctx)

        check("audit_chain_verifies", await ledger.verify_chain())
    finally:
        await connection.close_pool()
    ok = all(c["ok"] for c in checks.values())
    print(json.dumps({"ok": ok, "checks": checks}, default=str, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    if os.getenv("NEOH_LOAD_TEST_ALLOWED") != "1":
        sys.exit("refusing: local perf topology only")
    sys.exit(asyncio.run(main()))
