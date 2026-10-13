# 17 — Security, privacy, compliance and data lifecycle

> **Scope:** Protect brokerage and customer data across every page, AI context, voice, media, backups and provider, close the plaintext-PII and log-leak findings, and turn the staging security gate into a production release gate. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 01, 03 · **Unblocks:** 18, 19, 20 · **Ledger:** [L17](LEDGER.md#l17) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Auth and tenancy: `backend/auth.py`, `backend/tenancy.py`, `backend/ai_tool_policy.py`, `backend/ai_chat_store.py`, `backend/db/migrations/`
- Privacy lifecycle: `backend/privacy_lifecycle.py`, `backend/privacy_requests.py`, `backend/privacy_data_map.py`, `scripts/reapply-erasures.py`, `scripts/privacy-orphan-audit.py`
- AI call notes (audit A01 widening): `backend/commands_api.py` (`deliver_call_notes`), `backend/elevenlabs_realtime.py`
- Browser storage: `oracle-app/src/lib/clearPrivateCaches.js`, `oracle-app/src/sw-oracle.js`
- Existing security tests: `backend/tests/rls_security_review.sql`, `backend/tests/rls_brokerage.sql`, `performance/security/idor_sweep.py`
- Docs: `docs/security-launch-gate.json`, `docs/security-launch-review.md`, `docs/security/`, `docs/privacy-data-map.md`, `docs/privacy-counsel-checklist.md`, `docs/support-model.md`, `docs/security-incident-response.md`, `docs/subprocessor-inventory.md`
- Audit findings [A01 and A06](../neoh-full-platform-source-audit-2026-10-08.md); ledger rows L17-01, L17-02, L09-02

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Client contact data is stored in plaintext.** `docs/privacy-data-map.md:40` (`clients`, unused `encrypted_contact`); `backend/privacy_data_map.py:58` entry `"clients"` reads "name/email/phone plaintext".
- **Lead payloads, SMS bodies and raw chat turns are plaintext too.** `privacy-data-map.md:42` (`leads.payload`), `:48` (`sms_messages`), `:71` (`user_interactions.content`); `privacy_data_map.py:100` (`sms_messages`) and `:135` (`user_interactions`) say the same.
- **AI call notes are written into a plain text column (A01 widened).** `commands_api.py:2335`, inside `deliver_call_notes` (`:2301`), inserts the notes into `client_notes.body`, a `text` column (`0019_client_enterprise.sql:51`). Added in `aec14c4` (ledger L09-02).
- **The data map does not flag client notes as sensitive text.** `privacy_data_map.py:65` classifies `client_notes` as `BUSINESS_CRM` with no plaintext note.
- **A failed property lookup logs the client's full name (A06).** `client_ai_automation.py:701-703`, in `_property_candidates_bounded` (`:680`), logs `full_name` with `%r`.
- **The Python read check for clients and leads is tenant-wide.** `tenancy.py:113` `can_read_private` ("hard wall, no sharing") compares only `tenant_id`; the 0001 policy `clients_tenant_isolation` (`0001_init_tenancy.sql:135`) does the same.
- **Logout revokes every session of the account.** `auth.py:756` `_end_account_sessions` bumps `session_epoch`; `auth.py:828` (`_session_still_current`) and `tenancy.py:351` (`verify_session_current`) re-check it.
- **Login is rate limited and operators need a second factor.** `auth.py:548` `_check_rate_limit`; `auth.py:210` `_operator_second_factor` and `:242` `_require_operator_email_otp`.
- **Sign-out clears browser copies.** `clearPrivateCaches.js:13` `PRIVATE_LOCAL_KEYS`; `clearPrivateCaches` (`:15`) also deletes the IndexedDB store named at `:7`.
- **The security gate passed on staging only.** `docs/security-launch-gate.json:7` `"verdict": "PASS"`; `:112` says writes and paid routes were not actively scanned on staging; `:131` lists production-only secrets still to set. Ledger L17-01: ZAP active scan 0 High, PASS on staging (`0739563`).
- **Staging databases accept only the app.** PostgreSQL and Valkey trust only the App Platform app; operators use `scripts/db-access-window.sh` (`open|close|status`, `:12-14`); migrations run as a PRE_DEPLOY job. Ledger L17-02 (`b6d1160`).
- **Erasures are re-applied after a restore.** `privacy-data-map.md:134` describes `scripts/reapply-erasures.py`; the engine is `privacy_lifecycle.py:801` `run_erasure`, with caches in `_phase_caches` (`:1121`).
- **Voice AI audio goes to an international region by default.** `privacy-data-map.md:125` (DashScope, Singapore by default); `qwen_omni_realtime.py:73` defaults `DASHSCOPE_REGION` to `intl`.
- **The support address is still a placeholder.** `docs/support-model.md:11` (`<<SUPPORT_EMAIL — owner to decide…>>`).

## 3. Goal

A brokerage owner and 20 agents can use every page knowing that an agent sees only what is assigned or shared, that customer data is minimized and encrypted where it must be, that deletion reaches every copy, and that no log, cache or model answer leaks it. The release gate rests on production evidence and human sign-offs, never on a staging pass alone.

## 4. Engineering tasks

- **T17.01 Re-check the security baseline.** Re-read `docs/security-launch-review.md` and the gate at HEAD, list what changed since `0739563`, and keep the verified fixes (session revocation, signed webhooks, RLS, role changes, operator MFA, recovery and egress guards). Re-run the suites after any ACL or schema change.
- **T17.02 Verify whether any migration after 0001 narrows `clients`, `leads` or `client_notes` to the assigned agent.** Then enforce same-brokerage, cross-agent isolation (C3.5) on every dependent table, export, search, AI tool, media link, background job and privileged function.
  - An unauthorized request must not reveal that a record exists.
- **T17.03 Inventory every sensitive field.** Start from `docs/privacy-data-map.md` §7 and add `client_notes.body` (AI call notes), transcripts and model outputs. Record copies in memory, storage, logs, caches and provider systems.
- **T17.04 Encrypt sensitive columns in stages.** Use field-level encryption with HMAC lookup where search needs it, dual read/write during the transition, a bounded backfill of old rows, validation, restore compatibility and erasure. Start with `clients`, `leads.payload`, `sms_messages.body` and `client_notes.body` (A01).
- **T17.05 Invalidate browser and derived copies on change.** Clear service-worker IndexedDB, browser storage, prefetch caches, signed links and AI summaries on session change, agent reassignment, record deletion and revocation, within a bounded time.
  - Public capability links get an expiry and are bound to an audience and purpose.
- **T17.06 Harden authentication and sessions.** Cover login rate limits, token freshness, MFA for broker owners where required, privilege-elevation audit, single-use expiring invitations, password reset, policy acceptance and fresh permission state on WebSockets. Operator-only functions are never reachable through a broker-owner route.
- **T17.07 Prepare the privacy and legal gate for counsel.** List the questions: brokerage controller vs Neoh processor terms, DPA and subprocessor list, marketing consent, TCPA and internal DNC, recording disclosure, cross-border model processing (DashScope Singapore default), training opt-outs and retention schedules. No pretend legal sign-off.
- **T17.08 Cover the whole data-subject lifecycle.** Access, export, deletion, offboarding and legal hold must reach user attribution, caches, embeddings, real-time agent context, backups after restore, orphaned reconstruction files and provider copies. Test erasure re-application after a restore in a safe environment.
- **T17.09 Secure third-party and AI use.** Scope and encrypt keys and secrets, keep PII out of prompts and logs, record model and training-data licence provenance, and hold prompt-injection boundaries so no unauthorized call, email or financial action can run. Write provider breach and outage runbooks with incident severity.
- **T17.10 Close the API and infrastructure checks.** Cover CSRF, CORS/CSP, request limits, SSRF, webhooks, signed uploads, SQL injection, MIME validation and malware scanning, WebSocket auth, rate limits and budgets, dependencies, SBOM, SAST, and deploy secret gates.
  - Run an authenticated OWASP dynamic scan that includes write routes, in a safe environment, and compare it with the known accepted residuals.
- **T17.11 Build the role-by-resource security matrix.** Test 20 accounts (assigned, shared, team manager, owner, suspended) plus a second brokerage, including concurrency, SQL-level claims, cache leaks, stale sockets, tool execution and signed media.
- **T17.12 Write the privacy and incident readiness report.** Label every legal or external sign-off as human-owned. A risk acceptance names its signatory and its impact.
- **T17.13 Set up owner and operator workflows.** Support channel, urgent route, incident notification, revocation and offboarding, consent explanation, billing disputes and data export. Replace the placeholder support address (owner decision).
- **T17.14 Remove PII from logs (A06).** Log stable IDs and outcome codes instead of names in `_property_candidates_bounded`, review neighbouring warning and error paths, and close the log-line gap at `privacy-data-map.md:148`.

## 5. Failure and adversarial cases

- **F17.01 Same-brokerage leak.** An agent reads another agent's unshared client through the API, search, export, an AI tool, a job or a signed media link.
- **F17.02 Existence leak.** A denied request returns a different status or message for a record that exists than for one that does not.
- **F17.03 New plaintext after migration.** An AI call note, SMS or chat turn is written in plaintext after the encrypted path is live.
- **F17.04 Interrupted backfill.** The encryption backfill stops mid-batch, and dual read returns wrong or missing values, or a rollback loses data.
- **F17.05 Stale device copy.** After sign-out or reassignment, the next user of a shared device reads cached media, legal payloads or identity.
- **F17.06 Stale socket.** A suspended or demoted user keeps receiving WebSocket data.
- **F17.07 Restore resurrects data.** A backup restore brings back erased records, and the erasure directives are not re-applied before traffic resumes.
- **F17.08 PII in logs.** A failure path logs a name, phone number, message body or another tenant's identifier.
- **F17.09 Prompt injection.** Text in a note, email or web page makes the AI call a tool the user did not authorize.
- **F17.10 Reused capability link.** A public link works after expiry or for a purpose or audience it was not issued for.
- **F17.11 Operator route reached by an owner.** A broker owner reaches an operator-only function.
- **F17.12 Unsigned legal claim.** A report states a legal or compliance approval that no named human signed.

## 6. Acceptance criteria

- **AC17.01 Cross-agent and cross-brokerage access is denied everywhere.** Both directions are denied for every resource in the matrix, and denials do not reveal existence. Evidence: real-PostgreSQL test as the application role, `backend/tests/test_within_brokerage_isolation.py` (proposed), plus the matrix report. Covers: T17.02, T17.11, F17.01, F17.02.
- **AC17.02 No new plaintext rows; old rows migrate.** Sensitive write paths create no plaintext, the backfill is resumable, and search still works. Evidence: migration test that interrupts and resumes the backfill, and a column scan before and after. Covers: T17.03, T17.04, F17.03, F17.04.
- **AC17.03 Device and socket copies are revoked.** After sign-out, reassignment or suspension, caches are empty and the socket closes or re-checks permission. Evidence: Playwright test on a shared-device profile; WebSocket test with a demoted user. Covers: T17.05, T17.06, F17.05, F17.06, F17.10.
- **AC17.04 Erasure reaches every copy, including after restore.** Evidence: `backend/tests/test_privacy_lifecycle_live.py` extended with a simulated restore and the derived copies from T17.08. Covers: T17.08, F17.07.
- **AC17.05 Logs carry no PII.** Evidence: failure-path test plus a representative log sample with no names, phones or bodies. Covers: T17.14, T17.09, F17.08.
- **AC17.06 Injection cannot reach a tool.** Evidence: evaluation set of injected notes, emails and pages, run and scored against the tool policy. Covers: T17.09, F17.09.
- **AC17.07 Operator functions stay operator-only.** Evidence: route test as broker owner for every operator route. Covers: T17.06, F17.11.
- **AC17.08 Zero known unresolved critical or high access issues.** The authenticated scan, including write routes, has each finding fixed or justified against the accepted residuals. Evidence: committed scan report under `docs/security/` and the re-run gate. Covers: T17.01, T17.10.
- **AC17.09 Legal and processor decisions are documented and signed.** Evidence: counsel checklist with named signatories and dates. Covers: T17.07, T17.12, F17.12.
- **AC17.10 Each plaintext field is remediated or formally accepted.** Each A01 field is encrypted or has a signed risk acceptance with impact. Evidence: the report from T17.12. Covers: T17.04, T17.12.
- **AC17.11 Support and incident routing reach a person.** Evidence: a test incident routed end to end to a named responder. Covers: T17.13.
- **AC17.12 The report never says "guaranteed secure".** It states staging vs production evidence (C6). Evidence: review of the T17.12 report. Covers: T17.12.

## 7. Out of scope, owner decisions, category-specific rules

- **Production evidence belongs to 19.** Production secrets, production trusted sources and backup restore on production are checked in 19; this category supplies the security checks they run.
- **Legal decisions are human-owned.** Claude prepares questions and evidence; counsel and the owner decide. Never write that a legal review happened.
- **Do not weaken a passing control to make a test pass.** A failing security test is a defect to fix, not a test to loosen.
- **Cross-border processing** (DashScope default region) needs an owner decision before production; record it as an owner question.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the role-by-resource security matrix (`docs/security/access-matrix.md`, proposed);
  - the sensitive-field inventory and encryption plan;
  - the privacy and incident readiness report;
  - tests for each AC above.
- **First slice:** T17.14 + AC17.05, which closes A06 with a small change. Then T17.02 + AC17.01, which proves within-brokerage isolation.
- **Ledger:** update L17-01 and L17-02, then add L17-03 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
