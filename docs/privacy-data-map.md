# Privacy data map

Where customer data lives in Neoh, what protects it, and what happens to it
when a person, an agent or a whole brokerage leaves. Snapshot of 2026-10-01,
taken from the live schema (158 tables, migrations 0001–0122), the object
store code and the frontend.

**The machine-readable version is `backend/privacy_data_map.py`.** It
classifies every table a migration creates (retention category plus what
erasure does to it), and the erasure engine executes it. The test
`tests/test_privacy_data_map.py` fails when a migration adds a table the map
does not name. This document is the narrative: the shape, the risks and the
reasons.

Roles: for its customers' clients and CRM, **the brokerage is the controller
and Neoh is the processor**. Neoh is the controller only of its own
relationship with the brokerage: the accounts, billing and security logs.

## 1. Stores

| Store | What is in it | Customer data? | Deleted by |
|---|---|---|---|
| PostgreSQL (DO Managed, nyc) | Everything in §2 | yes | erasure engine (`privacy_lifecycle.py`), subject deletion (`privacy_requests.py`), retention sweeps (0122) |
| Managed DB backups | 7-day point-in-time copies | yes | expiry only (cannot be edited) — see §6 |
| Object storage (Spaces nyc3 / local `ORACLE_MEDIA_ROOT`) | photos, video, 3D splats, LOA/invoice PDFs, exports | yes | erasure by key and by tenant prefix (`object_storage.delete_object/delete_prefix`, new 2026-10-01) |
| Contract vault bucket (`CONTRACT_VAULT_BUCKET`, SSE-S3) | contract PDFs | yes | erasure by recorded key |
| Valkey / Redis (optional) | rate-limit windows, AI-chat concurrency keys, live-call state | identifiers only, ≤1 h TTL | TTL; erasure scans `ai-chat:*{tenant}*` |
| SQLite audit fallback (`ORACLE_AUDIT_SQLITE`) | copy of audit rows when Postgres is down | emails, IPs | drained into Postgres and deleted once it answers again (boot and first successful write); capped at `ORACLE_AUDIT_SQLITE_MAX_ROWS`; rows older than `ORACLE_AUDIT_SQLITE_MAX_AGE_DAYS` (30) purged with an alert (L17-12) |
| Security alert email (`ORACLE_SECURITY_ALERT_EMAIL`, else `ORACLE_ALERT_EMAIL`) | one message per privileged sign-in, repeated failure, role change, new broker owner or operator second-factor change (`backend/security_alerts.py`) | the account id (an email) and the client IP address, with the UTC time; never a passphrase, code or token | the operator's mailbox retention; Neoh keeps no copy (the log line names the actor by `actor_ref()` and has no IP) (L17-09) |
| Browser localStorage / sessionStorage / IndexedDB | signed-in identity, comms templates, cached splats & legal payloads | yes, on the device | sign-out (`clearPrivateCaches.js`; identity + templates cleared since 2026-10-01) |
| Provider systems | calls, messages, recordings, payments | yes | §5 and `docs/subprocessor-inventory.md` |

## 2. Database, by category

Counts are from the dev database. They show shape, not production volume.

**Account** (Neoh is controller): `users` (email, name, password hash), `user_profiles` (phone, licence number, bio, LLM-written profile summary — sealed in `profile_summary_ciphertext` for every app write since 0154, L17-22), `team_memberships`, `agent_licenses`, `agent_ce_log`, `agent_ai_settings`, `autonomy_preferences`, `agent_routing_state`, `brokerage_invitations`, `user_policy_acceptances`, `account_security_acceptances`.
`tenants.postal_address` (0146) is the brokerage's mailing address, printed in the footer of every commercial email it sends (CAN-SPAM). Plaintext because it is published by design; for an independent agent it can be a home address, so erasure scrubs it with the name and website.
**`agent_id` is the user's email address**, so every text actor column (`created_by`, `author_id`, `assignee_id`, `requested_by`, …) is personal data.

