# 10 — Communications, calendars, showings and provider integrations

> **Scope:** Give each authorized agent truthful, consent-checked email, SMS, phone, calendar and showing workflows, including an email agent that reads the agent's mail and drafts replies for approval, with provider acceptance kept separate from delivery. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03, 04, 07, 08, 09 · **Unblocks:** 13, 14, 20 · **Ledger:** [L10](LEDGER.md#l10) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Commands and approvals: `backend/commands_api.py`, `backend/command_providers.py`, `backend/approval_service.py`, `backend/automation_jobs.py`, `backend/ai_tools_gated.py`
- Email: `backend/smtp_mailer.py`, `backend/email_outbox.py`, `backend/sales_api.py` (`_provider_snapshot`), `docs/email-dns-setup.md`
- SMS and numbers: `backend/messaging_api.py`, `backend/messaging_data.py`, `backend/messaging_provider.py`, `backend/outreach_compliance.py`
- Voice routing: `backend/telephony_api.py`, `backend/voice_provider.py`, `backend/inbound_voice.py`, `backend/lead_routing_api.py`, `backend/speed_to_lead.py`
- Frontend: `oracle-app/src/components/CommsTab.jsx`, `oracle-app/src/components/CommsComposer.jsx`, `oracle-app/src/components/ShowingLogger.jsx`, `oracle-app/src/components/ProviderDeliveryPage.jsx`
- Tests: `backend/tests/test_smtp_mailer.py`, `backend/tests/test_sales_stack.py`, `backend/tests/test_email_outbox.py`, `backend/tests/test_command_send_bookkeeping.py`
- Docs: `docs/provider-failure-matrix.md`; owner notes `shared-notes/neho-project-notes-2026-10-11.md` (§4, §5, §9–§11); audit finding [A07](../neoh-full-platform-source-audit-2026-10-08.md)

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Per-agent email connect is not on this branch.** It exists only as pending local commit `75c6ba6` (L10-01: provider presets, one-step connect, test send to the agent's own address).
- **The platform SMTP relay has never sent an approved email on staging (L10-02).** The send path is `send_smtp_email` (`command_providers.py:88`) over `smtp_mailer.py`.
- **A typed-in mail server never receives the platform login (L10-05).** Commit `155d474`: `resolve_settings` (`smtp_mailer.py:94`) returns no platform value once a tenant host is set (`:106`, inner `platform`).
- **Google is calendar-only.** `_GOOGLE_SCOPES` (`commands_api.py:367`) is `openid`, `email` and `calendar.events` (`:370`); the comment at `:365` says mail never leaves through Google.
- **Google connect is blocked on a missing OAuth client (L10-03).** `_google_oauth_settings` (`commands_api.py:379`) returns 503 while `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and the redirect URI are absent; staging has none.
- **The calendar can only insert events.** `create_google_calendar_event` (`command_providers.py:262`) is the only Google Calendar call; no free/busy query was found.
- **Nothing reads an inbox.** A grep of the backend for `imaplib`, `IMAP4`, `gmail.readonly` and `In-Reply-To` finds nothing. `email_outbox.py:1` describes a sender only (queued → sending → sent / failed).
- **Approval is bound to a payload hash.** `create_approval` hashes the draft (`approval_service.py:53`); `enqueue_job` refuses a job whose payload no longer matches (`automation_jobs.py:189`); `edit_command` revokes the old approval on edit (`commands_api.py:1741`, `:1764`).
- **The timeline says "sent" for a server acceptance.** `_SEND_ACTIVITY_SUMMARY` writes "Email sent (approved in Neoh)" (`commands_api.py:2376`); SMS says "accepted by the carrier".
- **The Email channel reads "Ready" from env vars alone.** `_provider_snapshot` (`sales_api.py:299`) counts `ORACLE_SMTP_FROM_EMAIL` or `ORACLE_SMTP_USERNAME` as an email channel (`:394`, `:405`). `ProviderDeliveryPage.jsx:427` shows "Ready" with the caption "SMTP or Google".
- **SMS waits on 10DLC registration (L10-04).** The brand and campaign are per brokerage (`get_business_registration`, `messaging_api.py:279`; brand `:305`, campaign `:373`, number assignment `:422`). The business number is per agent (`get_business_messaging_status`, `messaging_api.py:114`). Telnyx is the default provider (`messaging_provider_name`, `messaging_provider.py:93`).
- **Audit A07 is still present.** `ensure_messaging_route` (`messaging_data.py:96`) says an existing route is never changed, but its insert uses `ON CONFLICT … DO UPDATE SET provider_account_id` (`:108`).
- **Opt-out keywords and call hours exist.** `STOP_KEYWORDS` (`outreach_compliance.py:120`); calling window 08:00–20:00 (`CALL_WINDOW_OPEN`, `:100`).
- **Inbound calls resolve a route per brokerage and agent.** `resolve_inbound_route` (`inbound_voice.py:211`); forwarding target in `resolve_forward_target` (`:576`).

## 3. Goal

One broker and 20 agents can connect their own channels, see honest setup states, draft and approve messages, and see what the provider really did. The email agent reads the agent's own mail and thread history, drafts grounded replies, and sends only the exact version the agent approved. Consent, opt-out and carrier rules are never bypassed.

## 4. Engineering tasks

- **T10.01 Audit every provider integration.** Callbacks, signature checks, sender and number ownership, token encryption, registrations and health. Keep carrier-neutral adapters, and prove tenant isolation when brokerages share one platform provider account.
- **T10.02 One communication lifecycle.** Requested, drafted, staged, approved, accepted, in transit, delivered or connected, replied or completed, bounced or failed, delivery unknown, cancelled, expired. Map duplicate and out-of-order callbacks idempotently; never write "delivered" or "sent" from a 200 (fix `_SEND_ACTIVITY_SUMMARY`).
- **T10.03 Route inbound calls correctly.** Right brokerage and assigned agent, explicit failover, business hours, tenant preferences, verified inbound number, suspended agents and escalation. No caller-ID or number collision across brokerages.
- **T10.04 Harden SMS.** Verified number, 10DLC brand and campaign, STOP keywords, quiet hours, recipient checks, consent evidence and human approval. SMS stays off until registration is approved (L10-04).
- **T10.05 Harden email.** Verified sender and domain, SPF/DKIM/DMARC, a provider DPA, unsubscribe and bounce states, PII minimization, templates, correct reply threading, and no unchecked rewriting of recipients.
- **T10.06 Calendars and showings.** Free/busy, showing reservation, scoped permissions, time zones and DST, working hours, calendar choice per agent, double-booking protection, tentative vs confirmed, cancel and reschedule, the provider's event ID, and compensation on failure.
- **T10.07 A brokerage inbox that honors access.** Agents own their client threads, team leads see delegated team records, the owner sees diagnostics with controlled content. Search and AI summaries use the same rules.
- **T10.08 Reconcile timeline, ledger and billing.** Derive all three from one canonical event type. Usage counted at the staged step is audit A05, owned by 18; coordinate there.
- **T10.09 Truthful setup states.** Per-tenant capability probes, retries, circuit-breaker UI, and clear "requires setup" and "registration pending" states. Never "Ready" because an SDK is imported or an env var is set; drop "Google" from the email caption.
- **T10.10 Bound outbound campaigns.** Rate and budget limits, consent, personalization from verified CRM data only, agent or broker approval, delivery receipts, and quiet hours in the recipient's own time zone and law.
- **T10.11 Onboarding and offboarding.** Each agent gets a verified business number or approved routing identity; a departing agent keeps no brokerage numbers, threads or secrets.
- **T10.12 Provider-sandbox end-to-end harness.** Plus human-run test calls, texts, email and calendar events to the operator's own destinations, covering the cases in §5.
- **T10.13 Re-verify and fix A07.** Pick one invariant: first creation vs an authorized provider migration. Make creation atomic, never switch `provider_account_id` implicitly, and add a deterministic race test.
- **T10.14 Land per-agent email connect.** With owner push approval, deploy pending local commit `75c6ba6` (L10-01), then run one real app-password connect and test send to the agent's own address. Follow the provider notes in §7.
- **T10.15 Prove the platform relay.** Send one approved email to the operator's own address and record the server receipt (L10-02).
- **T10.16 Keep Google honest until configured.** Show a complete "setup required" flow while no OAuth client exists (L10-03); after the operator adds one, connect one agent calendar and keep the grant to `calendar.events`.
- **T10.17 Email agent: read incoming mail and thread history.** Add per-agent inbox ingestion (new; nothing reads mail today) with access rules, consent, minimal retention and encrypted storage (A01).
- **T10.18 Email agent: draft replies for approval.** Extract questions, concerns and commitments; retrieve only authorized CRM context; separate facts from assumptions; match the agent's tone. Send only through the hashed approval path, and require a new approval after any edit.
- **T10.19 Email agent: evaluation set.** Score drafts against the owner's checklist (shared notes §11): the real question answered, no invented facts, the agent's style, flags for missing information, no send without valid approval, accurate send results.

## 5. Failure and adversarial cases

- **F10.01 Accepted reported as delivered.** A 200 from SMTP or a carrier shows as "sent" or "delivered".
- **F10.02 Webhook replay or disorder.** A duplicate, replayed, unsigned or out-of-order callback changes state twice or backwards.
- **F10.03 Racing route creation (A07).** Two requests switch the provider account or overwrite another route.
- **F10.04 Consent ignored.** STOP, an opt-out or a consent change mid-job is not honored, or quiet hours use the wrong time zone.
- **F10.05 Credential or recipient leak.** A typed-in mail server receives the platform login again, or a recipient is rewritten without a check.
- **F10.06 False readiness.** A channel shows "Ready" or "connected" with nothing ever sent, or Google is offered as email.
- **F10.07 Calendar errors.** Double booking, an unavailable calendar, a token revoked mid-job, or a DST shift moves a showing.
- **F10.08 Wrong number or misroute.** A wrong number, a cross-brokerage number collision, or an inbound call routed to the wrong agent.
- **F10.09 Offboarded agent keeps access** to brokerage numbers, threads or secrets.
- **F10.10 Email agent oversteps.** It reads mail the agent may not see, invents facts or completed actions, or sends without approval or after an edit.
- **F10.11 Outage and false success.** Partial outage, credentials revoked mid-job, or runaway carrier cost reported as success.
- **F10.12 Inbox leak.** An agent or team lead sees threads outside their assignment or delegated scope.

## 6. Acceptance criteria

- **AC10.01 Every message shows its true stage.** Timeline, ledger and API show only the stage the provider confirmed, and duplicate or out-of-order callbacks are idempotent. Evidence: lifecycle state-machine tests plus webhook replay tests. Covers: T10.02, F10.01, F10.02.
- **AC10.02 A07 is closed.** Concurrent `ensure_messaging_route` calls never switch provider account or overwrite a route. Evidence: deterministic concurrency test on real PostgreSQL. Covers: T10.13, F10.03.
- **AC10.03 SMS is gated and compliant.** SMS stays off until 10DLC approval; STOP, consent and recipient-time-zone quiet hours block sends at execution. Evidence: compliance gate tests plus a sandbox send after registration. Covers: T10.04, T10.10, F10.04.
- **AC10.04 Email sends are safe and proven.** The L10-05 regression stays green, unsubscribe and bounce are recorded, and one approved relay send has a receipt. Evidence: `backend/tests/test_smtp_mailer.py`, bounce tests, and the L10-02 receipt. Covers: T10.05, T10.15, F10.05.
- **AC10.05 Setup states are truthful.** No channel reads "Ready" without a successful probe or send; Google shows "setup required" while unconfigured. Evidence: `_provider_snapshot` contract test plus a Playwright check of the delivery page. Covers: T10.01, T10.09, T10.16, F10.06.
- **AC10.06 Showings book once and correctly.** Free/busy, tentative vs confirmed, reschedule and cancel work across DST with the provider event ID stored. Evidence: calendar sandbox tests incl. double booking and token revocation. Covers: T10.06, F10.07.
- **AC10.07 Calls and numbers route to the right agent.** Inbound routing, failover and offboarding are correct, with no cross-brokerage collision. Evidence: routing tests incl. a suspended and an offboarded agent. Covers: T10.03, T10.11, F10.08, F10.09.
- **AC10.08 The inbox honors access.** Agent, team-lead and owner views match their grants, and search and AI summaries do too. Evidence: RLS tests under the application role, same-brokerage and cross-brokerage. Covers: T10.07, F10.12.
- **AC10.09 The email agent reads, drafts and waits.** It ingests only the agent's own authorized mail, drafts with cited context, and cannot send without a matching approval hash. Evidence: ingestion and approval tests plus a scored evaluation set of at least 20 threads (the owner's checklist). Covers: T10.17, T10.18, T10.19, F10.10.
- **AC10.10 Per-agent email connect is deployed and proven.** Evidence: the deploy SHA and one real connect plus test-send receipt to the agent's own address (L10-01). Covers: T10.14.
- **AC10.11 Outages and costs are reconciled.** Partial outages, revoked credentials and cost caps end in a true state, and the harness covers every §5 case. Evidence: provider-sandbox harness run with command and results. Covers: T10.08, T10.12, F10.11.

## 7. Out of scope, owner decisions, category-specific rules

- **Build order (owner, shared notes 2026-10-11):** calls (09) → SMS → email → follow-up.
- **Email provider notes (as of 2026-10-07):**
  - personal Outlook.com accounts cannot send over SMTP with a password since September 2024 (OAuth only);
  - Microsoft 365 SMTP AUTH basic auth stays available through December 2026, then is disabled by default.
- **Mail never goes through Google** while the grant is calendar-only. Adding a Gmail read scope for T10.17 is an owner decision (it widens the grant to the whole mailbox).
- **Owner questions:** which inbox access to build first for T10.17 (IMAP with app passwords, or provider OAuth)? Is a brokerage-wide inbox wanted, and how much content may the owner see?
- **Out of scope here:** the voice engine (09), usage billing (A05, 18), and follow-up workflows beyond drafting (later, per the build order).

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the lifecycle state machine and wording fix;
  - the A07 fix with its race test;
  - truthful setup states on the delivery page;
  - the email-agent design note (`docs/email-agent.md`, proposed) and evaluation set.
- **First slice:** T10.13 + AC10.02 (A07) and T10.09 + AC10.05, which need no provider. Then T10.14 and T10.15 once the owner approves a push.
- **Ledger:** update L10-01 to L10-05, then add L10-06 onward per slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
