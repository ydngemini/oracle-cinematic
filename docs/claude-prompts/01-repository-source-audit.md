# 01 — Repository source audit, dependency map and safe refactoring

> **Scope:** Build a reproducible, line-accounted inventory of the whole repository, trace what is reachable from a user or a job, re-check the 2026-10-08 audit findings at the current SHA, and turn the result into ledger rows and safe, tested fixes. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** — · **Unblocks:** 02, 03, 04, 11, 16, 17, 20 · **Ledger:** [L01](LEDGER.md#l01) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- `CLAUDE.md`, `README.md`, `docs/README.md` and the [full platform source audit](../neoh-full-platform-source-audit-2026-10-08.md) (findings A01–A10 in [LEDGER.md § Audit cross-reference](LEDGER.md#audit-cross-reference))
- Source trees: `backend/`, `oracle-app/`, `backend/db/migrations/`, `scripts/`, `infra/digitalocean/`, `tests/`
- CI: `.github/workflows/ci.yml`
- Large modules: `backend/commands_api.py`, `backend/ai_chat_store.py`, `backend/crm.py`, `backend/server.py`, `backend/reconstruction_worker.py`, `backend/auth.py`
- Frontend entries: `oracle-app/src/main.jsx`, `oracle-app/src/App.jsx`, `oracle-app/src/routes.js`, `oracle-app/package.json`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **The repository tracks 1,660 files at `745f2fa`.** Command: `git ls-tree -r --name-only 745f2fa | wc -l`. Recount at your own SHA: docs changes move it. The old prompt said 1,637 and the audit said 1,636 (`docs/neoh-full-platform-source-audit-2026-10-08.md:13`, inventory paragraph).
- **626 Python files and 190 JSX files are tracked.** Commands: `git ls-files '*.py' | wc -l` and `git ls-files '*.jsx' | wc -l`.
- **Most tracked files sit in nine top-level trees.** `backend` 766, `oracle-app` 401, `data` 102, `docs` 94, `infra` 60, `src` 59, `performance` 54, `scripts` 47, `observability-platform` 36. Command: `git ls-tree -r --name-only 745f2fa | awk -F/ '{print $1}' | sort | uniq -c | sort -rn`.
- **There are 126 SQL migrations, numbered 0001 to 0128, with 0040 and 0125 absent.** Directory `backend/db/migrations/`; the last file is `0128_finish_job_attempt_platform_scope.sql`.
- **The root Next.js prototype is not built or deployed.** `README.md:48-59` (section "What does not run"). The root `package.json` runs `next`; `oracle-app/package.json:7-8` runs `vite`.
- **The audit says its inventory counts are not review counts.** `docs/neoh-full-platform-source-audit-2026-10-08.md:13` (line-by-line review outstanding) and `:207` (batch method by SHA).
- **The six large modules are 1,494 to 3,503 lines each.** `commands_api.py` 3,503, `ai_chat_store.py` 1,972, `crm.py` 1,891, `reconstruction_worker.py` 1,573, `server.py` 1,504, `auth.py` 1,494. Command: `wc -l` on each.
- **CI runs three kinds of backend test.** `.github/workflows/ci.yml:76` runs `pytest tests compliance_engine/tests`; `:233` loops over RLS SQL files; `:248` runs a named list of live PostgreSQL tests.

## 3. Goal

Any engineer can rebuild the inventory from one command at a pinned SHA and see, for every tracked file, what it is, whether it is reachable, and how much of it has actually been read. Every finding has a `path:line`, a label, a reproduction and a ledger row. Fixes land as small tested commits, never as a one-shot rewrite.

## 4. Engineering tasks

- **T01.01 Rebuild the file inventory.** Pin the SHA and record, per tracked file: path, extension, lines, size, domain and owner, last revision, and class. Classes are production entry, runtime dependency, test, infrastructure, documentation, optional integration, archived or prototype, and orphaned. Read code, not filenames. Write it to `docs/audit/repo-inventory-<sha>.csv` (proposed).
- **T01.02 Trace the dependency graph.** Follow frontend route → component → state hook → REST or WebSocket → backend router → domain service → storage or queue → callback → customer-visible state. Include non-HTTP jobs, incoming webhooks, scheduled tasks and public capability links. Mark each edge as potential or exercised.
- **T01.03 Review Python modules in bounded, logged batches.** Per module, check inputs, imports, ownership, lifetime, tenant and agent context, error handling, validation, SQL, provider SDK use, timeouts, circuit breakers, retries, idempotency, audit, PII, logging, event-loop safety and races. Record `path:line` and tests; do not rely on regex-only assertions.
- **T01.04 Review the frontend the same way.** Cover JSX and TS components, hooks, CSS Modules and route or feature entries, with the same batch log.
- **T01.05 Review every migration in order.** Check compatibility, grants, RLS interaction, indexes, constraints, encryption, outbox, irreversible operations and cold-start restore. Cross-check the runtime role against SECURITY DEFINER functions and session claims. Explain the 0040 and 0125 gaps. Produce a migration-to-code crosswalk.
- **T01.06 List API-to-UI contract mismatches.** Pagination versus totals, legacy versus canonical client IDs, status enums, date and time-zone meaning, blank strings versus missing facts, billing event states, and provider acceptance versus delivery.
- **T01.07 Extract bounded domains from the large modules.** Put each behind a versioned interface with regression tests first. Do not rewrite a file only because it is long.
- **T01.08 Audit the bundle and import graph.** Check dead prototype imports and route state in the Vite app. Isolate high-cost libraries, unreachable panels, obsolete docs and unused provider code only after call-graph evidence.
- **T01.09 Map tests to reachable behaviour and risk.** Cover unit, integration, real PostgreSQL with RLS, React, Playwright, provider sandbox, load, backup and real-device tests. Record whether each ran and where, or only exists.
- **T01.10 Label and rate every finding.** Use the labels in §7. Rate severity separately from customer impact.
- **T01.11 Re-check audit findings A01–A10 at the pinned SHA.** This includes plaintext data (A01), failed property lookups (A02), billing event meaning (A05) and quality labels (A03, A08). Propose updated status lines for the audit cross-reference.
- **T01.12 Write the engineering matrix as L01 rows.** Each row has `path:line`, current behaviour, reproduction, fix, dependencies, risk, targeted test, benchmark, owner role, status, source SHA and fixing commit.
- **T01.13 Account for line coverage.** Keep a per-file count of lines actually read, plus a known-uninspected list. Never report lines as read until they are.
- **T01.14 Reproduce each security finding with a test.** The test fails before the fix and passes after. A finding without a test stays `UNPROVEN_RISK`.
- **T01.15 Verify whether the non-app trees are reachable.** Classify `data`, `performance`, `observability-platform`, `training_data`, `legal_sentinel` and root `tests` by evidence, not by name.

## 5. Failure and adversarial cases

- **F01.01 Cross-agent or cross-brokerage access.** Same-brokerage cross-agent data reaches a user through search, details, AI context, export, caches or signed media, or another brokerage is not denied.
- **F01.02 Repeated side effects.** A double tool invocation, a repeated callback, a crash between provider acceptance and DB commit, or a retry after credential revocation repeats an effect.
- **F01.03 Injection and exposure.** Prompt injection arrives in notes, uploads or URLs; PII reaches logs or analytics; memory stays stale after a permission change.
- **F01.04 Contention at 20 users.** 20 concurrent users and a manager hit auth, permission, pool, pagination or stuck-job failures.
- **F01.05 False completeness.** A source is reported complete when a required provider timed out, or a missing metric is treated as success.
- **F01.06 Inventory drift.** A file count comes from an old snapshot, or a file is marked read with no recorded line range.
- **F01.07 Reachable code deleted.** A cleanup removes code that a route, job or script still uses (for example the `scripts` Playwright helpers that still drive the prototype, `README.md:58-59`).

## 6. Acceptance criteria

- **AC01.01 The inventory rebuilds and reconciles.** The CSV row count equals `git ls-files | wc -l` at the pinned SHA; every row has a class and an audit state; "lines read" counts only recorded ranges. Evidence: committed generator and checker `scripts/check-repo-inventory.py` (proposed), run with exit 0. Covers: T01.01, T01.13, T01.15, F01.06.
- **AC01.02 The dependency map traces real chains.** Every route in `routes.js` and every public route in `App.jsx` reaches a router and a table, and jobs and webhooks are included. Evidence: `docs/audit/dependency-map-<sha>.md` (proposed) plus 10 chains spot-checked against code. Covers: T01.02, T01.06.
- **AC01.03 Module reviews are logged.** Every reviewed batch has line ranges, checklist results and linked tests. Evidence: batch log rows in L01. Covers: T01.03, T01.04.
- **AC01.04 The migration crosswalk has a row for every migration.** The gaps are explained, and every created function has a matching `GRANT` or a finding. Evidence: crosswalk file and a grep comparing `CREATE FUNCTION` with `GRANT EXECUTE`. Covers: T01.05.
- **AC01.05 Findings are reproducible.** Each security finding has a test that fails before and passes after; each adversarial case has a test or an `UNPROVEN_RISK` row. Evidence: test paths in L01 and the pytest command, exit code and counts. Covers: T01.10, T01.14, F01.01, F01.02, F01.03, F01.04, F01.05.
- **AC01.06 A01–A10 are re-checked.** Each has a current status with `path:line` at the pinned SHA. Evidence: proposed audit cross-reference rows in the report. Covers: T01.11.
- **AC01.07 Refactors keep behaviour.** Each extraction has regression tests that pass before and after; no deletion lands without call-graph evidence. Evidence: test runs before and after, with the call-graph citation. Covers: T01.07, T01.08, F01.07.
- **AC01.08 The test map says what ran.** Each test family lists command, exit code, counts and environment, or "exists, not run". Evidence: CI run IDs and local commands. Covers: T01.09.
- **AC01.09 The matrix lives in the ledger.** Every finding is an L01 row with the fields in T01.12; no parallel backlog exists. Evidence: L01 diff. Covers: T01.12.

## 7. Out of scope, owner decisions, category-specific rules

- **Finding labels:** `CONFIRMED_DEFECT`, `VERIFIED_GAP`, `UNPROVEN_RISK`, `PERFORMANCE_HYPOTHESIS`, `INTEGRATION_MISMATCH`, `COMPETITIVE_ENHANCEMENT`. One label per finding.
- **An inventory count is not a review count.** Report files listed, files read and lines read as three separate numbers.
- **The root `src/` prototype is reference material.** Classify it; do not revive or delete it without an owner decision.
- **Legacy MLS modules are inventoried as legacy, not as a feature** (D-2026-10-12-01). Gating them is prompt 11's task (T11.01).
- **This category fixes; it does not only plan.** Commit fixes in small tested slices.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - status rows in [L01](LEDGER.md#l01) (replaces the old "REPO-AUDIT-LEDGER.md");
  - the file inventory `docs/audit/repo-inventory-<sha>.csv` (proposed);
  - the dependency map and migration crosswalk (proposed, under `docs/audit/`);
  - security findings with reproducible tests;
  - safe refactor commits;
  - line-coverage accounting and the known-uninspected list.
- **First slice:** T01.01 + T01.13 + AC01.01, the reproducible inventory. Then T01.11 + AC01.06, the A01–A10 re-check.
- **Ledger:** add L01-01 onward per slice and link the inventory artifact under L01, per [C10](CONTRACT.md#c10-ledger-update-rule).
