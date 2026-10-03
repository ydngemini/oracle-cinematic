# Launch readiness runs

Saved output of `scripts/neoh-launch-readiness.py`, which is read-only and
prints no secret. Re-run before relying on it, because infrastructure changes
under it. Run on 2026-10-03 at 20:13 UTC (same results as the 20:03 run).

| File | What it is |
|---|---|
| [`production.json`](production.json) | `--env production` |
| [`staging.json`](staging.json) | `--env staging`, against the live `neoh-staging` app |
| [`owner-gates.json`](owner-gates.json) | Launch conditions only a person can close. The script reports each one |

## Production: BLOCKED (5 PASS · 9 WARN · 30 BLOCKED)

**There is no production.** No app `neoh`, no `neoh-postgres` / `neoh-redis`,
none of the 10 CI secrets on the `production` GitHub environment (the
environment exists, with required reviewers), and no `NEOH_DOMAIN`. Twenty-one
of the 30 blockers are "not verifiable: the app does not exist". The rest are:

- the security gate (`staging_dast_no_blocker`)
- the owner gates: email DPA, DashScope region, counsel sign-off, resilience drills on staging, support contact

What passes: doctl access, the registry (both images pushed), the GitHub
`production` environment with reviewers, the RLS gate, and the erasure-owner
owner gate (doadmin has BYPASSRLS on DO).

## Staging: BLOCKED (25 PASS · 17 WARN · 4 BLOCKED)

The first live run against `neoh-staging` (app `3386001b…`,
`https://neoh-staging-ksfpn.ondigitalocean.app`).

**PASS:**
- app ACTIVE in `nyc` with api / web / worker
- api 2 instances, exactly 1 worker, sizing matches the reduced render
- images digest-pinned, api and worker on the same image
- Postgres and Valkey online and attached; first backup 0.5 h old
- Spaces configured
- core secrets present (names only)
- recovery mode ON, demo login off, `ORACLE_ENV=staging`
- CORS: spec and live probe
- `/health` 200 (the API's JSON); worker alive
- CSP + HSTS on the SPA
- GitHub `staging` environment exists; the RLS gate passes

**BLOCKED:**
1. No AI provider key, so Neoh cannot answer.
2. `git_sha unknown`. Expected: the running image is the owner's hand-built bootstrap, not a CI build.
3. `DIGITALOCEAN_ACCESS_TOKEN` not yet on the `staging` environment. The other 9 CI secrets are set.
4. Security gate FAIL. Needs the OWASP ZAP run, which staging now makes possible.

**WARN** (none blocks launch):
- Stripe test keys and SMTP unset. Staging cannot charge or send anyway, because of recovery mode.
- No voice, messaging or MLS credentials.
- `ORACLE_ALERT_EMAIL` unset.
- Postgres and Valkey have **0 trusted sources**. This needs a decision, see `docs/staging-setup.md` §9.
- The running spec predates today's slots (the next CI deploy adds them).
- `NEOH_DOMAIN` unset; `STAGING_ENABLED` unset.
- No operator token given.
- Optional providers and 3D not configured.
- Open warning-level owner gates.

The live smoke test on the same app
(`APP_URL=https://neoh-staging-ksfpn.ondigitalocean.app SPACES_BUCKET=neoh-media-staging`)
gave these results:
- **PASS:** health, worker, router mounted, all security headers, Twilio/Plivo/Google callbacks, API schema not published.
- **WARN:** the Stripe and Telnyx webhooks answer 503, shown as 504 by DO's edge, because they are not configured on staging.
- **FAIL:** `git_sha unknown` only (the bootstrap image).