**Contact PII**:
- `clients` holds name, email, phone and notes **in plaintext**. Its `encrypted_contact` column exists but is unused.
- `client_contact_index` (0156) holds, per client, HMAC-SHA-256 lookup hashes of the normalised email and phone and keyed word-prefix name tokens, under the tenant key (the `agent_contacts` scheme). No plaintext; not reversible without the master key. Filled by `client_contact_index.py` (periodic, internal-only); goes with its client. Nothing searches it yet (L17-23).
- `agent_contacts` keeps PII encrypted with pgcrypto under a per-tenant key, with HMAC lookup hashes. `assigned_user_id` (0134) is the canonical assignee (a `users.id`, no PII); who may read a contact follows it, the brokerage pool and `record_shares` rows of kind `contact` (D-2026-10-13-01).
- `leads` has 10.4M rows in dev. `payload` holds owner names **in plaintext**, and `encrypted_payload` is unused.
- Also in this category: `transaction_parties` (encrypted), `contact_property_relationships`, `buyer_profiles` and `lead_intake_events` (encrypted payload).

**Business CRM**: notes (`client_notes.body` is communication content — AI call notes are what a customer said on a call; since 0132 every app write seals it in `body_ciphertext` under the tenant key, and pre-0132 rows stay plaintext until `backend/backfill_client_notes.py` runs), tasks, activities, tags, segments, showings, listings, transactions, offers, milestones, checklists, buyer requests, marketplace, smart plans (revisions immutable), missions, hyperlocal sites, studio campaigns.
- `property_match_decisions` (0168, T11.03): the append-only record of a broker owner confirming, rejecting or withdrawing that a public property record is a linked client's house — ids, the decision, evidence kinds and timestamps. The decider's reason may name an owner, so it is stored only sealed in `reason_ciphertext` under the key of the lead's own tenant; there is no plaintext column for it and no fingerprint of the record's address or owner. `decided_by` is the decider's login (an email), stamped by trigger from the session. The reason never enters audit metadata; the brokerage export decrypts it, and the rows go with their lead, client or tenant.
- `hyperlocal_site_events` (0173, T14.04): the append-only trail of a site's publish, rollback and archive moves (status, from and to revision) and of its custom-domain claim's states, written by trigger. `actor` is the acting login (an email) from the session, `actor_role` its role; a domain row carries the claimed hostname and the DNS check's evidence (record name, answer counts, verdicts, the matched value's hash — never another TXT value); a site row may carry the approval id and its reviewer's login. Erased with the site or the brokerage. `hyperlocal_site_domains` gained `challenge_token` (a random value the customer publishes in DNS, not a secret), `last_checked_at` and `last_check` (the same evidence shape).

**Communication content**:
- `sms_messages`: numbers **in plaintext**. Since 0152 (L17-21) every app write seals the body in `body_ciphertext` under the tenant key and leaves `body` NULL; pre-0152 bodies stay plaintext until `backend/backfill_sealed_text.py` runs.
- `email_outbox`: recipients **in plaintext**. Since 0154 (L17-22) every app write seals the subject and body in `subject_ciphertext` / `body_text_ciphertext` (the plaintext columns hold `''`); `body_html` has no writer and must stay NULL on a sealed row. Pre-0154 rows stay plaintext until `backend/backfill_sealed_text.py` (or the periodic `sealed_text_seal` task) seals them. The send worker opens them at send time and logs neither.
- `interaction_logs`: since 0154 the subject (`subject_ciphertext`) and the payload's text keys `body`, `text`, `transcript`, `summary` and `extraction` (`content_ciphertext`, one JSON object) are sealed for every app write; the behavioural keys (listing ids, link kinds, delivery status) stay plaintext so counts and filters keep working. Pre-0154 rows plaintext until the backfill.
- `inbound_voice_calls`: caller, transcript, summary and intake answers, all encrypted.
- `contact_intake_sessions` (encrypted).
- `negotiation_events.transcript_excerpt`: sealed in `transcript_ciphertext` for every app write since 0152; pre-0152 excerpts plaintext until the backfill. Retention (`purge_expired_platform_data`) clears both columns.
- `command_executions.draft`: outbound drafts (and the same draft inside `action_approvals.draft_payload`). Since 0142 both rows carry `client_id`/`contact_id` links (no PII) and are visible only to those who can see that client or contact.

