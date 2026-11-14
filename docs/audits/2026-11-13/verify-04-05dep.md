# Re-verification: external audit chunks 04 and 05 (database, jobs, recovery, deployment)

- **Checkout:** `/media/ydn/SYPHER_CORE2/Oracle`, branch `main`, `git rev-parse --short HEAD` = **`54740ef`**, working tree clean. This matches the SHA the audits were written against.
- **Inputs (untrusted claims):** `CHUNK-04-database-jobs-recovery.md` (DR-001, JOB-001, MIG-001, OPS-001), `CHUNK-04-database-jobs-recovery(1).md` (REC-01..04, OPS-01, JOB-01), `CHUNK-05-deployment-production-reliability.md` (DEP-01..09).
- **Method:** grep plus line-range reads of every cited file at `54740ef`. Read-only `gh run list` and `gh run view` for DEP-01 and DEP-08. Nothing was executed against a database or a cloud, and no file was edited outside this scratchpad.
- **Duplicates merged:**
  - DR-001 = REC-01 + REC-02. REC-02 is the "even with a key" half of DR-001.
  - OPS-001 = OPS-01.
  - JOB-001 and JOB-01 are **different** defects; both are kept.
- **New finding:** one, from my own reading while verifying DEP-02/03. It is listed as **DEP-03b**.

Category ownership comes from `docs/claude-prompts/README.md:40` (08 durable missions and workflows), `:49` (17 security and incident readiness) and `:51` (19 production operations: backups, rollback, realistic load).

---

## Summary table

| id | verdict | file:line (at 54740ef) | category | already tracked? | P | one-line fix |
|---|---|---|---|---|---|---|
| DR-001 / REC-01 | CONFIRMED | scripts/verify-restore.sh:178-189, 261-264 | 19 (DR drill history: 17, L17-18) | **No.** Docs claim the opposite (disaster-recovery-state-map.md:34) | P1 (P0 gate before prod) | Missing key → `bad`, so the script exits 1 |
| REC-02 (part of DR-001) | CONFIRMED, stronger than claimed | verify-restore.sh:179-180; 0006_pii_encryption.sql:86-115; scripts/dr-drill.sh:272 | 19 / 17 | No | P1 | Decrypt sampled restored ciphertext under the real per-tenant derived key |
| REC-03 | CONFIRMED | scripts/restore-postgres.sh:233-243, 272; verify-restore.sh:107-114, 252-258 | 19 | No | P1 | Fail on unlisted `pg_restore` errors; keep the log; verify every authoritative table's count |
| REC-04 | PARTIAL | verify-restore.sh:133-141; dr-drill.sh:313-345 (drill probes `clients` only) | 19 / 17 | No | P1 | Record the exact RLS/FORCE/policy inventory in the manifest and compare for equality |
| OPS-001 / OPS-01 | ENVIRONMENT | docs/dr-drill-report.md:86-97; docs/disaster-recovery-state-map.md:339-347; no backup schedule in infra/ or .github/ | 19 (escrow: 17) | **Yes.** LEDGER row 17 (line 52), L17-18 next_task, A04, AC19.05/AC19.10, L17-06 risk register | P1 (P0 before prod) | Owner: schedule encrypted off-host backups, escrow the key, run a timed restore |
| JOB-001 | PARTIAL (latent) | backend/automation_jobs.py:192-227 | 08 | No | P2 | On conflict, compare job_type + payload hash; raise IdempotencyConflict when they differ |
| JOB-01 | CONFIRMED | automation_jobs.py:436-462, 464-509, 512-535 | 08 | No | P2 | Finish the attempt in the same `tenant_tx` as the terminal update, plus an orphan-attempt sweep |
| MIG-001 | PARTIAL | backend/run_migrations.py:825-846, 848-880, 917; prod path guarded at :1010-1015, :958-962 | 19 | No | P2 | Pre-scan every SHA and return 4 before any DDL in direct mode |
| DEP-01 | CONFIRMED / ENVIRONMENT | run 43113413233, step `doctl auth`: "Unable to use supplied token … status 401"; .github/workflows/ci.yml:352-355 | 19 | Partly: only as a note in L18-05 (LEDGER:3237) and in memory; no cat-19 ledger row | P0 (pipeline) | Owner mints a new DO token; rerun; require the staging-verified artifact |
| DEP-02 | CONFIRMED | scripts/rollback.sh:113-119, 128-130 | 19 | Generic only (T19.11, AC19.05) | P1 | `--apply` requires `--from` and checks it against live `/version` |
| DEP-03 | CONFIRMED | rollback.sh:228-250 (contrast ci.yml:773-782); carry-secrets.py has no name check | 19 | Generic only (AC19.04) | P1 | `doctl apps get` and require `spec.name == neoh` before carry and update |
| DEP-03b (new) | CONFIRMED (new) | rollback.sh:201-205 renders raw app.yaml with sed; render-app-spec.py:200-210 | 19 | No | P1 (once prod has a domain) | Render through `render-app-spec.py --env production --domain`, then spec-drift and validate |
| DEP-04 | CONFIRMED | ci.yml:863-867 (prod), 591-595 (staging) | 19 | No | P1 | `spec get` failure after a successful `apps get` → exit 1 |
| DEP-05 | CONFIRMED | backend/migration_safety.py:112-125, 84-85, 238-239 | 19 | Generic (T19.06/F19.03) | P2 | Move ADD CONSTRAINT / UNIQUE INDEX / POLICY / TRIGGER / DROP DEFAULT to UNKNOWN |
| DEP-06 | CONFIRMED | rollback.sh:253-256 | 19 | Generic (T19.11) | P2 | Run smoke-test.sh after the update; exit 4 `ROLLED_BACK_BUT_UNVERIFIED` |
| DEP-07 | DESIGN-GAP (documented) | infra/digitalocean/app.yaml:20-28, 368, 406-407; docs/capacity-plan.md:83-89 | 19 (+16) | **Yes.** A10, T19.09, T19.10 | P2 | Measure 20 agents on DO first; the "100 agents" premise is not the owner's target |
| DEP-08 | CONFIRMED, observed | infra/digitalocean/smoke-test.sh:167-186; ci.yml:609-615, 884-888 (no Spaces creds) | 19 | No | P2 | Give smoke Spaces read creds, or move the bucket check to an in-app readiness probe |
| DEP-09 | CONFIRMED (evidence gap) / ENVIRONMENT | docs/launch-state.md:16; docs/launch-readiness/README.md:5; docs/resilience-drill-report.md:3-9 | 19 | **Yes.** A04, AC19.01, AC19.11 | P1 | Fresh dated `neoh-launch-readiness.py --env production` once prod exists |

