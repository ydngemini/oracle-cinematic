# Security incident response

For the people on call for Neoh. Threat model: `docs/security-threat-model.md`.
Rotation procedures referenced below: `docs/credential-rotation.md`. Recovery
of data: `docs/disaster-recovery.md` (restores into a NEW cluster; DO backups
are 7-day and not downloadable).

## Always, first

1. **Open an incident note** (time-stamped, append-only) — who noticed what, when.
2. **Preserve evidence before changing anything:** export the relevant
   `audit_ledger` rows, App Platform runtime logs for both `api` instances and
   the `worker` (`doctl apps logs <app> <component> --type run`), and the
   Postgres slow/error log window. Logs on App Platform are short-lived —
   export them now, not after the fix.
3. **Decide containment scope** with the table below. Prefer the narrowest
   control that stops the harm.
4. **Customer impact:** which tenants, which data classes (crown jewels table
   in the threat model), which time window. Breach-notification duties depend
   on the data class and the customer's jurisdiction — involve counsel for any
   cross-tenant exposure or PII disclosure.

## Containment controls that exist

| Control | Effect | How |
|---|---|---|
| Suspend a user | ends every session of that account at its next request; blocks sign-in | `POST /api/brokerage/team/{user_id}/suspend` (owner; platform admin for an owner) |
| End one account's sessions without suspending | same, keeps the account | `UPDATE users SET session_epoch = session_epoch + 1 WHERE id = …` |
| End ALL sessions | every token invalid at once | rotate `ORACLE_SECRET_KEY` (credential-rotation.md) |
| Recovery mode | no SMS, email, calls, calendar writes, billing calls or provisioning; auth and RLS unchanged | set `ORACLE_RECOVERY_MODE=1` on api + worker, redeploy |
| Stop the worker | no background job runs (sends, syncs, AI replies) | scale `worker` to 0 |
| Refuse a webhook source | provider deliveries 503/400 | unset that provider's signing secret (Stripe answers 503 when unset) |
| Revoke a capability link | portal / upload link dead | revoke in the CRM (sets `revoked_at`) |

## Playbooks

### Credential leak (a key, password or token appears somewhere it should not)
- **Contain:** rotate that credential now (credential-rotation.md). Assume it was used.
- **Isolate:** if it is a provider key, check the provider's own console for activity since the earliest possible exposure.
- **Evidence:** where it leaked (commit, log line, ticket), the exposure window.
- **Recover:** remove the copy (a git history rewrite does not un-leak; rotation does).
- **Impact:** what the credential could reach; for a DB credential, treat every tenant's data as exposed.

### Cross-tenant exposure (tenant A saw tenant B's data)
- **Contain:** recovery mode is not enough — suspend the reporting account if hostile, and disable the route (feature flag or hotfix) that leaked.
- **Evidence:** the exact request (audit ledger + API logs), the response size, the B rows involved.
- **Recover:** fix, add a test to `tests/test_security_launch_review.py` and `tests/rls_security_review.sql`, re-run `performance/security/idor_sweep.py` against staging.
- **Impact:** tenant B must be told what was seen and by whom.

### Compromised user account
- **Contain:** suspend it (sessions end immediately), or bump its `session_epoch` and force a password reset.
- **Evidence:** audit rows `WHERE user_id = <agent_id>` for the window; outbound commands it created or approved.
- **Recover:** reinstate after the owner confirms identity; the old sessions stay dead.
- **Impact:** that user's brokerage only — an agent cannot reach other tenants or admin (tested).

### Compromised platform admin (operator credential)
- **Contain:** rotate `ORACLE_ADMIN_PASSPHRASE` and `ORACLE_SECRET_KEY` together (the second ends every outstanding session including the attacker's); redeploy.
- **Evidence:** audit rows for `tenant_id = 00000000-…` and every admin route in the window; `role-changes`, MLS entitlement grants.
- **Impact:** potentially every tenant. There is no MFA on this login today (launch review AUTH-12).

### Webhook signing secret leak
- **Contain:** rotate at the provider and in App Platform together; during the gap, forged events are possible — Stripe events still need a valid event id and are deduplicated, Telnyx inbound needs an active route.
- **Evidence:** deliveries in the window whose provider ids the provider does not recognise.
- **Recover:** reconcile subscription state from Stripe's API (source of truth), not from webhook history.

### Provider key leak (Plivo, Telnyx, Twilio, Stripe secret, SMTP, Fireworks, Spaces)
- **Contain:** rotate at the provider; put the app in recovery mode if the key can send to customers.
- **Impact:** usage billed to Neoh, messages sent in Neoh's name — check the provider console.

### Encryption master key issue (lost, leaked, or wrong value deployed)
- **Wrong value deployed:** decryption returns garbage/NULL silently (see the DR note: a keyless restore SUCCEEDS and reads as blank). Roll back the env change immediately; do not let jobs write with the wrong key.
- **Leaked:** every `*_ciphertext` column of every tenant is exposed to anyone who also has the database. Rotation is a re-encryption project (credential-rotation.md) — plan it, don't improvise it.
- **Lost:** encrypted columns are unrecoverable. The master key is its own backup artifact.

### Malicious upload
- **Contain:** the file is already refused if over the pixel or byte limits; if one got through, delete the `property_media`/`media_blobs` row and the storage object.
- **Evidence:** keep a copy in an isolated location before deleting.
- **Note:** uploads are served with a server-sniffed type, `nosniff` and `CSP default-src 'none'`; no SVG/HTML is ever accepted.

### AI action abuse (Neoh did something it should not have)
- **Contain:** reject pending approvals for the affected record; recovery mode stops anything already approved from sending.
- **Evidence:** `ai_chat_messages` (encrypted; decrypt with the tenant key), `ai_tool_operations` ledger, `action_approvals` (who requested, who approved).
- **Recover:** undo applied INTERNAL_EDITs from their receipts (`ai_chat_actions`).
- **Remember:** the tool layer, not the model, is the boundary — find which control let it through.

### Supply-chain vulnerability
- **Contain:** if it is exploitable on a shipped path, ship the bump; CI's `pip-audit --strict` and Trivy block releases on known CVEs.
- **Evidence:** the SBOM attached to each release (Mission 6) says which builds carried it.