**Communication metadata**:
- `telephony_routes`: numbers, including the agent's personal hand-off phone.
- `messaging_routes`.
- `tenant_messaging_brands`: EIN and address for 10DLC.
- `live_call_sessions`: recording-consent flags.
- `agent_call_intents`, `contact_nurture_jobs`, `lead_response_events`.

**Consent / suppression**:
- `outreach_consent`: proof text and IP, plaintext contact.
- `outreach_suppression`, `outreach_attempt_log`. An email unsubscribe (the footer link or RFC 8058 one-click, `email_unsubscribe_api.py`) adds an `outreach_suppression` row (channel `email`). The link itself carries no readable personal data: the token is an AES-GCM ciphertext of brokerage + address (`commercial_email.py`), and it is masked in access logs.
- `suppression_tombstones` (new): keyed hash only.

**AI**:
- `ai_chat_messages` (encrypted). `metadata.semantic_frame` (prompt 05, plaintext jsonb on the assistant row): goal labels, authorised record ids, stated constraint kinds and times, and language codes (`lang` en/es, `detected`, the kinds the deterministic backstop added; L05-04) — never the person's words, names or addresses. It is the discourse context the next turn reads (constraints apply for 30 minutes) and is erased with the message.
- `missions.constraints.semantic_frame` (prompt 05): the same machine-readable constraints parsed from the objective, kept for the mission's life.
- `ai_tool_operations.result`: tool outputs (receipts can carry CRM values), sealed in `result_ciphertext` for every app write since 0154; pre-0154 receipts plaintext until the backfill.
- `user_interactions.content`: raw chat turns, sealed in `content_ciphertext` for every app write since 0152 (content `''`); pre-0152 turns plaintext until the backfill. Text `tenant_id`, no FK.
- `beliefs.source_quote` (sealed in `source_quote_ciphertext` for every app write since 0154; pre-0154 quotes plaintext until the backfill), `client_ai_state` (summaries), `agent_decisions`, `ai_decision_traces`.
- `style_training_examples`: redacted.
- Model registry, training runs and evaluations.

**Derived intelligence** (property signals, scores, zoning and title analyses):
- `title_findings`: `notes` is the agent's working note **in plaintext** (often a third party's court record) and never reaches the client portal. The reviewer's reason for confirming or dismissing a finding (T11.03) may name an owner, so it is stored only sealed in `review_reason_ciphertext` under the key of the finding's own tenant (0167); there is no plaintext column for it. `reviewed_by` is the reviewer's email. The reason never enters audit metadata; the brokerage export decrypts it, and erasure deletes it with the row.

**Documents and media**:
- Contracts, drafts and synthesis artifacts (encrypted content plus a vault object).
- `ai_record_attachments` (encrypted bytes).
- `messaging_hosted_documents`: LOA and invoice. The object is **not** app-encrypted.
- `property_media` and `media_blobs`, the latter raw bytes with no RLS and no `tenant_id`, removed through `property_media`.
- Pano scenes, floorplans, upload links, capture sessions, reconstruction jobs, tour variants and video jobs.
- `voice_walkthrough_jobs.audio`, which is nulled when the job finishes.

**Audit / security** (Neoh is controller): `audit_ledger` (hash-chained; email, IP, path), `audit_anomaly_alerts` (actor, source IP), `audit_chain_checkpoints` (new), `api_rate_limit_windows` (unsalted SHA-256 of IP; minutes), `process_heartbeats`.