---

## Chunk 04: database, jobs, recovery

### DR-001 / REC-01: the verifier passes with no master key. CONFIRMED

**Evidence**
- `scripts/verify-restore.sh:178` gates the round trip on `[ -n "${ORACLE_ENCRYPTION_MASTER_KEY:-}" ]`.
- The `else` branch (`:184-189`) only `echo`s a `WARN`. It never calls `bad()`, which is defined at `:48` and is what increments `FAIL`.
- `:261-264`: when `FAIL -eq 0` the script prints `RESTORE VERIFIED` and runs `exit 0`.
- The manifest carries `"requires_master_key": true` (`scripts/backup-postgres.sh:446`), but the verifier never reads that key.
- So a keyless restore whose other checks pass exits 0 and prints "RESTORE VERIFIED". The script's own header (`:13-17`) calls that "the quietest failure in the whole system".

**The docs, memory and vault say the opposite**
- `docs/disaster-recovery-state-map.md:34`: "`scripts/verify-restore.sh` refuses to claim verification without that."
- User memory `oracle-disaster-recovery.md:25`: "verify-restore.sh won't claim verification without a…"
- Vault `SYPHER_VAULT/10_Active_Builds/Neoh_Disaster_Recovery.md:14`: "A restore is done when this passes."

The memory note is accurate about the **behaviour** (a keyless restore reads as blank). It is wrong about the **guard**.

**The existing test checks text, not behaviour.** `backend/tests/test_backup_restore_scripts.py:218-222` only asserts that the substrings `oracle_decrypt(oracle_encrypt(` and `decryptability is UNPROVEN` are present. `:225-227` asserts that the file ends in `exit 1`. Neither executes the script.

**Tracked?** No.
- The DR blockers that are tracked (dr-drill-report.md:93, "not escrowed") are about escrow, not about the verifier.
- L17-18 (LEDGER:2842-2856) records the drill as passing.

**Category:** 19 (AC19.05 "cold restore works"). The drill's history is in 17 (L17-18).

**Smallest fix:** in the `else` branch, replace the `echo`s with `bad "ORACLE_ENCRYPTION_MASTER_KEY not set — decryptability UNPROVEN; manifest requires_master_key=true"`.

**Failing-first test:** a new test in `test_backup_restore_scripts.py`, modelled on the fake-`doctl` harness in `test_release_immutability.py:398-450`.
1. Put a fake `psql` on `PATH` that answers each query by pattern: head and count equal to the manifest, 60 tables, RLS counts > 0, pgcrypto present, `UNGRANTED` 0, and so on.
2. Write a minimal flat manifest.
3. Run `bash scripts/verify-restore.sh manifest.json` with `ORACLE_ENCRYPTION_MASTER_KEY` unset.
4. Assert `returncode != 0` and `"RESTORE VERIFIED" not in stdout`.

