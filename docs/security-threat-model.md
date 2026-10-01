# Neoh threat model

Written for the security launch review of 2026-10-01 (HEAD `0f0b851`,
migration head `0116` at the start; `0119` after the review's fixes). Findings
and evidence: `docs/security-launch-review.md`. Reviewed against OWASP
Top 10:2025, ASVS 5.0.0, API Security Top 10 (2023), WSTG 4.2 and the OWASP Top
10 for LLM Applications 2025 — applied to this architecture, not as a checklist.

## The system in one paragraph

A React SPA and a FastAPI API share one origin on DigitalOcean App Platform
(`api` service ×2+, `worker` ×1, `web` static site). PostgreSQL 16 (Managed)
holds every tenant's data behind row-level security; the app connects as
`oracle_app_login` (no superuser, no BYPASSRLS, not a table owner). Valkey
holds rate-limit windows and caches. Spaces holds media and documents.
Plivo/Telnyx/Twilio carry voice and SMS; Stripe carries billing; SMTP carries
mail; Fireworks (via LiteLLM) is the model provider; RunPod runs GPU
reconstruction. Neoh, the AI agent, acts through a tool layer that runs with the
signed-in user's tenant context.

## Crown jewels — what must never cross a boundary

| Asset | Where it lives | Boundary that protects it |
|---|---|---|
| Tenant customer data (clients, leads, deals, notes, tasks) | Postgres, FORCE RLS on all 129 tenant tables | RLS keyed on server-set GUCs from a verified session |
| Contracts and documents | `contract_documents.content_ciphertext`, Spaces | RLS + per-tenant pgcrypto key + authenticated download routes |
| Call/SMS content and recordings | `inbound_voice_calls.*_ciphertext`, `sms_messages.body` (plaintext) | RLS; transcripts encrypted, SMS bodies are not |
| Provider credentials, OAuth tokens | `provider_credentials.*_ciphertext` | per-tenant key derived from `ORACLE_ENCRYPTION_MASTER_KEY`; never returned by any API |
| `ORACLE_SECRET_KEY` (JWT) and `ORACLE_ENCRYPTION_MASTER_KEY` | App Platform SECRET env only | not in the database, not in images, not in backups |
| Billing state and entitlement | `subscriptions`, Stripe | server-side entitlement (`require_active_subscription`), signed + replay-proof webhooks |
| MLS licensed data | `oracle_mls_listings` (no RLS, by design) | entitlement filter on every MLS read path (application) |
| Private property media | Spaces, `media_blobs` | server-generated keys, authenticated/capability-token serving, sniffed types |
| AI memory and chat history | `ai_chat_messages` (encrypted), memory tables (FORCE RLS) | RLS by tenant AND agent |
| Audit trail integrity | `audit_ledger` | app role INSERT/SELECT only; insert policy tenant-scoped (0119) |
| Admin capability | env operator identity + `platform_admin` role | role from a verified session, proved against the DB on admin gates |
| Outbound communications | provider accounts | approval-gated commands, `guard_outreach` at execute time, recovery-mode egress guard |

## Actors

| Actor | Can do legitimately | Must never be able to |
|---|---|---|
| Anonymous internet user | sign up, sign in, request a reset, use a capability link | read tenant data, exhaust a replica, forge webhooks |
| Legitimate agent | work their brokerage's CRM, chat with Neoh, request outreach | act as owner/admin, read another brokerage, decide others' approvals |
| Broker owner | manage team, billing, numbers, approvals | act as platform admin, reach another brokerage |
| Platform admin (operator) | cross-tenant operations through admin routes | read raw secrets through any API |
| Invited-not-yet-registered user | accept their invitation once | choose their role or tenant, accept twice, take another tenant's account |
| Malicious tenant user | everything their role allows | everything outside it — this actor is the review's default attacker |
| Compromised tenant account | that user's capabilities until revoked | survive a password change, reset, suspension or demotion |
| Malicious client with a portal link | view their own portal scope | other clients, other tenants, write anything durable |
| External webhook sender | nothing without the provider's signature | create billing/messaging/call state |
| Compromised provider credential | act on that provider account | reach Neoh's data plane |
| Insider / operator | run admin tooling | silently rewrite the audit trail |
| Attacker with a leaked DB backup | read what is stored in plaintext | passwords, live tokens, credentials, encrypted PII (needs the master key) |
| Attacker with a leaked object-storage URL | the one object until expiry | enumerate or overwrite other objects |
| AI prompt-injection source (MLS remarks, notes, email, PDFs, web, portal input, phone callers) | have their text read by Neoh | cause any action the signed-in user did not approve |

## Trust boundaries

1. **Browser → API.** Session = HttpOnly `oracle_session` cookie (SameSite=Lax,
   Secure), CSRF double-submit on every mutation, exact-origin CORS. The API
   trusts nothing from the body for identity: tenant, agent and role come from
   the verified token, and since 0117 the token is re-proved against the account
   row on every tenant transaction.
2. **API → PostgreSQL.** `tenant_tx` sets `app.current_tenant/role/agent`
   transaction-locally through `app_begin_session`; RLS does the rest. This
   boundary assumes no SQL injection — a query running as the app role can set
   its own GUCs (finding RLS-1, accepted with compensating controls).
3. **Provider → API (webhooks).** Each provider's signature over the raw body,
   checked before any durable effect; Stripe and Telnyx carry replay windows;
   Stripe events are deduplicated by id in the same transaction as their effect.
4. **Public capability links → API.** Hashed, expiring, revocable tokens bound
   to one tenant and resource; the body of an upload is not read until the
   token is proved.
5. **Model → tool layer.** The model proposes; the executor authorizes. Only
   offered tools run, identity comes from the server context, contact fields
   are not model-writable, side effects stage approvals that only the requester
   or a broker owner can release.
6. **API → outside world (SSRF).** Tenant-influenced URLs: the SMTP host only
   (global-address check, IP pinned). Everything else fetched is a fixed
   provider endpoint or a provider-returned URL.
7. **CI/CD → production.** Fork PRs run with no secrets; deploy jobs gated on
   `main` + GitHub environments (protection rules are an operator setting).

## Entry points

418 routes (417 HTTP + WebSockets), of which 52 have no session dependency:
12 auth bootstrap, 4 health/static, 20 signed webhooks, 1 OAuth callback,
8 capability-token routes and 7 WebSockets that authenticate inside the
handler. Inventory: `docs/security-launch-review.md` §Attack surface.

## Privileged operations

Role changes (two-person, `admin_ops`), team suspension (owner; owner-of-owner
needs platform admin), MLS entitlement grants (platform admin), provider
configuration (broker owner), number provisioning and 10DLC registration
(broker owner + live subscription), outbound release (requester or owner +
live subscription), FINANCIAL/LEGAL approvals (broker owner), migrations (CI
job with the DB admin secret, production environment).

## Assumptions this model depends on

- GitHub environment protection rules (main only, required reviewers) are set
  on `production` and `staging` — not verifiable from the repository.
- DO Managed Postgres/Valkey trusted sources restrict access to the app.
- The Spaces bucket is private (code never sets public-read).
- The DO edge appends the client address as the rightmost public
  `X-Forwarded-For` hop (to verify on staging).
