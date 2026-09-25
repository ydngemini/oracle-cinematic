# Neoh — Release Hardening Report (Mission 6)

**2026-09-25.** HEAD `1a369fc`, migration head `0112_process_heartbeats.sql`,
12 commits from `3127d7b`. Detail lives in `docs/release-hardening-audit.md`,
`docs/deploy-digitalocean.md`, `docs/release-checklist.md` and
`docs/supply-chain-policy.md`. This report answers the brief's final list.

**Not exercised against a real DigitalOcean app.** No DO resources exist yet.
Everything below is verified by linting, executed scripts (a mocked `doctl`,
throwaway Postgres containers, a real local registry) and 2,600+ tests, and
the restructured workflow has run on GitHub once: tests green, deploy jobs
correctly skipped. The first real staging deploy is still the first real test.

---

| # | Item | Result |
|---|---|---|
| 1 | Current HEAD | `1a369fc` |
| 2 | Migration head | `0112_process_heartbeats.sql` |
| 3 | Deployment architecture found | One CI job built images, pushed them, migrated, then applied an app spec that **built from source**. Manual dispatch only. No staging, no environments, no secrets on the repo. |
| 4 | Immutable-artifact mismatch | **Confirmed, and worse than suspected.** App Platform rebuilt all three components; CI's images were orphaned. A runtime `ORACLE_GIT_SHA=${_self.GIT_COMMIT_HASH}` override made `/version` report intent, so the smoke test would have passed on any image. A CI comment and the runbook both claimed digest pinning. |
| 5 | Artifact model | One SHA → one backend digest + one frontend digest + a migration set, recorded in `release-manifest.json`. Digests, never tags. |
| 6 | Backend deployment | api + worker pinned to the same `neoh-backend@sha256:…`; migrations run from that image by digest. |
| 7 | Frontend deployment | Static site pinned to `neoh-frontend@sha256:…`, built **environment-agnostic** (empty API/WS bases → same origin), so one bundle serves both environments. |
| 8 | Release manifest | `scripts/build-release-manifest.sh`: digests read back from the registry, migration head + aggregate hash, run id. No secrets. |
| 9 | Staging architecture | `release` job: the **only** builder. Deploys to staging, smoke-tests, then uploads `staging-verified-release-<sha>`. Staging is rendered from the one spec with `ORACLE_RECOVERY_MODE=1`. |
| 10 | Production approval gate | `promote` job: manual dispatch + `confirm: deploy` + `main` + the `production` environment's required reviewer (**created on GitHub**, ydngemini, main-only). Never builds; deploys the staging-verified digests, refuses unstaged SHAs. |
| 11 | Environment separation | Rendered specs share no app/cluster/bucket/domain (enforced). Each job refuses unless `doctl` reports the expected app. Each database must identify itself (`COMMENT ON DATABASE … 'neoh-environment=…'`) or the migration precheck fails closed. |
| 12 | Migration classification | `backend/migration_safety.py`: can the *previous* release run on the new schema? 84 additive / 22 destructive / 4 unknown across the repo; unknown counts as unsafe. |
| 13 | Compatibility strategy | Additive → roll the app back, leave the migration. Destructive → `APPLICATION ROLLBACK UNSAFE`: forward fix, compatibility patch, or PITR — none automated. |
| 14 | Worker rollout | Worker heartbeats with its SHA (0112); the smoke test requires a live worker **on this release**, waiting out the 300 s drain grace. Clean shutdown deletes its row, so only crashes linger. |
| 15 | Readiness / liveness | Separate probes: `health_check` stops traffic, `liveness_health_check` restarts. `termination` drain 30 s / grace 120 s (api), 300 s (worker). |
| 16 | Smoke-test changes | `/version` must match the SHA; a worker on this SHA must be alive; seven provider callback URLs must return their **exact** unsigned status (an unmounted POST is answered 403 by CSRF, so "not 404" would pass a missing webhook). |
| 17 | DigitalOcean rollback | `scripts/rollback.sh`: redeploys a previous digest, dry-run by default, never rebuilds, never touches the DB, refuses across a destructive migration (exit 3). Avoids DO's rollback endpoint because it **pins** the app and blocks the fix-forward deploy. Executed against a mocked `doctl`. |
| 18 | Legacy AWS paths | All ten `infra/scripts/*.sh` refuse without `NEOH_LEGACY_AWS=1` (one said "to prod"). `infra/DEPLOY.md` bannered. Nothing deleted. |
| 19 | Config drift | `scripts/spec-drift.py` blocks a deploy that would **remove** something from the running app (console-added env var, component, ingress, binding, domain). Names only. |
| 20 | Security / supply chain | Least privilege pinned by test. Trivy scans what ships, fixable CRITICAL blocks. Base images + Trivy digest-pinned. SBOMs per release. Fail-fast secret presence check. |
| 21 | Tests added | Release immutability, render, drift, migration safety, heartbeat, callback routes, Plivo outbound webhooks, backup/restore contracts. |
| 22 | Test results | Backend **2,601 passed, 0 failed** at the last full run; targeted suites green since. actionlint (with shellcheck over every `run:` block) clean; shellcheck clean on all release/DR scripts. |
| 23 | Remaining manual DO setup | Create staging + production DO resources; put secrets on each **environment**; set `NEOH_DOMAIN` per environment (not the console); stamp each DB's `neoh-environment`; allow both origins on the Maps key; then `STAGING_ENABLED=true`. |
| 24 | Anything preventing build-once/promote | Nothing in the pipeline. Operationally, production is blocked until staging exists — by design. |
| 25 | Recommended rollback drill | First week after staging exists: deploy release A → B (additive) → `rollback.sh --to A --apply` on **staging**; then B → C with a destructive migration and confirm the refusal. Quarterly thereafter. |

---

## Defects found along the way that were not about releases

These were found *because* the release checks exercised real paths:

- **Every AI-placed outbound Plivo call fetched a 404** for its instructions
  since 2026-09-22 — the answer URL named `/api/commands/…`, the handler
  lives at `/api/telephony/…` (`aa8bd58`).
- **The two outbound Plivo webhooks took no signature.** The answer route
  handed out the live call's stream URL and bridge token for any posted UUID;
  the status route tore down any posted call (`aa8bd58`).
- **The runbook's domain step would have detached the production domain on
  every deploy** — attached in the console, absent from the spec (`f481874`).
- **A push to `main` could cancel a production deploy between its migrations
  and its app update** (`478d2d1`).

## The lesson this mission kept teaching

Four times, a protection existed and was defeated by something asserting an
answer instead of deriving it: a comment claiming digest pinning, a version
endpoint reporting the intended commit, a workflow naming an environment that
did not exist, a runbook describing a flow that would have destroyed the
domain. Each fix made the check *derive* the fact — and each is now held by a
test that fails if the prose and the code disagree again.