The test fails today: it gets rc 0 and the marker.

**Severity:** P1 today, because no production exists (LEDGER:54, A04). It must be closed before any production restore drill (AC19.05). **Customer impact:** an operator could bring a restored service online that shows every lead payload, SMS, transcript and contract as empty, with a green verifier.

### REC-02: the round trip never touches the real key. CONFIRMED, and stronger than the audit says

**Evidence**
- `verify-restore.sh:179-180` runs `SELECT oracle_decrypt(oracle_encrypt('neoh-restore-probe', 'k'), 'k')`. The key is the literal `'k'`, and the env var's **value** is never used. Any non-empty value passes.
- `scripts/dr-drill.sh:272` passes `ORACLE_ENCRYPTION_MASTER_KEY="${…:-drill-probe-key}"`. So CI's drill always "proves" encryption with a dummy key.
- `scripts/dr-drill-fixture.sql` contains no ciphertext at all (grep for `encrypt|bytea|cipher` returns nothing). The CI drill therefore never restores a single sealed value.
- `oracle_decrypt` swallows every error and returns NULL (`0006_pii_encryption.sql:99-115`).
- The real key is **per tenant**: HKDF `crypto.derive_tenant_key(tenant_id, master)` (`backend/client_note_crypto.py:37-44`; LEDGER:2946). SQL alone cannot derive it, so a correct check needs a host-side derivation step.

**Tracked?** No.

**Category:** 19 / 17.

**Smallest fix**
1. Add a host-side probe (Python, run on the operator host, because the stock postgres image has no python3, per `verify-restore.sh:53-55`). For each table with a `*_ciphertext` column, it selects up to N non-null `(tenant_id, ciphertext)` rows. It derives the key with `crypto.derive_tenant_key` and runs `SELECT oracle_decrypt($1, $2) IS NOT NULL`.
2. Any NULL is a failure. Zero sampled rows when `ciphertext_columns > 0` and the source had sealed rows is reported as "unproven" (FAIL).
3. Have `verify-restore.sh` call it, and FAIL if the probe cannot run.
4. Seed one sealed row per tenant into `dr-drill-fixture.sql`, so the CI drill exercises the probe.

**Failing-first test**
- Fake-psql harness: the key is set to `wrong`, and the fake returns NULL for decrypts of the sampled row → expect rc 1.
- Live: `test_*_live.py` seals a row under key A, then runs the probe with key B → expect failure.

Both fail today: the probe does not exist, and the literal-`'k'` check passes.

**P1.** **Customer impact:** the same as DR-001, and it applies even when someone "has" a key that is the wrong one.

### REC-03: pg_restore errors are tolerated, and reconciliation is shallow. CONFIRMED

**Evidence**
- `scripts/restore-postgres.sh:233-243`: a non-zero `RC` prints the top 20 `pg_restore: error` lines, then "scripts/verify-restore.sh decides. Continuing."
- The verifier never receives `RC`.
- `:272` runs `rm -f "$DIR/.restore.log"`, so the full error log is **deleted** at the end. Only the first 20 lines survive, in scrollback.
- `verify-restore.sh:107-114` checks counts for only `tenants`, `users`, `clients` and `leads`, and `continue`s when the expected value is empty.
- `:252-258` passes on more than 50 tables. That is a table-count check, not a row check.
- The manifest records `authoritative_extract_rows` (`backup-postgres.sh:441`), but the verifier never compares it.
- A failed data load of, for example, `sms_messages`, `contracts` or `interaction_logs` passes every check.

**Mitigation that exists:**
- The scoped extract is applied with `ON_ERROR_STOP=1` and dies on failure (`restore-postgres.sh:210-214`, `:249-255`).
- The CI drill compares 12 seeded facts (`dr-drill.sh:291-305`).
- Both of those are drill-only or extract-only.

**Tracked?** No.

**Category:** 19.

**Smallest fix**
1. Pass `RC` and the error lines to the verifier: write `.restore.rc` plus the kept log, and stop deleting it.
2. FAIL on any error line that is not on an explicit allowlist. The allowlist starts with the known extension/comment-ownership errors that the comment at `:234-237` describes.
3. Have `backup-postgres.sh` record `expected_rows_<table>` for every authoritative table. The keys stay flat, per `:424`.
4. Have the verifier compare all of them.

**Failing-first test:** fake-psql harness where `sms_messages` returns 0 against a manifest expecting 5 → rc 1. It fails today, because that table is never checked.

**P1.** **Customer impact:** a partial restore could silently drop a whole table of customer conversations or contracts and still be declared verified.

### REC-04: the RLS check passes when protection is only partial. PARTIAL

**Confirmed:** `verify-restore.sh:133-141` passes if **any** table has `relrowsecurity`, **any** table has `relforcerowsecurity` and **any** policy exists. Losing RLS on 133 of 134 tables passes.

