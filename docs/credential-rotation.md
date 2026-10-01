# Credential rotation

What each credential protects, what breaks when it changes, and the actual
procedure. Values live as `type: SECRET` env vars on the App Platform app
(`infra/digitalocean/app.yaml`), set through the DO console or
`doctl apps update` — never in the repo, never in an image. After any change,
redeploy both `api` and `worker` and run `infra/digitalocean/smoke-test.sh`.

Do every rotation on **staging first**.

| Credential | Overlap supported? | User-visible effect |
|---|---|---|
| `ORACLE_SECRET_KEY` | no | everyone signs in again |
| `ORACLE_ENCRYPTION_MASTER_KEY` | **no — re-encryption required** | downtime for a re-encryption run |
| DB admin / app password | via the migration runner | none if done in order |
| Spaces keys | yes (two keys) | none |
| DigitalOcean API token | yes | CI deploys fail until the GitHub secret is updated |
| Plivo auth token | yes (`PLIVO_AUTH_TOKEN_PREVIOUS`) | none |
| Twilio auth token | yes (`TWILIO_AUTH_TOKEN_PREVIOUS`) | none |
| Telnyx API key / public key | key: yes; webhook public key: no | inbound SMS refused until both sides match |
| Stripe secret key / webhook secret | yes (Stripe "roll" keeps the old one live for a chosen window) | none |
| SMTP password | no | mail fails until updated |
| Google OAuth client secret | yes (Google allows two) | none |
| MLS provider tokens | provider-dependent | sync pauses |
| AI provider key (Fireworks) | yes (issue new, revoke old) | none |
| `ORACLE_ADMIN_PASSPHRASE` | no | operator signs in again |

## ORACLE_SECRET_KEY (JWT signing)

Signs every session, reset and portal token. No previous-key support: the
moment it changes, every outstanding token fails signature verification.

1. Generate ≥ 32 random bytes (`python -c "import secrets;print(secrets.token_urlsafe(48))"`).
   Production refuses a missing, short or placeholder value at boot (`config.validate_or_die`).
2. Set it on `api` and `worker` together; redeploy.
3. Everyone (including the operator) signs in again; outstanding reset and
   portal links stop working — re-issue portal links that matter.

This is also the **"end every session now"** control in an incident. To end one
account's sessions without touching anyone else: bump its `users.session_epoch`
(migration 0117) or suspend it.

## ORACLE_ENCRYPTION_MASTER_KEY (PII and credential encryption)

Per-tenant keys are derived from it (HKDF-SHA256, `crypto.derive_tenant_key`)
and used with pgcrypto `pgp_sym_encrypt`. **Ciphertext carries no key version
and there is no previous-key fallback**, so changing the value makes every
existing ciphertext undecryptable — silently: decryption yields NULL/garbage
rather than an error, and a restore with the wrong key "succeeds" and reads as
blank (see the DR notes). This is not a config change; it is a migration.

Columns that depend on it (verified against the schema, 2026-10-01):
`agent_contacts.pii_ciphertext`, `ai_chat_actions.{before,after}_ciphertext`,
`ai_chat_messages.content_ciphertext`, `ai_record_attachments.{bytes,extracted_text}_ciphertext`,
`clients.encrypted_contact`, `contact_intake_sessions.{normalized_fields,raw_answers,transcript}_ciphertext`,
`contact_property_relationships.property_label_ciphertext`, `contract_documents.content_ciphertext`,
`contract_draft_workspaces.payload_ciphertext`,
`inbound_voice_calls.{caller_phone,intake_answers,summary,transcript}_ciphertext`,
`lead_intake_events.payload_ciphertext`, `lead_source_connectors.webhook_secret_ciphertext`,
`leads.encrypted_payload`, `oauth_authorization_states.code_verifier_ciphertext`,
`provider_credentials.{token,refresh}_ciphertext`, `transaction_parties.contact_ciphertext`.

Procedure (planned maintenance, never mid-incident improvisation):

1. Take a fresh backup and verify a restore WITH the current key decrypts.
2. Put the app in recovery mode and scale the `worker` to 0 (nothing writes ciphertext).
3. Run a re-encryption job that, per tenant and per column, decrypts with the
   old derived key and encrypts with the new one in one transaction per batch.
   **No such tool exists in the repo yet** — it must be written and drilled
   against a restored copy before it is ever run in production.
4. Spot-check decryption with the new key on every column above.
5. Deploy the new key, lift recovery mode, scale the worker back.
6. Store the old key offline until the next backup cycle has fully rolled over
   (7 days on DO) — older backups still need it.

If the key **leaked**, assume every encrypted column of every tenant is exposed
to whoever also holds the database; rotation limits future exposure only.

## Database credentials

- **App role (`oracle_app_login`):** set `ORACLE_DB_APP_PASSWORD` and run the
  migration runner — it sets the role password (`run_migrations.py`) — then set
  the same value on the app and redeploy. Brief connection errors during the
  swap; run it in a quiet window.
- **Admin (`doadmin` / migration user):** rotate in the DO console, then update
  the `ORACLE_DB_ADMIN_PASSWORD` GitHub environment secret (staging and production).

## Spaces keys

Create a second key pair in DO, set `ORACLE_S3_ACCESS_KEY_ID` / `ORACLE_S3_SECRET_ACCESS_KEY`, redeploy,
verify a media upload and download, then delete the old key pair.

## DigitalOcean API token

Create a new token (scoped to the app and registry), update the
`DIGITALOCEAN_ACCESS_TOKEN` secret in both GitHub environments, run a staging
deploy, then revoke the old token.

## Plivo / Twilio auth tokens

1. Rotate at the provider (both keep the old token valid during their rollover).
2. Set the new value as `PLIVO_AUTH_TOKEN` / `TWILIO_AUTH_TOKEN` and the OLD one
   as `*_PREVIOUS`; redeploy. Webhook signatures under either now verify.
3. After the provider retires the old token, unset `*_PREVIOUS`.

## Telnyx

API key: issue new, deploy, revoke old. The webhook public key
(`TELNYX_PUBLIC_KEY`) changes only if Telnyx rotates its signing key — inbound
webhooks are refused (503/400) until it matches, and Telnyx retries.

## Stripe

- **Secret key:** "roll" in the dashboard with an overlap window, deploy the
  new `STRIPE_SECRET_KEY`, confirm a checkout session can be created on staging.
- **Webhook secret:** roll the endpoint secret (Stripe signs with both during
  the overlap), deploy the new `STRIPE_WEBHOOK_SECRET`. Production refuses to
  boot without it when billing is configured, and the webhook answers 503
  while it is unset — it never verifies with an empty key (review BILL-1).

## SMTP

Change the mailbox password, set `ORACLE_SMTP_PASSWORD`, redeploy, send a reset
to a test account. Tenant-stored SMTP credentials are each brokerage's own and
are rotated by them in the CRM.

## Google OAuth client secret

Add a second secret in Google Cloud, deploy it, verify a connect flow, delete
the old one. Tenant refresh tokens are unaffected.

## MLS provider tokens and AI provider key

Issue new at the provider, deploy, confirm one sync / one chat reply on
staging, revoke the old. MLS tokens never reach the browser.

## ORACLE_ADMIN_PASSPHRASE (operator login)

Set a new value (≥ 10 characters; use a long random one) and redeploy; also
rotate `ORACLE_SECRET_KEY` if the old passphrase may have been used by someone
else, since that is what ends their existing session.