**Secrets / capability tokens**: `provider_credentials` (encrypted Google/SMTP tokens), `lead_source_connectors` (encrypted webhook secret), `oauth_authorization_states`, `password_reset_tokens`, `client_portals`, `property_view_upload_links`, and invitations (all hashed tokens). Two-step sign-in (0150, L17-19): `user_mfa_factors.totp_secret_ciphertext` is the authenticator secret sealed with the brokerage key (pgcrypto, per-tenant HKDF, never plaintext); `user_mfa_recovery_codes.code_hash` is the SHA-256 of each one-time recovery code. Both tables admit only the platform context (RLS), are erased with the brokerage (and with the user, by cascade) and are never exported.

**Billing** (Neoh is controller): `subscriptions` (Stripe ids), `billing_usage_events`, `stripe_webhook_events` (event ids only), `billing_checkout_sessions` (0169: the brokerage's open Stripe Checkout session id and payable link, erased with the brokerage).

**Spend controls** (0170, T18.07; operational, no customer content): `tenant_action_budgets` holds the missions cents cap and the AI token budget (monthly cap, warning percent, paused); `updated_by` is the setter's sign-in email; erased with the brokerage. `ai_token_reservations` holds the token estimate of each hosted model call in flight (a call key made of the feature kind — `chat`, `voice`, `mission`, `client_ai`, `parse`, `cma` and so on — with the chat message id or a random id per unit of work, the provider and a number; never a phone number, a call id or any prompt or answer text; the same key, with `:prompt`/`:completion`, is the `billing_usage_events` idempotency key), lapses within minutes and is erased with the brokerage. `platform_spend_controls` is the platform kill switch (one row per channel, no tenant); `reason` is a platform admin's free text and `updated_by` their sign-in id. Audit entries for budget changes carry the numbers and switches only, never the switch's reason.

**Public / licensed / reference** (not brokerage data):
- `public_property_records`: 9.8M rows of owner names from public records, with a trigram search index.
- `oracle_mls_listings`: licensed feed, no RLS, entitlement enforced in the application.
- `di_cache`: shared vendor responses with TTLs.
- State and FEMA reference tables.

**Privacy records** (new, survive erasure by design): `privacy_operations`, `erasure_ledger`, `legal_holds`.

## 3. Object keys

| Prefix | Tenant in key | Recorded by | Erasure |
|---|---|---|---|
| `property-media/{tenant}/…`, `property-view/{tenant}/…`, `video-studio/{tenant}/…`, `splats/{tenant}/…` | yes | `property_media.s3_key` | by key, then prefix |
| `tenants/{tenant}/contracts/…` | yes | `contract_synthesis_artifacts.s3_key` | vault, by key |
| `clients/{client}/contracts/{doc}.pdf` | **no** | `contract_documents.s3_key` | vault, by key (collected before rows go) |
| `messaging-hosted-documents/{tenant}/…` | yes | `messaging_hosted_documents.storage_key` | by key, then prefix |
| `recon-inputs/{uuid}/…`, `recon-outputs/{uuid}/…` | **no** | **nothing** | not reachable per tenant — `scripts/privacy-orphan-audit.py` finds them |
| `privacy/exports/{tenant}/{op}.zip` | yes | `privacy_operations.artifact_key` | 7-day expiry; prefix at erasure |
| `privacy/erasure-directives/{op}.json` | tenant id only | — | kept: the restore re-application record |

## 4. Caches and browser

- **Redis keys**:
  - `ai-chat:*:{tenant}:{agent_id}` holds a plaintext email, 1 h TTL.
  - `twilio|plivo|acs:call_state:*` holds the callee or caller phone, 1 h TTL.
  - `rate:*` holds a raw IP for a minutes-long window.
  - `di:*` holds vendor responses (may include owner names), not tenant-keyed, with a per-source TTL.
- **localStorage**:
  - `oracle_user_id` (an email), `oracle_tenant_id` and `oracle_comms_templates_v1` are cleared at sign-out since 2026-10-01.
  - `neoh.theme` and the tour flag are kept.
- **IndexedDB `oracle-predictive-cache`**: splats and legal payloads, cleared at sign-out.

## 5. What leaves Neoh

See `docs/subprocessor-inventory.md`. The flows that matter here:
- Call audio goes to the voice AI (Qwen via DashScope, **Singapore by default**).
- SMS goes to Telnyx, email to SMTP (Gmail) and payments to Stripe.
- LLM prompts go to Fireworks.
- Photos go to RunPod during a reconstruction; the pod is terminated, which deletes everything on it.

## 6. Backups

Postgres backups are 7-day point-in-time recovery. They cannot be downloaded or edited, and a restore goes only to a new cluster.

Deleted data therefore stays in backups for up to 7 days, and every receipt says so with the date. A restore from a backup taken before an erasure brings the data back. Every completed erasure writes a directive to object storage, and `scripts/reapply-erasures.py` re-runs those after any restore, before traffic resumes. The live test proves this with a simulated restore.

Spaces versioning is off, so a deleted object is gone.

## 7. Known gaps (tracked, not hidden)

| Gap | Risk | Status |
|---|---|---|
| `clients` name/email/phone and `leads.payload` plaintext | DB read access exposes contacts | open — designed in L17-21; clients slice (a) built in L17-23 (0156: hashes and tokens kept current, not yet searched); plaintext not yet sealed; leads not started |
| `sms_messages.body`, `negotiation_events.transcript_excerpt`, `user_interactions.content` plaintext | DB read access exposes what people wrote and said | new writes sealed since 0152 (L17-21, local); old rows plaintext until `backfill_sealed_text.py` runs in each environment; refusing plaintext is a later contract migration |
| `email_outbox` subject/body, `interaction_logs` subject/text, `ai_tool_operations.result`, `beliefs.source_quote`, `user_profiles.profile_summary` plaintext | DB read access exposes what people wrote, what the AI returned and what it remembers | new writes sealed since 0154 (L17-22, local); old rows plaintext until `backfill_sealed_text.py` / `sealed_text_seal` runs in each environment; refusing plaintext is a later contract migration |
| `client_notes.body` (AI call notes) plaintext | DB read access exposes what customers said | new writes sealed since 0132 (L17-03); old rows plaintext until `backfill_client_notes.py` runs in each environment |
| Per-tenant keys are *derived* (HKDF of the master key), not stored | No crypto-shredding of one tenant | accepted; erasure deletes rows instead |
| `recon-inputs/` staging copies carry no tenant | Orphaned photo copies | orphan audit finds them; worker should record the prefix (follow-up) |
| SQLite audit fallback | Emails/IPs on a container disk | fixed 2026-10-14 (L17-12, local): drained into the Postgres chain and deleted, capped, and purged after 30 days with an alert. A container that dies before Postgres returns still loses its rows (ephemeral disk); the loss is alerted only if the process lives to purge |
| `inbound_voice_calls.retention_expires_at` set but never read | Implies a 365-day timer that does not exist | policy is "brokerage record" (no timer); column to be dropped or wired |
| `pg_stat_statements` keeps query text | Literals in ad-hoc SQL | app queries are parameterised; operators must not paste PII into psql |
| PII in a few log lines (reset/signup email, consent normalisation) | Logs at DO for 90 days (build) / runtime retention | fixed 2026-10-13 (L17-04): those lines log ids; `backend/tests/test_log_pii_guard.py` fails on a logging call that passes a name, email, phone, contact or note/transcript variable. Since 2026-10-14 (L17-11) the guard also covers `agent_id` (logged as `log_redaction.actor_ref()`, a keyed pseudonym; `crypto.py`'s three sites are deferred to the slice that owns that file), and the root log handlers scrub emails, phone numbers and PostgreSQL row values (`Key (...)=(...)`, `Failing row contains (...)`) from messages and tracebacks. Not covered: free text with no recognisable pattern (a name or an address inside an exception message) |
| Free-text mentions (a name inside a note or chat) | Not found by identifier search | stated in every subject-request result |