**Narrower than claimed:**
- `dr-drill.sh:313-345` does run an isolation probe as the real `oracle_app_login` and checks for cross-tenant leakage.
- But it is CI-drill only, and it covers the `clients` table only.
- The operator-facing verifier has no inventory comparison, and the manifest records no RLS inventory (`backup-postgres.sh:408-448`).

**Compounding:** together with REC-03, a `CREATE POLICY` that fails during `pg_restore` is tolerated, and then this coarse check passes.

**Tracked?** No.

**Category:** 19 (17 for the tenancy assertion).

**Smallest fix**
1. Have `backup-postgres.sh` record `rls_tables`, `forced_tables` and `policy_count`, using the same three queries the verifier uses (`verify-restore.sh:123-131`).
2. The verifier asserts **equality**, not `> 0`.
3. Optionally add a sha256 of the sorted `(tablename, policyname)` list.

**Failing-first test:** a manifest with `rls_tables: 134` and a fake psql returning 133 → rc 1. It fails today, because 133 > 0 passes.

**P1.** **Customer impact:** a restore could re-expose one tenant's records to another, the most severe consequence class in this repo, while the verifier reports green.

### OPS-001 / OPS-01: backup schedule, off-host copy, key escrow and timed restore. ENVIRONMENT

**Evidence**
- The repo's own blockers list (`docs/dr-drill-report.md:86-97`): nothing schedules a backup; backups are local only; the key is not escrowed; Spaces versioning is off; no full-scale timed restore; provider reconciliation has never been exercised.
- `docs/disaster-recovery-state-map.md:339-347` says the same under "Still manual".
- grep finds no `schedule:`/cron backup in `.github/workflows/ci.yml` or `infra/digitalocean/app.yaml`.
- CI runs only the disposable drill (`ci.yml:265-283`).
- The saved readiness runs are dated 2026-10-03 (`docs/launch-readiness/README.md:5`).

**Tracked?** Yes:
- LEDGER row 17, open items "scheduled off-host backups" (LEDGER:52).
- L17-18 next_task (LEDGER:2854).
- L17-06 risk register "unescrowed master key" (LEDGER:2571) and its owner list (LEDGER:2578).
- A04 (LEDGER:3528).
- AC19.05 and AC19.10 in the cat-19 prompt.

**Category:** 19; key escrow sits with 17.

**Fix:** owner/infra actions:
1. A scheduled, encrypted backup to a private Spaces bucket with a lifecycle policy.
2. Key escrow outside DO and the workstation.
3. Spaces versioning.
4. A timed full-size restore into a disposable cluster that runs the fixed verifier (after DR-001, REC-02, REC-03 and REC-04 land).

**Code-side test:** a "backup age" readiness check in `neoh-launch-readiness.py` that BLOCKs when no backup receipt is younger than the RPO.

**P1** now, and P0 before production. **Customer impact:** today the RPO is "whenever someone runs it". Losing the host loses both the data and the backups.

### JOB-001: a reused idempotency key returns the old job silently. PARTIAL (latent)

**Confirmed code path**
- `automation_jobs.py:192-218`: `INSERT … ON CONFLICT (tenant_id, idempotency_key) DO NOTHING`.
- `:219-224`: on conflict it re-reads by `(tenant_id, idempotency_key)` only and returns `created=False`.
- No `job_type`, payload or `risk_class` comparison exists.
- The approval check at `:170-190` validates the **new** payload's hash against the approval, then hands back the **old** job. A caller can therefore believe that an approved payload was queued when a different one runs.

**Why only partial:** every current caller builds its key server-side, namespaced by job type and a server-generated id:
- `sales_api.py:1232` / `:1282`: `smart-plan:{run_id}:{n}`
- `commands_api.py:2079`: `command:{id}:execute`
- `models_api.py:576`: `model-training:{run_id}`
- `harvests_api.py:196`: a server `uuid4`
- `spatial_intelligence_api.py:277`
- `client_ai_automation.py:569`: a nonce

The one client-influenced key, `ai_chat_api.py:345` `ai-chat:{agent}:{frame.request_id}`, is de-duplicated upstream (`:331-332`, `if created["duplicate"]: return`). No caller I found can currently collide a key with a different job type.

**Tracked?** No.

**Category:** 08.

**Smallest fix**
1. After the conflict re-read, compare `row["job_type"] == job_type` and `payload_hash(row["payload"]) == body_hash`. Also compare `risk_class` and `approval_id` when approval is required.
2. On a mismatch, raise `IdempotencyConflict`, and map it to 409 at the API edge.
3. Genuine retries (identical inputs) still return the existing job.

