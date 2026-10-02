# Runbook: database down, slow or failing over

**You see:**
- alert `[Neoh] database UNAVAILABLE`, or the `database` component DEGRADED (pool saturated);
- `/health` returning 503;
- users seeing "Neoh is temporarily unavailable. Your work is saved."

**What Neoh does by itself:**
- Replicas go unready (`/health`) but stay alive (`/live`), so App Platform does not restart them into a boot that would fail.
- Every request answers 503 within 10 s (`ORACLE_DB_ACQUIRE_TIMEOUT`), with `Retry-After`. Nothing hangs, and nothing reports success.
- Job workers back off exponentially with jitter.
- The realtime listener reconnects by itself when the database returns.

**Do:**
1. Check the DigitalOcean database cluster's status, and the connection count against its limit (`docs/database-connection-budget.md`).
2. If it's slow rather than down, find the culprit with `SELECT … FROM pg_stat_statements ORDER BY total_exec_time DESC`, and `pg_cancel_backend` it.
3. If the cluster is lost, follow `docs/disaster-recovery-state-map.md`. **After any restore, run `python scripts/reapply-erasures.py --apply` before taking traffic.**
4. After recovery:
   - check `GET /api/admin/health/components`;
   - check open alerts resolve within about 2 minutes;
   - look for `reconciliation_required` commands from the outage window (reconciliation runs every 15 min).

**Do not** restart replicas to "fix" a database outage. They recover by themselves, and restarting adds a crash loop.
