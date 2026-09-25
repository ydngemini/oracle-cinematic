# Neoh — Disaster Recovery Drill Report

**2026-09-24.** Recovery was performed, not reviewed.

| | |
|---|---|
| HEAD at drill time | `738067d` |
| Migration head | `0111_mls_drop_unfed_columns.sql` (111 recorded) |
| Environment | disposable containers, created and destroyed by the drill |
| Customer data used | none — synthetic fixture only |
| Providers contacted | none |
| **Result** | **PASS** — 43/43 database drill, 8/8 Valkey drill, 19/19 against the real database |

Reproduce: `scripts/dr-drill.sh` and `scripts/dr-drill-redis.sh`.

---

## Measurements

| Metric | Value | Notes |
|---|---|---|
| Backup (fixture, 852 KB) | **~1 s** | |
| Backup (real DB, `authoritative` scope) | **152 s** → 24.7 MB | from a 47 GB source |
| Restore into clean cluster (fixture) | **5 s** | |
| Restore into clean cluster (real backup) | **13 s** | includes roles + authoritative-row extract |
| **Measured RTO, class A** | **~3 minutes** | backup + restore + verify, end to end |
| Measured data loss in test | **0 rows** | against the scope's stated contract |
| Logical-backup RPO | **= backup interval** | nothing schedules it yet — see blockers |
| DigitalOcean PITR (separate) | **~5 min worst case, 7-day window** | provider capability, verified from DO docs |

**Not measured: a full 47 GB restore.** The host disk is at 100%. Class-C RTO
(re-harvesting `public_property_records` and the bulk leads) remains an
estimate and is labelled as such rather than guessed.

## Results by area

| Area | Result |
|---|---|
| Backup tooling | PASS — manifest, checksum validated, git SHA, migration head, non-zero dump, no secret in manifest or log |
| Restore tooling | PASS — checksum verified before touching the target, refuses a populated database, zero `pg_restore` errors |
| Migration validation | PASS — head and count match the manifest exactly |
| RLS | PASS — 134 tables, FORCE on all 134, 146 policies, all three policy functions present |
| Tenant isolation | PASS — as `oracle_app_login`: A sees 3, B sees 2, neither sees 5 |
| Object restore | PASS — recovered by SHA-256, not merely by row count |
| Referential integrity | PASS — zero orphaned child rows |
| Valkey loss | PASS — Plivo fails closed, rate limiting degrades (does not open), DI cache falls back to PostgreSQL |
| Recovery mode | PASS — 25 egress methods, SMTP, both Stripe mutations and the scheduler, enforced by AST test |
| Encryption | PASS — round trip demonstrated; a wrong key returns NULL, not an error |
| Application access | PASS *after a fix* — see below |
| Provider reconciliation | DOCUMENTED, not drilled — no provider may be contacted from a drill |

## Failures discovered, and what was done

### 1. The restored database was unusable by the application — FIXED

`pg_dump` does not dump roles; they are cluster-level. The first restore had
every row, all 146 policies, FORCE RLS on 134 tables, and **none of the three
roles that 566 table grants point at**. Every check that existed passed. The
application could not open a connection.

Fixed three ways: roles are captured (without passwords), neither script
discards privileges any more, and the restore creates roles before anything
else. Re-verified against the real database — 4 roles, 566 grants, 19/19.

### 2. The fix had been applied to one side only — FIXED

After correcting `pg_dump`, the dump carried 350 ACL entries and `pg_restore`
was still being passed `--no-privileges`, so it threw all of them away. The
drill caught the asymmetry; a review of either file alone would not have.

### 3. The drill could not be run repeatedly — FIXED

`docker rm -f` strands a container's anonymous volume, and the postgres image
declares its data directory as one: ~300 MB per run. Twenty runs took the host
from 2.2 GB free to **35 MB**, at which point no container could start and
every assertion returned empty. Every removal now passes `-v`.

### 4. Two drill-harness bugs that produced false results — FIXED

`grep -c` prints `0` *and* exits 1, so `|| echo 0` yields `"0\n0"` and the
drill reported a failure whose entire message was `0`. And testing isolation
with an invented `drill_app` role read as a cross-tenant leak, when the real
cause was that 0003 revokes function EXECUTE from PUBLIC so the policy could
not evaluate — it now uses `oracle_app_login`, which is the honest test anyway.

## Blockers before production

1. **Nothing schedules the backup.** RPO is currently "whenever someone runs
   it." This is the single largest gap.
2. **Backups are written locally only.** No off-host upload to a private
   Spaces bucket; a host failure loses host and backup together.
3. **`ORACLE_ENCRYPTION_MASTER_KEY` is not escrowed.** A restore without it
   succeeds and returns blank data for 26 columns across 18 tables.
4. **Spaces versioning is off** (API-only, regional endpoint). Object recovery
   is proven for database-tracked metadata, not for the objects themselves.
5. **No full-scale restore has been timed.**
6. Provider reconciliation is documented, never exercised.

## Recommended drill frequency

| Trigger | Drill |
|---|---|
| Every merge to main | `scripts/dr-drill.sh` in CI — it takes 25 s |
| Monthly | Full-scale restore against a copy of production volume |
| Quarterly | DigitalOcean PITR, operator-run, into a new cluster |
| After any schema change touching roles, grants or RLS | Both drills |

The 25-second drill is cheap enough that there is no reason for it not to run
on every change. Every defect above was found by running it once.

---

See `docs/disaster-recovery-state-map.md` for the state classification and
RPO/RTO targets this report measures against.