**Failing-first test:** in a live test (`test_*_live.py`, CI live list), enqueue `(key K, payload A)`, then `(key K, payload B)` → expect `IdempotencyConflict`. Today it returns `created=False` with payload A. Add a same-payload retry that must still return the original.

**P2.** **Customer impact:** none today. A future caller that reuses a key could have a different approved action silently replaced by an older one.

### JOB-01: the terminal job update and the attempt ledger are not atomic. CONFIRMED

**Evidence**
- `complete_job` (`automation_jobs.py:436-462`) and `fail_job` (`:464-501`) commit the job row through `_lease_update`. That function opens its own `tenant_tx` (`:352-360`).
- They then call `_finish_attempt` (`:503-535`), which opens a **second** `tenant_tx` (`:513-524`).
- If the second transaction fails, the job is terminal and its `automation_job_attempts` row stays open.

**Broader than the audit says:** attempts orphaned by a crashed worker (lease expiry reclaim at `:265-266`, dead-letter at `:315-339`) are never closed either. The only writers are the finish function (migrations 0047/0128) and the erasure delete (0122:674). No reconciler exists.

The audit is right that this does not cause re-execution. After the job commit, the lease is cleared, so a follow-up `fail_job` raises `JobLeaseLost`.

**Tracked?** No.

**Category:** 08.

**Smallest fix**
1. Do the attempt finish inside the same transaction: give `_lease_update` an optional `after(conn)` hook that runs `finish_automation_job_attempt` before commit.
2. Add a periodic sweep that closes attempts whose job is terminal, or whose lease was re-claimed, with outcome `abandoned`.

**Failing-first test:** with a fake `tenant_tx` connection whose `fetchval` raises for `finish_automation_job_attempt`, assert that the job update is **rolled back** (state is not `succeeded`). Today the first transaction has already committed.

**P2.** **Customer impact:** none directly. The audit and execution history becomes inconsistent, which weakens incident forensics.

### MIG-001: direct mode notices drift after applying newer migrations. PARTIAL

**Confirmed for direct mode**
- `run_migrations.py:825-846` appends to `drifted` and `continue`s.
- Later unrecorded files apply and commit (`:848-880`).
- The role passwords are reset (`:886-910`).
- Only then does `:917` `return 4 if drifted else 0`.

**Mitigated in production:**
- The PRE_DEPLOY job runs `python run_migrations.py --precheck-then-migrate` (`infra/digitalocean/app.yaml:565`).
- `main()` returns the precheck's code 3 before calling `_run_migrations` (`run_migrations.py:1010-1015`).
- `evaluate_precheck` refuses edited migrations (`:958-962`).

**Where direct mode is still used:**
- `scripts/dev-start.sh:115`
- `scripts/livetest-local.sh:18`
- `scripts/rehearse-migrations-nosuperuser.sh:43`
- any manual `python run_migrations.py`

**Minor:** the precheck runs before the advisory lock is taken (`_run_migrations:795`). That is a small check-then-act gap if two migrators ever start at once.

**Tracked?** No.

**Category:** 19 (T19.06).

**Smallest fix:** in `_run_migrations`, before the apply loop, compute every file's digest. If any recorded digest differs, print the drift and `return 4` without applying anything.

**Failing-first test:** extend `backend/tests/test_run_migrations.py:228-241`. Write `0001` (recorded with a wrong SHA) plus an unrecorded `0002`, then assert `code == 4` **and** that `0002` is absent from `_event_names(conn)`. This fails today, because 0002 is applied.

**P2.** **Customer impact:** none in the production path. Dev and live-test databases can be migrated further away from the repository before anyone is told.

---

## Chunk 05: deployment and production reliability

### DEP-01: main's release is blocked at `doctl auth`. CONFIRMED / ENVIRONMENT

