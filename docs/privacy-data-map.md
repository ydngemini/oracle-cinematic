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
| SQLite audit fallback (`ORACLE_AUDIT_SQLITE`) | copy of audit rows when Postgres is down | emails, IPs | **nothing** — see §7 |
| Browser localStorage / sessionStorage / IndexedDB | signed-in identity, comms templates, cached splats & legal payloads | yes, on the device | sign-out (`clearPrivateCaches.js`; identity + templates cleared since 2026-10-01) |
| Provider systems | calls, messages, recordings, payments | yes | §5 and `docs/subprocessor-inventory.md` |

## 2. Database, by category

Counts are from the dev database. They show shape, not production volume.

**Account** (Neoh is controller): `users` (email, name, password hash), `user_profiles` (phone, licence number, bio, LLM-written profile summary), `team_memberships`, `agent_licenses`, `agent_ce_log`, `agent_ai_settings`, `autonomy_preferences`, `agent_routing_state`, `brokerage_invitations`, `user_policy_acceptances`, `account_security_acceptances`.
**`agent_id` is the user's email address**, so every text actor column (`created_by`, `author_id`, `assignee_id`, `requested_by`, …) is personal data.

**Contact PII**:
- `clients` holds name, email, phone and notes **in plaintext**. Its `encrypted_contact` column exists but is unused.
- `agent_contacts` keeps PII encrypted with pgcrypto under a per-tenant key, with HMAC lookup hashes.
- `leads` has 10.4M rows in dev. `payload` holds owner names **in plaintext**, and `encrypted_payload` is unused.
- Also in this category: `transaction_parties` (encrypted), `contact_property_relationships`, `buyer_profiles` and `lead_intake_events` (encrypted payload).

**Business CRM**: notes, tasks, activities, tags, segments, showings, listings, transactions, offers, milestones, checklists, buyer requests, marketplace, smart plans (revisions immutable), missions, hyperlocal sites, studio campaigns.

**Communication content**:
- `sms_messages`: bodies and numbers **in plaintext**.
- `email_outbox`: recipients, subjects and bodies.
- `interaction_logs`.
- `inbound_voice_calls`: caller, transcript, summary and intake answers, all encrypted.
- `contact_intake_sessions` (encrypted).
- `negotiation_events.transcript_excerpt` (plaintext).
- `command_executions.draft`: outbound drafts.

**Communication metadata**:
- `telephony_routes`: numbers, including the agent's personal hand-off phone.
- `messaging_routes`.
- `tenant_messaging_brands`: EIN and address for 10DLC.
- `live_call_sessions`: recording-consent flags.
- `agent_call_intents`, `contact_nurture_jobs`, `lead_response_events`.

**Consent / suppression**:
- `outreach_consent`: proof text and IP, plaintext contact.
- `outreach_suppression`, `outreach_attempt_log`.
- `suppression_tombstones` (new): keyed hash only.

**AI**:
- `ai_chat_messages` (encrypted).
- `ai_tool_operations.result`: tool outputs, plaintext.
- `user_interactions.content`: raw chat turns, **plaintext**, with a text `tenant_id` and no FK.
- `beliefs.source_quote`, `client_ai_state` (summaries), `agent_decisions`, `ai_decision_traces`.
- `style_training_examples`: redacted.
- Model registry, training runs and evaluations.

**Documents and media**:
- Contracts, drafts and synthesis artifacts (encrypted content plus a vault object).
- `ai_record_attachments` (encrypted bytes).
- `messaging_hosted_documents`: LOA and invoice. The object is **not** app-encrypted.
- `property_media` and `media_blobs`, the latter raw bytes with no RLS and no `tenant_id`, removed through `property_media`.
- Pano scenes, floorplans, upload links, capture sessions, reconstruction jobs, tour variants and video jobs.
- `voice_walkthrough_jobs.audio`, which is nulled when the job finishes.

**Audit / security** (Neoh is controller): `audit_ledger` (hash-chained; email, IP, path), `audit_anomaly_alerts` (actor, source IP), `audit_chain_checkpoints` (new), `api_rate_limit_windows` (unsalted SHA-256 of IP; minutes), `process_heartbeats`.

**Secrets / capability tokens**: `provider_credentials` (encrypted Google/SMTP tokens), `lead_source_connectors` (encrypted webhook secret), `oauth_authorization_states`, `password_reset_tokens`, `client_portals`, `property_view_upload_links`, and invitations (all hashed tokens).

**Billing** (Neoh is controller): `subscriptions` (Stripe ids), `billing_usage_events`, `stripe_webhook_events` (event ids only).

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
| `clients` name/email/phone and `leads.payload` plaintext; `sms_messages.body` plaintext | DB read access exposes contacts | open — column encryption migration is a separate project (known limitation in security review) |
| Per-tenant keys are *derived* (HKDF of the master key), not stored | No crypto-shredding of one tenant | accepted; erasure deletes rows instead |
| `recon-inputs/` staging copies carry no tenant | Orphaned photo copies | orphan audit finds them; worker should record the prefix (follow-up) |
| SQLite audit fallback never purged | Emails/IPs on a container disk | containers are ephemeral on DO; purge on boot is a follow-up |
| `inbound_voice_calls.retention_expires_at` set but never read | Implies a 365-day timer that does not exist | policy is "brokerage record" (no timer); column to be dropped or wired |
| `pg_stat_statements` keeps query text | Literals in ad-hoc SQL | app queries are parameterised; operators must not paste PII into psql |
| PII in a few log lines (reset/signup email, consent normalisation) | Logs at DO for 90 days (build) / runtime retention | log_redaction masks tokens, not emails; follow-up |
| Free-text mentions (a name inside a note or chat) | Not found by identifier search | stated in every subject-request result |
