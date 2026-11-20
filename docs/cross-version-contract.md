# Cross-version contract (audit 35-06)

> CI job `cross_version` ("cross-version contract (previous app vs new schema)"),
> `scripts/cross-version-contract.sh`, `scripts/cross_version/`. Advisory: it is
> in neither `release.needs` nor `promote.needs`.

## The question

Can the **previous** release keep working on the schema **this** change leaves
behind?

That question comes up on every deploy, not only on rollback. The staging and
production `PRE_DEPLOY` job (`infra/digitalocean/app.yaml`, `jobs[migrate]`)
migrates *before* traffic switches, so the old release always serves on the
new schema for a while. An application-only rollback (`scripts/rollback.sh`)
puts it back there for good.

`backend/migration_safety.py` answers the question by reading SQL. It stays
the **only authority** over application-only rollback (DEP-05), and this job
does not change it. `rollback.sh` classifies every migration the live release
has and the target does not: the set difference between the two manifests'
migration lists, plus the old filename range (35-06 F1). This job adds
evidence the classifier cannot produce. It runs the previous app and the
previous app's own tests on the new schema.

## When it runs

On every pull request and every push to `main`. Pushes to other branches skip
it.

**The previous release:**

| Event | Previous release |
|---|---|
| `pull_request` | `HEAD^1` of `refs/pull/N/merge`: the `main` tip this PR lands on |
| push to `main` | `github.event.before`, fetched if needed; `HEAD^1` when it is all zeros or cannot be fetched |
| `workflow_dispatch` | `HEAD^1` |
| local run | `--previous <sha>` |

**The gap** is a set, not a filename range:
`git diff --name-status <previous> <current> -- 'backend/db/migrations/*.sql' backend/run_migrations.py`.
Added, edited and removed files all count. A late, lower-numbered migration
counts too, such as 0144, which landed while the head was 0145 (531c41d).