**Evidence**
- `gh run view 43113413233` (main, 2026-11-11T22:38Z, the merge of PR #44 = `54740ef`):
  - The database-security, security-scans, backend and frontend jobs all passed.
  - `build release → staging` failed at `doctl auth`. The log says `Error: Unable to use supplied token to access API` / `status 401`.
  - `promote → production` was skipped.
- The previous main run, 43112892182, was cancelled.
- The step is `ci.yml:352-355` (`digitalocean/action-doctl`, `secrets.DIGITALOCEAN_ACCESS_TOKEN`). The secret is **present**: the "Required secrets are present" step passed, so the token is rejected, not missing.

**Context:**
- LEDGER L18-05 (LEDGER:3237) records 401s for both the CI token and the local token since 0161 reached main on 2026-11-08. It also records that `neoh-staging-ksfpn.ondigitalocean.app` **no longer resolves**. That hints at an account- or app-level problem, not only an expired token.
- Memory `staging-do-token-expired.md` says the last staging build was `26e06e1`. I did not re-derive that SHA.

**Tracked?** Only as a side note in L18-05 and in memory. The cat-19 row (LEDGER:54) does not list it.

**Category:** 19.

**Fix:** owner action:
1. Restore DO account access and mint a new token.
2. Update the `staging` environment secret.
3. Re-run CI on main.
4. Treat the `staging-verified-release-<sha>` artifact (`ci.yml:617-625`) as the only proof that a build is staged.

**Ledger hygiene:** give this its own cat-19 ledger row.

**P0** for the delivery pipeline, because nothing merged since roughly 2026-11-08 has reached staging. **Customer impact:** none today (no production, no customers on staging). Every fix since then is unverified on the target platform.

### DEP-02: rollback derives the current schema head from the local checkout. CONFIRMED

**Evidence**
- `scripts/rollback.sh:113-119`: if `--from` is absent or unreadable, `CURRENT_HEAD` is the last migration file in the working tree. The script only prints a hint.
- `:128-130`: the gap is built from **local** files only.
- An old checkout under-counts the gap, and `GAP_COUNT=0` prints "nothing to be incompatible with" (`:148-149`).
- `--from` is optional, even with `--apply` (`:42-50`).
- The PRE_DEPLOY precheck that would compare against the live ledger is **deliberately disabled** for rollbacks (`:200-205`). Nothing compares against the live `schema_migrations`.

**Tracked?** Only generically (T19.11, AC19.05).

**Category:** 19.

**Smallest fix:**
1. With `--apply`, require `--from`.
2. Require the target app's live `/version` SHA to equal `--from`'s `release_id`.
3. Require `max(filename)` of every file the gap needs to exist locally, and refuse if the local head is below `--from`'s head.

A live-ledger read cannot come from a runner (`ci.yml:854-857`: the DB accepts the app only), so `/version` is the reachable cross-check.

**Failing-first test:** in `test_release_immutability.py`, run `_run_rollback` without `--from` and with `--apply` → expect a non-zero exit. Today it applies. Add a second case where `--from`'s head is above the local tree head → expect a refusal.

**P1.** **Customer impact:** an operator on a stale checkout could roll production back onto a schema that the old release cannot run against, turning one incident into two.

### DEP-03: rollback never checks which app it is updating. CONFIRMED

**Evidence**
- `rollback.sh:228` takes `DIGITALOCEAN_APP_ID` from the environment.
- `:243` runs `list-deployments`, `:245` runs carry-secrets and `:249` runs `doctl apps update`. None of them checks the name.
- `scripts/carry-secrets.py` has no app-name check either (grep: no `spec.name` validation).
- CI does check, for both production (`ci.yml:773-782`) and staging (`:366-380`).

**What a wrong ID means:** the rendered spec is production-shaped (`name: neoh`, cluster `neoh-postgres`; app.yaml:59,67). A staging app ID would get staging's secrets carried into a spec that attaches the **production** database cluster.

**Tracked?** Only generically (AC19.04).

**Category:** 19.

**Smallest fix:** before line 242, run `name=$(doctl apps get "$DIGITALOCEAN_APP_ID" -o json | python3 -c '…["spec"]["name"]')` and refuse unless it equals `neoh`. Copy the CI step verbatim.

**Failing-first test:** extend the fake `doctl` in `test_release_immutability.py:419-427` to answer `apps get` with `{"spec":{"name":"neoh-staging"}}`, then assert a non-zero exit and that no `apps update` appears in `calls`. Today `apps update app-123` is called.

**P1.** **Customer impact:** a mistyped app ID during an incident could point an app at the wrong environment's database and secrets.

### DEP-03b (new): the rollback spec bypasses `render-app-spec.py`, so it drops the domain and skips validation. CONFIRMED (not in either audit)

**Evidence**
- `rollback.sh:201-205` builds the spec with `sed` on raw `infra/digitalocean/app.yaml`.
- `app.yaml` has no `domains:` block (grep returns nothing).
- CI renders production with `--domain "${{ vars.NEOH_DOMAIN }}"` (`ci.yml:811-815`).
- `render-app-spec.py:200-210` explains why that matters: "`doctl apps update --spec` applies the WHOLE desired state, so a domain … absent from the spec is detached by the next deploy — the public URL every provider calls stops resolving."
- The rollback also skips `render-app-spec.py`'s `validate()` (`:256`), `spec-drift.py`, and `doctl apps spec validate`.
- The existing test (`test_release_immutability.py:452-470`) does not assert a domain.

**Tracked?** No.

**Category:** 19.

**Smallest fix:** replace the `sed` with `python3 scripts/render-app-spec.py --env production --backend-digest … --frontend-digest … --domain "$NEOH_DOMAIN"`, keeping the PRE_DEPLOY no-op substitution. Then run `spec-drift.py` against `doctl apps spec get` before `apps update`.

**Failing-first test:** in `_run_rollback`, set `NEOH_DOMAIN=app.example.com` and assert that `applied.yaml` contains `domain: app.example.com`. That fails today.

**P1** once production has a domain (latent until then). **Customer impact:** a rollback would take the public hostname offline, breaking provider webhooks and customer logins.

### DEP-04: the drift guard fails open when the running spec cannot be fetched. CONFIRMED

**Evidence**
- Production `ci.yml:863-867`: `if doctl apps spec get … ; then spec-drift.py …; else echo "no running spec to compare — first deploy"; fi`. The step continues successfully.
- Staging has the same at `:591-595`.
- The app is already proven to exist by `doctl apps get`, under `set -euo pipefail`, at `:776` (production) and `:374` (staging).
- So a `spec get` failure here is never a first deploy. It is an auth, permission or transient failure, and it skips the domain/component removal guard (`scripts/spec-drift.py`) before a whole-spec `apps update`.

**Tracked?** No.

**Category:** 19.

**Smallest fix:** in the `else` branch, `echo "::error::cannot read the running spec of an existing app"; exit 1`. A first deploy has no app ID to confirm, so it never reaches this step.

**Failing-first test:** `test_ci_deploy_wiring.py` already parses ci.yml. Assert that the drift step's script contains no "first deploy" pass-through, or that it exits non-zero in the `else` branch. That fails today.

**P1.** **Customer impact:** one flaky DigitalOcean API call during a release could let a deploy silently remove a domain or env var someone added in the console.

### DEP-05: the rollback classifier treats risky statements as ADDITIVE. CONFIRMED

**Evidence**
- `backend/migration_safety.py:112-125` lists these as additive: `ADD CONSTRAINT`, `CREATE UNIQUE INDEX`, `CREATE POLICY`, `CREATE TRIGGER`, `ENABLE ROW LEVEL SECURITY` and `CREATE OR REPLACE FUNCTION`. Each one can reject or hide writes or reads that the previous release makes.
- `:84-85` explicitly excludes `ALTER COLUMN … DROP DEFAULT` from breaking. On a NOT NULL column, that breaks the old release's INSERTs.
- `DROP EXTENSION`, `DROP SEQUENCE` and `DROP SCHEMA` are not in `_BREAKING` at all.
- `:238-239`: one additive match plus no breaking match gives `ADDITIVE`.
- `rollback_is_safe` (`:254-268`) correctly blocks UNKNOWN, but these never reach UNKNOWN.

**Tracked?** Only generically (T19.06, F19.03).

**Category:** 19.

**Smallest fix:**
1. Move `ADD CONSTRAINT`, `CREATE UNIQUE INDEX`, `CREATE POLICY`, `CREATE TRIGGER` and RLS enable out of `_ADDITIVE_SHAPES`, into a new "needs review → UNKNOWN" list. They become ADDITIVE only when they target a table created in the same file (`_objects_created_here`, `:161-169`).
2. Add `DROP DEFAULT`, `DROP EXTENSION`, `DROP SEQUENCE` and `DROP SCHEMA` to `_BREAKING`.

**Failing-first test:** in `test_migration_safety.py`:
- `classify_sql("x.sql", "ALTER TABLE leads ADD CONSTRAINT c CHECK (score >= 0);")` should not be ADDITIVE.
- The same goes for `ALTER TABLE leads ALTER COLUMN status DROP DEFAULT;`.

Both are ADDITIVE today.

**P2.** **Customer impact:** an "approved" rollback could run the old app against a schema that rejects its writes.

### DEP-06: post-rollback verification is advisory. CONFIRMED

**Evidence**
- `rollback.sh:253-256` only `say`s the smoke-test command and exits 0.
- Production promotion runs the smoke test and fails on it (`ci.yml:884-888`).

**Tracked?** Generic (T19.11).

**Category:** 19.

**Smallest fix:** after `apps update`, run `APP_URL="$NEOH_PUBLIC_API_BASE" EXPECTED_GIT_SHA="$TARGET_SHA" infra/digitalocean/smoke-test.sh`. On failure, `exit 4` with `ROLLED_BACK_BUT_UNVERIFIED`. Require `NEOH_PUBLIC_API_BASE` with `--apply`.

**Failing-first test:** in `_run_rollback`, put a fake `curl` on `PATH` that returns 503 and assert `returncode == 4`. Today it is 0.

**P2.** **Customer impact:** a rollback that "succeeded" could leave a broken app live with nobody alerted.

### DEP-07: single worker, and capacity is not proven at target. DESIGN-GAP (documented)

**Evidence**
- `app.yaml:20-28` documents the in-process queues and why the worker is pinned to 1.
- `:368` sets `instance_count: 1`.
- `:406-407` sets `ORACLE_INTERACTIVE_JOB_WORKERS=16`.
- `docs/capacity-plan.md:88-89` (mocked models): 50 chats / 16 slots, p95 4.7 s, PASS; 100 chats, p95 13.3 s, FAIL. The audit's numbers match.

**Correction to the premise:** the audit assumes a target of about 100 agents. The owner's target is **one brokerage, 20 agents plus the owner**:
- D-2026-11-05-01, seat_limit 21 (LEDGER:3237).
- T19.10, "Measure capacity for one brokerage and 20 agents".

The single worker is a real availability single point of failure for in-process work (voice, reconstruction). Durable jobs survive through their leases.

**Tracked?** Yes: A10 (LEDGER:3534), T19.09 ("a worker killed with a job pending"), T19.10, AC19.09.

**Category:** 19 (+16).

**Fix:**
1. First, measure 10-20 concurrent chats with real model latency on DO (AC19.09).
2. Only then split the singleton scheduler and in-process queues from the durable queue (an advisory-lock leader), before raising `instance_count`.

**Test:** the T19.09 drill (kill the worker with jobs pending; durable jobs recover, in-process work is reported as lost).

**P2.** **Customer impact:** if the single worker dies, live voice and reconstruction work stalls until DO restarts it.

### DEP-08: the Spaces smoke check cannot fail in CI. CONFIRMED and **observed**

**Evidence**
- `smoke-test.sh:167-186`: when the bucket is unreachable it only `warn`s, and with no `aws` CLI it only `info`s.
- CI passes **no** Spaces or AWS credentials to either smoke step (`ci.yml:609-615` staging, `:884-888` production).
- So the check probes with the runner's (absent) credentials, never the app's.
- Observed: the last green staging run, 38549337342 (2026-10-14), logged `WARN Spaces bucket neoh-media-staging is not reachable … NOT a release failure`.
- The public-ACL hard failure (`:172-174`) is therefore unreachable in CI.
- grep finds no in-app object-storage readiness probe (`object_storage.py`, `media_storage.py` and the health modules have no `head_bucket`).

**Tracked?** No.

**Category:** 19.

**Smallest fix:** add an in-app readiness component that runs `head_bucket` with the app's own `ORACLE_S3_*` credentials and reports `degraded` vs `misconfigured` (403/404). The smoke test then asserts that component through `/health`, so the app's credentials are tested without giving them to CI. Keep the WARN for 5xx/timeout only.

**Failing-first test:** a unit test for the health component, where a fake S3 client raises 403 → expect status `misconfigured` (a release failure); 503 → expect `degraded`. It fails today, because no component exists.

**P2.** **Customer impact:** a release with wrong or missing storage credentials passes the gate, and media and document uploads fail in front of customers.

### DEP-09: historical readiness docs are not current proof. CONFIRMED (evidence gap) / ENVIRONMENT

**Evidence**
- `docs/launch-state.md:16`: "State as of 2026-10-03".
- `docs/launch-readiness/README.md:5`: run on 2026-10-03, "There is no production".
- `docs/resilience-drill-report.md:3-9`: 2026-10-02, local DinD, every provider mocked.

**Tracked?** Yes: A04 (LEDGER:3528 also notes that the readiness script reports `security_gate`/`rls_gate` PASS for production from staging-only evidence), AC19.01, AC19.11, L19-02 (production not created, an owner decision).

**Category:** 19.

**Fix:** after DEP-01 is unblocked and production exists (owner approval required, CONTRACT C4.4), run a fresh dated `neoh-launch-readiness.py --env production` at the current SHA and commit the JSON.

**P1** before launch. **Customer impact:** none until launch. The risk is a go/no-go based on six-week-old evidence.

---

## Cross-cutting notes for the caller

1. **Three places assert a guard that does not exist** (see DR-001):
   - `docs/disaster-recovery-state-map.md:34`
   - the vault's `Neoh_Disaster_Recovery.md:14`
   - memory `oracle-disaster-recovery.md:25`

   These are now known to be wrong. Correct them in the same commit as the fix. I made no writes: this task is read-only, so I skipped the vault-write steps of the CLAUDE.md session protocol.
2. **Suggested build order:** DR-001 → REC-02 → REC-03 → REC-04 (one PR on the verifier with a fake-psql harness), then DEP-03 → DEP-03b → DEP-02 → DEP-06 (one PR on rollback.sh, extending the existing fake-doctl test), then DEP-04, then DEP-05, MIG-001, JOB-001 and JOB-01.
3. **What stays with the owner:** DEP-01 (DO access/token, and the staging hostname not resolving), OPS-01 (backup schedule, escrow, versioning, timed restore) and DEP-09 (production creation).
4. **Unverified:** the claim about PR #45 (it is not on main and I did not inspect it), and the memory's "last staging build 26e06e1".
