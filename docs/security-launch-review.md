# Neoh security launch review — 2026-10-01

**Verdict: FAIL.** No exploitable path to another brokerage's data or to
platform admin was found or remains. But the gate's own conditions are not met:
no DigitalOcean staging exists to run DAST against, one HIGH
defense-in-depth finding needs either a fix or explicit acceptance, and three
controls depend on settings outside the repository. See *Launch gate* and
*Blockers before real brokerage data*.

| | |
|---|---|
| Repository | `ydngemini/oracle-cinematic` |
| HEAD at start | `0f0b851a16f73c35f176d8852ee2bb0f54fb7e63` (= origin/main) |
| Migration head | `0116_mls_search_order_index.sql` at start → `0119_security_review_hardening.sql` |
| Environment attacked | local production-shaped topology (`performance/topology/`: 2 API replicas + worker + nginx + Valkey + provider mock, PostgreSQL 16, `ORACLE_ENV=loadtest`, recovery mode on). **No DigitalOcean app exists** (`doctl apps list` is empty), so nothing on DO, production or third-party infrastructure was touched. |
| Standards applied | OWASP Top 10:2025, ASVS 5.0.0, API Security Top 10 (2023), WSTG 4.2, Top 10 for LLM Applications 2025; CWE per finding |
| Companion documents | `security-threat-model.md`, `security-incident-response.md`, `credential-rotation.md`, `security-pentest-brief.md`, `security-launch-gate.json` |

## Method

1. Seven parallel code reviews (auth/session, tenancy/IDOR/mass assignment,
   PostgreSQL RLS/definer/grants, webhooks/telephony/billing/outbound,
   uploads/SSRF/injection, AI/tool layer/XSS, web/CI/supply chain/infra), each
   required to confirm by running code or SQL.