| Mode | When | What happens |
|---|---|---|
| `no_previous` | no previous commit with `backend/db/migrations` | record written, exit 0 |
| `no_gap` | nothing in the gap | record `{contract_result: no_gap}`, exit 0, about one runner-minute |
| `gap` | the gap is not empty | the full run below |
| `canary` | the harness itself changed | the full run on a synthetic breaking migration (see [Canary](#canary)), after the gap run if there is one |

## What a run does

1. **Virtualenvs.** One venv when the previous and current `requirements.txt`
   and `requirements-test.txt` are byte-identical, two otherwise. A failed
   install is HARNESS.
2. **Two clusters.** Candidate **C** (`xv_candidate`, port 5432) and control
   **B** (`xv_control`, port 5433) are separate PostgreSQL 16 services. Roles
   are cluster-wide, so a gap migration that grants to or alters a role cannot
   leak into the control.
3. **The previous schema.** On each cluster, the *previous* runner applies the
   *previous* migrations as a superuser (`xv_su`). Everything is then handed
   to `xv_doadmin` (NOSUPERUSER BYPASSRLS CREATEROLE CREATEDB, DigitalOcean's
   `doadmin` profile, `docs/staging-setup.md`):
   - ownership, by `REASSIGN OWNED`;
   - the schema-scoped `ALTER DEFAULT PRIVILEGES` of 0001 and 0037, which
     `REASSIGN OWNED` does not move and `DROP OWNED` deletes;
   - ADMIN (not INHERIT, not SET) on every role a migration created, which
     PostgreSQL 16 gives a role's creator.

   The database is stamped `neoh-environment=ci`.
4. **Deploy rehearsal on C.** As `xv_doadmin`, the *current* runner runs
   `python run_migrations.py --precheck-then-migrate`, with exactly the
   environment keys of the `PRE_DEPLOY` job (`ORACLE_DB_APP_PASSWORD` absent,
   as there). Any non-zero exit (3 refused, 4 drift, an SQL error such as
   42501) is DEPLOY_WOULD_FAIL. The exit code is the verdict; the log is never
   grepped.
5. **Boots.** `scripts/cross_version/boot_probe.py` checks three combinations:
   - **P@B**, the previous app on its own schema. It must pass, or the result
     is HARNESS.
   - **P@C**, the previous app on the new schema.
   - **N@C**, the new app on an *incrementally upgraded* schema. The `rls` job
     only proves the new app on a fresh build.

   Each boot runs with `ORACLE_ENV=staging`, which fails closed (`server.py`
   lifespan: a pool or WebSocket-listener failure aborts startup), and with
   `ORACLE_RECOVERY_MODE=1`. It has per-run secrets and no Redis or provider
   key. The web phase needs `/health` 200, `/version` reporting that
   database's migration head, and 401, 403 or 422 from an unauthenticated
   `GET /api/commands`. The worker phase must stay up for 30 s. Both login
   roles must have neither `rolsuper` nor `rolbypassrls`.

   Each boot's log is scanned for schema errors. Application warnings reach
   it because `server.py` configures the root logger at import (INFO by
   default), and the probe sends stderr into the log. The scan matches:
   - asyncpg exception names whose SQLSTATE class means the schema does not
     fit;
   - PostgreSQL's own message texts, because an app that swallows a
     database error usually logs only `str(exc)`, the message. These are
     `violates check constraint` (23514), `violates not-null constraint`
     (23502), `violates foreign key constraint` (23503), `violates unique
     constraint` (23505), `permission denied for …` and row-level-security
     refusals (42501), `column … does not exist` (42703), `relation "…" does
     not exist` (42P01), `function …(…) does not exist` (42883) and `value too
     long for type` (22001);
   - the two startup-failure messages.

   Log errors are attributed the way tests are. An error the previous app
   also logs on its own schema (P@B) is a warning (`not_proven`). An error
   that appears only on C fails the boot. A failed *check* on B is HARNESS.
6. **The previous live suite on C.** This is the previous tree's own
   `tests/test_*_live.py`, from the previous tree, with
   `scripts/cross_version/xv_pytest_plugin.py` recording why each test failed.
   `test_migration_precheck.py` is left out on purpose: an older release is
   built to refuse a newer database.
7. **Attribution.** Only failing tests are rerun, on a freshly built **C′**
   (same gap) and on **B**.
8. **Verdict.** `scripts/cross_version/evaluate.py` writes the record, the
   step summary and the exit code.

## Verdicts

Per previous test:

| Verdict | Meaning |
|---|---|
| `PASSED` | passed on C on the first run |
| `UNREPRODUCED` | failed on C, passed on a fresh C′: flaky or order-dependent (warning) |
| `PRE-EXISTING` | fails on B too: not this change (warning) |
| `INCOMPATIBLE` | fails on C and C′, passes on B, and one of these holds: a schema SQLSTATE (class 42, 23, 22, 21, 0A, 2B or P0) raised through a previous-**app** frame or logged by the app; any non-assertion exception in an app frame (for example a `KeyError` after a result shape changed); a statement timeout (57014); or an infrastructure-class failure (connection, authentication, resources: SQLSTATE 08, 28, 53, 57, 58, or a connection/timeout error). The last is stricter than the plan: if it appears only on the new schema, the schema is the suspect. An app frame is an **absolute** path to an existing file under the previous `backend/`, outside `tests/` and any virtualenv. A compiled extension's relative frame (asyncpg reports `asyncpg/protocol/protocol.pyx`) never counts, so a test's own SQL refused by PostgreSQL is DRIFT. |
| `DRIFT` | fails on C and C′, passes on B, but only as an assertion, `pytest.fail` or DID NOT RAISE, or an error raised only from test frames: the previous *test* pins behaviour that changed (warning) |
| `UNATTRIBUTED` | the run had a harness problem, so nothing is attributed |

Job exit codes and `contract_result`:

| Exit | `contract_result` | Cause |
|---|---|---|
| 3 | `deploy_would_fail` | the PRE_DEPLOY rehearsal exited non-zero |
| 2 | `harness` | an install or build failed, a P@B check failed, zero tests collected, every test skipped, pytest and the plugin disagree, a rerun gave no answer, or `evaluate.py` crashed |
| 1 | `incompatible` | an INCOMPATIBLE test; or, while every P@B check passes, a P@C check fails or P@C logs a schema error that P@B does not |
| 1 | `new_boot_failed` | an N@C check fails, or N@C logs a schema error that P@B does not |
| 0 | `not_proven` | any DRIFT, UNREPRODUCED, PRE-EXISTING or skip, or a boot-log schema error P@B logs too; each gets a `::warning::` |
| 0 | `pass` | every previous live test passed on C on the first run, nothing skipped, every boot passed with a clean log |

The deploy window is why INCOMPATIBLE is red even when the classifier calls
the gap safe: the old release serves on this schema during the deploy, whatever
the classifier says.

**CLASSIFIER MISS.** When `migration_safety.py` calls the whole gap ADDITIVE
but the result is `incompatible` or carries DRIFT, the summary is headed
`CLASSIFIER MISS`. That is evidence for improving the classifier. It does not
change the classifier's verdict.

## Canary

When `scripts/cross-version-contract.sh` or `scripts/cross_version/` changes,
the job also runs on a copy of the migrations that has one extra file:

```sql
ALTER TABLE process_heartbeats ADD CONSTRAINT xv_canary_hb CHECK (false) NOT VALID;
ALTER TABLE leads ADD CONSTRAINT xv_canary CHECK (false) NOT VALID;
```

The classifier calls both ADDITIVE.

**What each constraint breaks:**
- **`process_heartbeats`** is written by the previous app's own code. Every
  worker upserts its row as the last startup step (`server.py` lifespan →
  `process_heartbeat.beat_once`). Recovery mode does not stop this, and only
  the worker role runs it.
- `beat_once` swallows the error and logs `heartbeat not written (new row for
  relation "process_heartbeats" violates check constraint "xv_canary_hb")`.
  The P@C worker phase logs it and P@B does not, so the gate must fail with an
  attributable 23514 on the P@C boot.
- **`leads`** is written only by the previous live tests' own SQL (no previous
  live test inserts leads through app code). Those failures are DRIFT. They
  show the suite side of the run, but cannot prove the gate fails.

**When the canary passes:** only if the gate **fails** on it. That means
`contract_result` is `incompatible`, and the P@C boot (or an INCOMPATIBLE test)
carries a 23514 that P@B does not. A 23514 that shows up only as DRIFT is not
enough, because DRIFT is a warning and the gate would not have failed.
Otherwise the job fails with "the gate cannot fail".

This is the only proof that the gate can fail, and it runs in the harness
PR's own CI. CI run 44741248526 went green for the wrong reason: asyncpg's
relative frame was taken for app code. That has since been fixed.

## Reading the record

The artifact is `cross-version-<prev7>-<sha7>-<run_attempt>`, kept for 90
days. It holds `cross-version-report.json` (`cross-version-canary-report.json`
for the canary), plus the build, deploy-rehearsal, boot and suite logs. Files
are scrubbed of every password before upload.

Record fields (schema 1):

| Field | Contents |
|---|---|
| `previous_sha`, `current_sha`, `prev_head`, `new_head` | the pair and the two migration heads |
| `gap` | `[{name, status, sha256, classification, reasons}]` |
| `classifier` | `rollback_safe`, `blocking`, `whole_gap_additive` |
| `deploy_rehearsal` | the command, `applied_as`, exit code and result |
| `boots` | `P@B`, `P@C`, `N@C`: ok, harness, failed checks, log hits |
| `tests` | `{nodeid: {verdict, kind, sqlstate, app_frame, exc_type, message}}` |
| `totals` | the suite's totals |
| `contract_result`, `exit_code`, `errors`, `warnings`, `harness_reasons` | the verdict and why |
| `pg_server_version` | the server the run used |
| `migrations_applied_as`, `app_role` | `nosuperuser-bypassrls-owner`, `oracle_app_login` |
| `run_id`, `run_attempt`, `canary`, `canary_result` | the run |
| `rollback_app_only_supported` | the classifier says safe **and** `contract_result == pass` (not for a canary) |

`rollback_app_only_supported` is informational. `rollback.sh` does not read it
yet; consuming it is follow-up F2, and it can only add refusals.

No password value ever appears in the record. DSN user info, `password=`
values and the run's own password variables are redacted.

## What it proves, what it reports, what it does not prove

**Proves, for this (previous, current) pair:**
- the PRE_DEPLOY command succeeds as a NOSUPERUSER BYPASSRLS owner on an
  incrementally migrated schema;
- the previous app boots, serves and runs its workers on that schema under
  fail-closed startup;
- the new app does the same;
- each previous live test either passes there or has a recorded, attributed
  reason.

**Only reports:**
- DRIFT, UNREPRODUCED and PRE-EXISTING failures, and skips (`not_proven`);
- the classifier's verdict next to what happened, including CLASSIFIER MISS.

**Does not prove:**
- anything the previous live suite and boot never exercise. App code that
  swallows a database error and only fails an assertion shows up as DRIFT.
- behaviour on production data, lock durations, or mixed-version replicas.
  The candidate database is empty.
- that the doadmin stand-in matches DigitalOcean exactly. The extension
  allowlist, pgaudit and the alpine image's collation can differ.
- rollbacks that skip releases. Only HEAD^1 (or `before`) is checked; see F3
  and F9.
- races. A race-only incompatibility can come out UNREPRODUCED.

## When it is red

- **INCOMPATIBLE:** use **expand → migrate → contract**. In release N, stop
  using the thing you want to drop or change: ship code that works with both
  shapes. Drop it in release N+1, once no running release uses it. A
  DESTRUCTIVE contract-phase PR is then INCOMPATIBLE only if N still uses what
  it drops.
- **DEPLOY_WOULD_FAIL:** the staging PRE_DEPLOY job would fail on this gap.
  Usual causes are superuser-only SQL (a function `SET` clause, `ALTER
  SYSTEM`), a privilege the owner lacks (42501), an edited applied migration,
  or a removed one. Fix the migration; never edit an applied one.
- **HARNESS:** the run proved nothing. Read `harness_reasons`. A new required
  production secret, for example, needs adding to `boot_probe.py`. Re-run once
  for a PyPI outage.
- **`not_proven` with DRIFT:** read the test. If the previous test pinned
  behaviour this change alters on purpose, that is expected, but app-only
  rollback across this pair is then unproven.

## No override

There is no declaration file, exemption list, flag or environment variable
that changes an outcome, and no "known failure" list.
`backend/tests/test_cross_version_contract.py` scans the harness for that:
which environment variables it reads, which options it accepts, and that no
exemption file is referenced. `migration_safety.py`, `rollback.sh` and the
`rls` job are unchanged by this job. Becoming a required check, or joining
`release.needs`, is an owner decision (F7), after the false-red rate has been
measured.

## Running it locally

You need two PostgreSQL 16 clusters on localhost (ports 5432 and 5433, or
`XV_CANDIDATE_PORT` and `XV_CONTROL_PORT`) with the same `postgres` password:

```sh
XV_PG_SUPERUSER_PASSWORD=... scripts/cross-version-contract.sh run \
    --previous <sha> [--current <sha>] [--canary] --work <dir>
```

The work directory holds a worktree and one or two virtualenvs (over 1 GB).
Never put it in `/tmp` or the home directory on a nearly full disk.

## Cost

These are estimates. The harness PR's own CI run records the measured numbers.

| Path | Share of runs | Runner time |
|---|---|---|
| skip (`no_gap`) | about 85% | about 1 runner-minute |
| gap | the rest | about 6–8 runner-minutes |
| canary | only when the harness changes | about 5 more minutes |

The job runs in parallel with the others.

## Follow-ups

- **F2:** `rollback.sh --apply` requires a `pass` record for the exact pair.
- **F3:** a dispatch mode for an explicit pair.
- **F4:** EXPLAIN-level checks of the previous release's SQL.
- **F5:** boot the previous registry image.
- **F6:** a candidate database with data in it.
- **F7:** make the job required (owner decision).
- **F9:** run against the release actually live on staging.
