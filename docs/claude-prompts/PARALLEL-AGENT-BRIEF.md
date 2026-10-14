# Brief for parallel worktree agents

> This is the common brief the coordinating session gives each agent it runs in a git worktree. It adds to [CONTRACT.md](CONTRACT.md) and never replaces it. It lives in the repo because process restarts wiped the session scratchpad copy twice on 2026-10-14.

## Read first
- [CLAUDE.md](../../CLAUDE.md)
- [CONTRACT.md](CONTRACT.md), which is binding
- your rows in [LEDGER.md](LEDGER.md)

**Record scope (owner decision D-2026-10-13-01):**
- An agent sees three kinds of record:
  - records assigned to them;
  - the unassigned brokerage pool;
  - records shared with them, view or edit per share.
- The broker owner, the platform admin and server pipelines marked brokerage scope see the whole brokerage.
- Consent, opt-out and frequency checks always stay brokerage-wide.

**Existing patterns to reuse:**

| Migration | What it does |
|---|---|
| 0131 / 0134 / 0137 | Clients, contacts, and a contact follows its client |
| 0140 | Communications follow the record: inline `<t>_linked_*` policies, `app_linked_access()`, `record_access.require_linked_edit` |
| 0141 | Indexable tenant wall |
| 0143 / 0151 | Leads and their child rows follow the seller client |
| 0132 / 0152 | Sealed text with the per-tenant key |

## Hard rules
- **Never push.** Never touch staging, production or any external provider. Never send a real message or place a call. Commit on your worktree branch only.
- **Commit a `wip(...)` after every milestone.** Process restarts have stopped agents mid-task, and the coordinator resumes from your commits.
- **Use only your assigned migration number.**
- **Staging and production migrate as DigitalOcean `doadmin`:** NOSUPERUSER, BYPASSRLS, owns the tables.
  - Never write `CREATE FUNCTION … SET app.x = …`. Use `set_config()` in plpgsql and restore the previous value. No superuser-only SQL.
  - Each migration must leave the previous release working: expand/contract, because migrations apply before the new code serves.
  - Rehearse with `scripts/rehearse-migrations-nosuperuser.sh <first-new-number>`. The guard is `backend/tests/test_migrations_managed_postgres.py`.
- **New functions need explicit GRANTs.** Migration 0003 revokes PUBLIC.
- **RLS policies:**
  - A policy written as a SQL function with sub-selects runs per row, so inline it instead.
  - Never restate the RLS predicate in app SQL.
- **No f-string or concatenated SQL.** Bandit B608 fails CI against `.bandit-baseline.json`.
- **Personal data:** classify new personal-data columns in `backend/privacy_data_map.py` and `docs/privacy-data-map.md`. Never log names, phone numbers, emails, addresses or card numbers.
- **New env vars** go in `.env.example`.
- **Test values that look like secrets** in new files get flagged by gitleaks. Review them, then add their fingerprints to `.gitleaksignore`.
- **Platform Use Policy:**
  - If a change alters who sees data, how people are contacted or what the AI does, draft the wording.
  - Never edit the policy or bump its version; the owner approves wording (C5.7).
- **LEDGER and CI:**
  - Update your LEDGER rows in the same commit.
  - The CI live-test list in `.github/workflows/ci.yml` is **one line**. Add your file to that line only, and never reflow it.
- **Disk is about 96% full.** Write atomically, make no large copies, and drop any scratch databases and bench data you create.
- **Never edit files while a pytest suite is running.**
- **Tenants and users in tests:**
  - A test tenant needs a `postal_address` before client email can be staged (L17-08).
  - Broker owners must enrol two-step sign-in (L17-19).
  - Sending a text, recording a call excerpt or saving a chat turn needs `ORACLE_ENCRYPTION_MASTER_KEY`; set a throwaway one with monkeypatch.

## Environment
- **venv:** `/media/ydn/SYPHER_CORE2/Oracle/venv`.
- **Backend tests:** `cd backend && ORACLE_SKIP_DOTENV=1 python -m pytest -q -p no:cacheprovider tests/`. Read the summary line for the result.
- **Frontend:**
  - Symlink `oracle-app/node_modules` to the main checkout's.
  - Then run `npx vitest run`, `npm run lint`, `npm run build` and `npm run bundle:check`.
- **Live DB tests:** use `scripts/livetest-local.sh`, which creates throwaway roles on the local dev DB in Docker-in-Docker.
  - Migrate with `BACKEND_DIR=<worktree>/backend scripts/livetest-local.sh migrate`.
  - Run tests with `… scripts/livetest-local.sh python -m pytest -q -p no:cacheprovider tests/<file>`.
  - Your worktree must sit under `/media/ydn/SYPHER_CORE2/Oracle`; Docker-in-Docker can't mount other paths.
  - The dev DB is shared, so another branch's migration can break your live test. Judge that on merged main.
- **SQL proofs:**
  - Files: `backend/tests/rls_security_review.sql`, `rls_brokerage.sql`, `platform_rls_test.sql`, `privacy_lifecycle.sql` and `rls_tenant_wall.sql`.
  - Run each with `docker exec -i oracle-db-1 psql -U postgres -d oracle -v ON_ERROR_STOP=1 < file`.

## Report (final message)
1. What changed and why, in plain words, plus the commit SHA and files.
2. Tests: exact commands and results, including the live tests, SQL proofs and rehearsal.
3. Measurements: EXPLAIN ANALYZE before and after, wherever a query path changed.
4. Defects found on the way.
5. Owner questions, drafted policy wording, and the defaults you chose.