2. Live attacks on the topology with two synthetic brokerages (`perf-brokerage-01`
   = A, `-02` = B; every tenant's records carry a unique sentinel string).
   Scripts in `performance/security/`, each refusing to run unless the target
   reports a test environment:
   - `idor_sweep.py` — every ID-bearing route in the OpenAPI schema replayed
     as A's agent and A's owner with B's identifiers, with schema-synthesized
     bodies so writes reach authorization.
   - `session_attacks.py` — revocation, forged role/tenant claims, `alg=none`,
     tampering, oversize, absolute session age.
   - `web_attacks.py` — body limits, CORS, CSRF, Stripe forgery/replay, billing
     roles, offboarding, route takeover, admin surface, WebSocket abuse.
   - `ai_chain_attack.py` — confused deputy and indirect prompt injection with
     the provider mock acting as a **fully hijacked model**.
3. Real-PostgreSQL attacks as the application role (`tests/rls_security_review.sql`).
4. gitleaks over all 327 commits; Bandit SAST over the backend.
5. Fix, add regression tests, re-attack, re-run every suite and a load A/B.

## Attack surface (from the running app)

- 418 routes: 362 behind `require_context`, 3 `require_policy_context`, 9
  `require_platform_admin`, 4 broker-or-admin, 52 inline `require_role`.
- 52 with no session dependency: 12 auth bootstrap, 4 health/version/static,
  20 signed webhooks, 1 OAuth callback, 8 capability-token routes, 7 WebSockets
  that authenticate in-handler.
- 135/174 request models use `extra="forbid"`; the only identity field honoured
  from a body (billing `tenant_id`) is checked against the session.
- Public ingress on App Platform: `/api /auth /billing /admin /portal /ws
  /health /version` → api; `/` → static SPA. Worker, Postgres and Valkey have
  no public ingress. `/docs /redoc /openapi.json` are no longer published in
  production.

## Controls that were already sound (verified, not assumed)

- JWT: HS256 pinned, `none`/other algorithms refused, 8 KB cap, `exp` checked,
  issuer/audience mandatory outside dev; `ORACLE_SECRET_KEY` and the master key
  fail closed if missing, short or placeholder.
- Passwords: scrypt (N=2¹⁴, r=8, p=1, 16-byte salt), constant-time compare,
  10–256 characters.
- Reset tokens and invitations: 256-bit, SHA-256 at rest, single-use under a
  CTE/UPDATE race guard, siblings retired, role and tenant taken from the row.
- RLS: 129/129 tenant tables RLS + FORCE; the app role is not superuser, not
  BYPASSRLS and owns no table; no context → zero rows; live loop over 115 tables
  found 0 cross-tenant reads and every cross-tenant write refused (42501).
- Two-person role change (approver ≠ requester, case-insensitive), no
  `platform_admin` assignment through the API.
- Telnyx Ed25519 + idempotent message ids; Twilio/Plivo signatures fail closed;
  webhook URLs from the configured base, never the Host header.
- Uploaded media served with a server-sniffed type, `nosniff`, `CSP
  default-src 'none'`; no SVG/HTML accepted; ClamAV on chat attachments.
- No SQL injection, command injection, open redirect, archive traversal or
  unsafe deserialisation found (all subprocess calls are argv lists; dynamic SQL
  uses allowlists).
- Provider credentials encrypted per tenant and never returned by an API.
- The frontend renders model output as text; no HTML sinks.
- CI: fork PRs run with no secrets; promote SHA validated as an ancestor of main.

## Findings

Severity follows the launch policy; a malicious authenticated tenant user is in
scope, so needing a login does not lower severity. Status: **Fixed** (with the
regression test that pins it), **Open** (owner/timeframe), **Accepted?** (needs
an explicit launch decision), **External** (outside the repository).

### Critical

| ID | Finding | Status |
|---|---|---|
| BILL-1 | Stripe webhook accepted forged events whenever `STRIPE_WEBHOOK_SECRET` was unset: stripe-python verifies an HMAC keyed with `""`, and production config did not require the secret | **Fixed** |

**BILL-1 in full.** *Precondition:* the secret unset (it ships blank in the DO
env template and boot did not require it). *Reproduction:* HMAC-SHA256 with an
empty key over `"{ts}.{payload}"`; `construct_event(payload, sig, "")` returned
a `checkout.session.completed` event (reproduced in-process). *Impact:* anyone
could activate or cancel any brokerage's subscription. *Root cause:* an empty
string treated as a key. *Fix:* the webhook returns 503 unless the secret is a
`whsec_` value; production boot requires it whenever `STRIPE_SECRET_KEY` is
set (`billing.py`, `config.py`). *Tests:*
`test_an_empty_webhook_secret_refuses_instead_of_verifying_with_an_empty_key`;
live: empty-key, missing and wrong-key signatures all refused (400).

### High

| ID | Finding | Status | Regression |
|---|---|---|---|
| AUTH-1 | No session revocation: tokens survived password change, reset, demotion, deactivation and tenant moves for 24 h | **Fixed** (0117 `session_epoch` + `app_begin_session` on every tenant transaction) | `test_every_tenant_transaction_of_a_session_runs_the_account_check`, `rls_security_review.sql`, live `session_attacks.py` 20/20 |
| AUTH-2 | `GET /auth/policy-acceptance` re-minted a fresh 24 h token from old claims → sessions renewed forever, and an account with no live row escaped the policy gate | **Fixed** (renewal bound to the verified row, original `auth_time`, 7-day absolute cap) | `test_policy_renewal_refuses_an_account_with_no_live_row`, `test_a_session_ends_seven_days_after_the_password_was_typed` |
| OFF-1 | No way to remove or deactivate a user at all — nothing ever set `is_active=false` | **Fixed** (`/api/brokerage/team/{id}/suspend` + `/reinstate`, owner-only for agents, platform admin for owners, audited) | live: suspended agent's session 401, sign-in refused, old session stays dead after reinstatement |
| UPL-1 | No request-body limit; FastAPI parses JSON/multipart before auth → anonymous memory/disk exhaustion (50 MB parsed before a 401) | **Fixed** (`body_limit_middleware.py`, per-route caps, declared and streamed) | `test_body_limit_*`; live: anonymous 3 MB → 413, declared 600 MB → 413 before any body |
| UPL-2 | Public upload link buffered up to 512 MB before checking its token | **Fixed** (token checked before parsing; 2 concurrent buffered uploads per replica) | `test_the_public_upload_checks_its_token_before_reading_the_body`; live: 40 MB to a guessed token → 404 in 0.6 s |
| WEB-1 | With `ORACLE_CORS_ORIGINS` unset (it was, in `app.yaml`), production granted credentialed CORS and `/ws` to `localhost:*` — and refused its own origin | **Fixed** (no localhost outside dev; derive from the base URL; production refuses to boot without an https origin; `app.yaml` sets it) | `test_production_cors_never_falls_back_to_localhost`; live: 6 hostile origins refused |
| BILL-2 | Subscription status enforced only in the frontend; an unpaid self-signup could buy numbers on the platform carrier account, register 10DLC, release outreach, queue GPU | **Fixed** (`require_active_subscription` on 12 spend/contact routes) | `test_spend_and_outreach_routes_require_a_live_subscription` |
| OUT-1 | Recovery mode missed the direct egress functions (Twilio SMS/calls, Google Calendar, custom calls, number provisioning) and usage metering; blocked jobs retried after recovery | **Fixed** (guard on 9 egress functions + metering; blocked jobs dead-lettered) | `test_recovery_mode_blocks_every_direct_provider_egress[9]` |
| HOOK-1 | Any tenant could mark another brokerage's number as its verified caller ID (shared platform Twilio account; no uniqueness) | **Fixed** (0119 unique verified caller ID; HOOK-2 removes the agent path) | `rls_security_review.sql` (index), live route takeover refused |
| RLS-1 | Defense in depth: platform-admin rights are a session GUC, so any SQL running as the app role could `set_config` itself into any tenant or admin | **Accepted?** — see below | — |

**RLS-1 — needs an explicit launch decision.** This is not exploitable through
the application: the GUCs are set only by `apply_rls_context` from a verified
session, every query uses bound parameters, and the review (code reading +
Bandit) found no SQL injection. The fix — admin work over a separate database
role tested with `pg_has_role`, or HMAC-signed context — rewrites the
predicate of 123 policies and is too large to do safely inside this pass.
Compensating controls in place: app role is neither superuser, BYPASSRLS nor
owner; no TEMP; definer functions pin `pg_temp`; schema_migrations read-only;
SAST gate in CI. Recommendation: accept for launch with an owner and a date
(next security milestone), or block launch on it.

### Medium

| ID | Finding | Status |
|---|---|---|
| AUTH-3 | Signup could register the operator's `agent_id` (attribution collision) | Fixed — reserved identities refused at register/accept |
| AUTH-4 | Login timing enumerated accounts (no scrypt for unknown users) | Fixed — dummy-hash verify |
| AUTH-5 | `/auth/forgot` timing + synchronous SMTP in the event loop | Fixed — all work after the response, SMTP in a thread |
| AUTH-6 | Per-account login limit counted every attempt → anyone could lock out any account, incl. the operator | Fixed — (account, network) 10/min + account-wide 50/min |
| AUTH-7 | Open WebSockets never re-validated expiry or revocation | Fixed — close at `exp`, re-check every 5 min, check before accept |
| AUTH-8 | An owner could invite a second owner (sock puppet defeats two-person rule) | Fixed — owner invites only by platform admin |
| AUTH-9 | Google OAuth `state` not bound to the starting browser (victim's Google tokens into attacker's tenant) | Fixed — HMAC binding cookie |
| AUTH-12 | No MFA anywhere, incl. the static operator login | **Open** — see *MFA* |
| AI-1 | The model could rewrite the selected client's email/phone and then stage outreach to it | Fixed — contact fields not model-writable (live chain 2) |
| AI-2 | `publish_to_marketplace` silently re-priced/re-scoped a LIVE publication | Fixed — upsert only rewrites drafts; 409 otherwise |
| AI-3 | Record data in the system prompt with no data boundary on the main providers | Fixed — fenced, labelled untrusted, `<` escaped |
| AI-6 / OUT-4 / TEN-1 / TEN-2 | Approvals tenant-wide: any agent could release others' outreach, approve GPU spend, or approve their own site publish through the bidding route; any agent listed every approval's payload | Fixed — requester or owner decides; FINANCIAL/LEGAL/publishing need an owner; routes bound to their action type; agents list their own |
| BILL-3 | Stripe events not deduplicated or ordered → a late event could re-activate a cancelled tenant; unpaid checkout granted access | Fixed — 0118 event ledger in the effect's transaction + `last_event_created` ordering + `payment_status` |
| BILL-4 / TEN-3 | Any agent could open the brokerage's Stripe portal/checkout | Fixed — owner-only |
| HOOK-2 / TEN-8 | Any agent could re-point their route at any number and flip its carrier | Fixed — identity fields owner-only; carrier never changed by that endpoint |
| HOOK-3 | Webhook secrets and Plivo bridge tokens in query strings reached uvicorn's access log | Fixed for logs (query strings and capability path segments redacted); the static ACS/custom-call query secret remains — **Open, LOW** |
| HOOK-4 | A failed STOP suppression was swallowed → opt-out lost permanently | Fixed — 503 so Telnyx retries |
| OUT-3 | An agent's email could go out through a colleague's personal SMTP/Google account | Fixed — fallback never onto another user's label |
| RLS-2 | `audit_ledger` insert policy `WITH CHECK (true)` → forged rows in any tenant's trail | Fixed (0119) |
| RLS-3 | App role could rewrite `schema_migrations` | Fixed (0119) |
| RLS-5 | `oracle_mls_listings` has no RLS; entitlement is application-only (documented 0110) | **Open** — accepted design; entitlement filters verified on every read path |
| RLS-8 | Client email/phone and SMS bodies stored in plaintext (`encrypted_contact` populated in 0 rows) | **Open** — breach blast radius, see *Breach assumption* |
| DOS-1 | Image decoders had no pixel limit (140 KB PNG → 865 MB RSS) | Fixed — 50 MP ceiling for Pillow and OpenCV |
| WEB-2 | `/docs`, `/redoc`, `/openapi.json` public in production | Fixed |
| WEB-3 | The SPA on the DO static site ships no response headers (nginx.conf never applies) | Partly fixed — build-time `<meta>` CSP with hashed inline script; **frame-ancestors, HSTS, XFO for the SPA remain External** |
| WEB-5 | WebSocket `OBSERVE` wrote unbounded caller-keyed text into a global cross-tenant store; no frame limit or rate | Fixed — removed; 1 MiB frames; 10 frames/s per socket |
| AUD-1 | The audit middleware attributed only Bearer tokens; production browsers use the cookie → every browser mutation unattributed | Fixed |
| SUP-1 | Python dependencies not locked/hashed | **Open** — plan below |
| CI-1 | Environment protection rules not verifiable from the repo | **External** |

### Low / informational (abbreviated)

Fixed: AUTH-10 (invite links returned outside real dev), AUTH-13 (change-password
guessing), AI-4 (memory replay labelled as history), AI-5 (unoffered tools
executable), SSRF-1 (tenant SMTP host: `is_global`, fail closed, IP pinned),
UPL-3 (PDF magic), DOS-2 (MLS offset/list bounds, LIKE escaping), INFO-2
(`/\host`), WEB-6 (SameSite=Lax), WEB-7 (exact CSRF exemptions), WEB-8
(IPv4-mapped + IPv6 /64 rate identity), WEB-9 (non-object frames), WEB-10
(service-worker cache cleared at sign-out/expiry), RLS-4 (definer `pg_temp`,
TEMP revoked), RLS-6 (`media_blobs` UPDATE), TEN-4 (capability tokens out of
the audit ledger), CI-2 (actions pinned to SHAs), CI-3 (no secrets interpolated
into `run:`), INF-2 (`/portal` was not routed — client portal links 404'd), and a
functional 500 found by the fuzz (`POST /api/contracts/documents/{id}/signed`
failed on every call — untyped `jsonb_build_object` parameters).

Open: AUTH-11 (unverified signup can squat an address), HOOK-5 (Plivo V3 has
no nonce replay cache), HOOK-6 (legacy Twilio URL from forwarded headers when
the base URL is unset — not forgeable), TEN-7 (write checks accept shared
listings — integrity, not disclosure), TEN-9, SSRF-2 (provider-returned URLs
unbounded), INFO-1/INFO-3, SUP-2 (unchecksummed `ADD`), INF-1 (DO DB CA bundle
binding), OUT-2 (functional: command worker route lookup), AI-7 (missions
auto-release — feature off), AI-8 (internal map in a tool), RLS-7 (shared
caches writable by the app role).

## Tests executed and results

| Suite | Result |
|---|---|
| Backend (`pytest tests compliance_engine/tests`) | **2,745 passed, 0 failed** (after fixes; 58 new in `test_security_launch_review.py`) |
| Frontend (`vitest run`) | **458 passed, 0 failed**; production build OK |
| RLS on real PostgreSQL built from all migrations: `rls_security_review.sql`, `rls_brokerage.sql`, `platform_rls_test.sql` | **PASS, PASS, PASS** (also on the live dev DB) |
| `idor_sweep.py` (1,356 requests, A agent + A owner × B's IDs) | **0** responses containing B's sentinel; **0** mutations accepted (403 ×78, 404 ×525, 409 ×16; 422 ×259 inconclusive) |
| `session_attacks.py` | **20/20** |
| `web_attacks.py` | **53/53** |
| `ai_chain_attack.py` | **9/9** — both chains stop at the tool layer |
| gitleaks, 327 commits | **0** real secrets (5 reviewed false positives in `.gitleaksignore`); the only live credentials found are in gitignored local `.env` files |
| Bandit (medium+) | 31 results, all triaged non-issues → CI baseline; new findings fail CI |
| `pip-audit` / Trivy / SBOM (Mission 6) | unchanged, still gating releases |

**DAST.** OWASP ZAP was **not** run: there is no staging deployment, and the
local topology's network cannot reach the internet to fetch it. The scripted,
authenticated, business-aware DAST above covers what ZAP's passive and active
rules would on these paths; running ZAP baseline + an authenticated active scan
against staging is a launch blocker.

**API fuzz.** The IDOR sweep synthesizes schema-valid bodies for every mutating
route. Only one route returned 500 for client input (fixed); GovInfo routes
return deliberate 502/504 when the upstream is unreachable.

## Attack chains (item 143)

1. **Confused deputy:** agent A selected brokerage B's client as Neoh's
   context. Refused at record resolution (RLS); no B data in any frame.
2. **Indirect prompt injection with a hijacked model:** hostile text planted
   in one of A's own client records, and the provider mock answered with exactly
   the calls it demanded — rewrite the email to `attacker@evil.example`, read
   B's client, assign the client to B's owner, run an unoffered GPU tool, draft
   an email. Result: email unchanged, no cross-tenant read, no foreign
   assignment, no GPU approval, nothing sent, and the one staged draft targets
   the record's real address and waits for a human.

## Breach assumption — read access to a database backup

| Data | Exposed? |
|---|---|
| Passwords | No — scrypt hashes (N=2¹⁴; raising to 2¹⁷ with rehash-on-login is recommended) |
| Reset / invitation / portal / upload-link / OAuth-state tokens | No — SHA-256 digests only |
| OAuth refresh tokens, provider credentials, chat, transcripts, contracts, intake, caller phone | Only with `ORACLE_ENCRYPTION_MASTER_KEY`, which is never in the database or its backups |
| Client email and phone, SMS bodies, lead addresses and owners, audit metadata, MLS and public records | **Yes — plaintext** (RLS-8) |

## Compromise models

- **Malicious JavaScript in one user's browser:** limited to that session's
  role in its brokerage. Role, tenant, admin and billing are server-decided; the
  cookie is HttpOnly; the SPA now has a CSP.
- **Stolen agent credential:** that agent's brokerage-level access (the CRM is
  flat within a brokerage by product design) until suspended or until the
  password changes — both now end every session immediately.
- **Stolen broker-owner credential:** the brokerage, its billing and numbers;
  not other tenants, not platform admin (cannot assign it), not raw provider
  secrets.
- **Platform admin:** reads and changes across tenants through admin routes;
  audited; no raw secrets returned. Protected by one static passphrase without
  MFA — the largest single remaining account risk.

## MFA

Not supported for anyone. Recommendation: TOTP for the platform-admin login
before real brokerage data (it is one static credential with cross-tenant
power); for broker owners within the first month after launch; agents
optional. This review deliberately did not build MFA.

## Load after hardening (item 153)

The per-transaction session check costs ~1 ms in the database once warm (a
primary-key index scan, 0.29 ms). A direct comparison with Mission 8's
baseline was invalid — the host was saturated by an unrelated browser process
— so HEAD and the hardened tree were run interleaved, three rounds each, on the
same database (`read_load`, 25 VUs, 75 s; `performance/out/results/read_load-ab-*`):

| Endpoint class (median of per-tenant p95 medians) | HEAD | Hardened | Δ |
|---|---|---|---|
| `GET /api/command-center` | 216 ms | 236 ms | +9 % |
| `GET /api/crm/clients` (list) | 353 ms | 380 ms | +8 % |
| `GET /api/crm/clients/{id}` | 107 ms | 122 ms | +14 % |
| `GET /api/crm/clients?q` | 262 ms | 237 ms | −9 % |
| `GET /api/crm/contacts` | 75 ms | 67 ms | −10 % |
| `GET /api/mls/search` | 124 ms | 153 ms | +23 % |

All six runs PASS every threshold with 0 errors and an even replica split.
Deltas are mixed in sign and within this host's run-to-run spread, so there is
no significant regression; the worst-case reading is a ~1 ms-per-transaction
cost that only shows on routes opening many transactions. The absolute numbers
are higher than Mission 8's because the host was contended during BOTH arms —
re-measure on staging.

## Launch gate

`docs/security-launch-gate.json` is the machine-readable form. Every condition
in the mission's gate is met **except**:

1. **Staging DAST** — no staging app exists; nothing was run on DigitalOcean.
2. **RLS-1** — HIGH, needs a fix or an explicit, owned acceptance.
3. **External controls unverified** — GitHub environment protection rules
   (branches = main, required reviewers) on `production` and `staging`; DO
   database/Valkey trusted sources; Spaces bucket private.

## Blockers before taking real brokerage data

1. Create the DO staging app (Mission 6 spec), run ZAP baseline + an
   authenticated active scan (rate-bounded, test tenant only) and the four
   `performance/security/` scripts against it; fix anything launch-blocking.
2. Decide RLS-1: accept with an owner and date, or implement role-based admin.
3. Verify and screenshot the GitHub environment protection rules and DO trusted
   sources; confirm the Spaces bucket is private.
4. Platform-admin MFA (or, at minimum, move the operator into `users` so it
   can be suspended and its sessions ended like any account).
5. Serve the SPA with real response headers (nginx service component, or DO
   edge headers if available) so `frame-ancestors` and HSTS apply.
6. Rotate the local development Stripe key away from LIVE mode.

## Recommended next production-readiness task

Stand up DigitalOcean staging and run the launch gate there end to end —
deploy, DAST, these attack scripts, the capacity runs from Mission 8 — because
every remaining blocker is either "measure it on DO" or "verify a DO/GitHub
setting", and none can be closed from this machine.
