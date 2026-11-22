# Runbooks — start here

An alert arrives as an email titled **`[Neoh] <component> <STATE>`** (from `backend/ops_alerts.py`, sent to `ORACLE_ALERT_EMAIL`) and as a row in `ops_alerts`. Use the component name to find the runbook below.

To prove alerts reach a person (required before go-live, audit 30.3): Operator overview > Alert delivery > **Send test alert**, then type the code from the email titled `[Neoh] Test alert: confirm you received it`. A code works once, for 24 hours, with 5 tries; one test a minute. Re-test after changing `ORACLE_ALERT_EMAIL` or the mail relay.

1. **Confirm** what is affected: `GET /api/admin/health/components` (platform admin). It returns every component's state plus open alerts.
2. **Open the runbook** for that component in the table.
3. **Mitigate** using the runbook's *Do* steps.
4. **Verify**: the component returns to `HEALTHY`, its alert closes by itself, and you get a `[Neoh] <component> recovered` email. Alerts close only after the component has stayed healthy for about 2.5 evaluation intervals (≈ 2.5 min).
5. If customers noticed, send the status update from [`docs/status-communication.md`](../status-communication.md).

| Alert component | What customers notice | Runbook | Verify recovery |
|---|---|---|---|
| `database` | Everything: "Neoh is temporarily unavailable. Your work is saved." | [database-down](database-down.md) | `/health` → 200; `database` HEALTHY |
| `realtime_fanout` | Live updates lag; refreshing shows the latest | [database-down](database-down.md) § realtime listener (reconnects by itself) | `realtime_fanout` HEALTHY; `/health` `components.realtime_fanout.reconnects` incremented |
| `workers` | Sends, syncs and Neoh replies wait | [background-work-stalled](background-work-stalled.md) | `GET /health/workers` lists a live worker on the current release |
| `scheduler` | Periodic syncs, reconciliation and cleanup stop | [background-work-stalled](background-work-stalled.md) | `scheduler` HEALTHY (heartbeat newer than 2 ticks + 2 min) |
| `job_queue` | Work waits longer than usual | [background-work-stalled](background-work-stalled.md) | `job_queue` HEALTHY; `oldest_ready_s` falling |
| `valkey` | "Calling is temporarily unavailable." The CRM works | [cache-down](cache-down.md) | `valkey` HEALTHY |
| `ai` | "Neoh is having trouble answering right now." | [ai-provider-down](ai-provider-down.md) | a test chat turn completes; `ai` HEALTHY |
| `outbound_side_effects` | "Some calls or messages are still being confirmed." | [voice-provider-down](voice-provider-down.md) · [messaging-provider-down](messaging-provider-down.md) | no new `reconciliation_required` commands; review "needs review" ones |
| `media_jobs` | Voice walkthrough notes or 3D tours take much longer than usual, or never finish | [background-work-stalled](background-work-stalled.md); what each job loses on a restart: [worker inventory](../worker-background-work-inventory.md). A voice job stuck `running` resumes only when the worker restarts | `media_jobs` HEALTHY; `voice_silent` and `recon_silent` back to 0 |
| `email_outbox` | "Some emails are waiting to send." | [email-provider-down](email-provider-down.md) | outbox `queued` drains; no `delivery_unknown` growth |
| `valuation_providers` · `property_harvesters` · `market_data` | Valuations, public-record facts or market figures say "unavailable" for a source | [property-data-down](property-data-down.md) | the component is HEALTHY (valuations: a recorded successful call) |
| `platform_email_relay` | "Some emails may not be sending right now." | [email-provider-down](email-provider-down.md) | a send succeeds; `consecutive_failures` back to 0 |
| `export_cleanup` | Nothing visible. An expired data-export archive is still in storage a day past expiry, so personal data is kept longer than promised | [object-storage-down](object-storage-down.md): the hourly delete is refused (credentials, bucket policy). The operation's `result.artifact_delete_pending` names the error | `export_cleanup` HEALTHY: the next hourly run deleted it |
| `external_cleanup` | A departed agent's Google connection could not be revoked at Google, so the grant may still be live; or a closed (or reopened) brokerage's Stripe renewal could not be confirmed, so it may renew (or stop) against what the owner was told. Each after ~2.4 h of retries | [account offboarding](../account-offboarding-runbook.md): § 1 "Google revocation is confirmed, not assumed" (`detail.credentials_unrevoked`; the operation's `result.credential_revocation` says why) and § 5 "Subscription renewal is confirmed, not assumed" (`detail.billing_divergent`; the closure operation's `progress.billing`) | `external_cleanup` HEALTHY: no row marked `revocation_failed`, no closure with `progress.billing.state = 'divergent'`, and nothing pending over 6 h (`detail.stuck_pending`) |
| `mls` | "Listing data may be out of date." | [mls-provider-down](mls-provider-down.md) (legacy, MLS out of scope) · [MLS production runbook](../mls-production-runbook.md) | the feed shows READY; `last_success_at` recent |

## Failures without a dedicated alert component

| Symptom | Runbook |
|---|---|
| Stripe checkout or portal fails; webhook 5xx in the Stripe dashboard | [stripe-down](stripe-down.md) |
| Calendar or Google sign-in actions fail | [google-integration-down](google-integration-down.md) |
| Uploads or media fail | [object-storage-down](object-storage-down.md) |
| 3D reconstruction stuck or failing | [gpu-provider-down](gpu-provider-down.md) · [RunPod pods](../runpod-pods-runbook.md) |
| Suspected breach, leaked credential, cross-tenant data | [security incident response](../security-incident-response.md) · [credential rotation](../credential-rotation.md) |
| Database lost or restore needed | [disaster-recovery state map](../disaster-recovery-state-map.md) — after any restore run `scripts/reapply-erasures.py --apply` before taking traffic |
| Hosted SMS number or 10DLC problem | [Telnyx hosted SMS](../telnyx-hosted-sms-runbook.md) |
| A brokerage leaving, or a data request | [account offboarding](../account-offboarding-runbook.md) · [privacy requests](../privacy-request-runbook.md) |

## Reference

- What fails how, and its customer impact: [provider failure matrix](../provider-failure-matrix.md)
- Every dependency, with its timeout, retries and breaker: [dependency resilience map](../dependency-resilience-map.md)
- Measured detection and recovery times: [resilience drill report](../resilience-drill-report.md)
- Release gate and smoke test: [release checklist](../release-checklist.md), `infra/digitalocean/smoke-test.sh`
- Launch readiness, one command: `python3 scripts/neoh-launch-readiness.py` (see [deploy-digitalocean](../deploy-digitalocean.md))

## Reading DigitalOcean signals

- **504 from `server: cloudflare` with `x-do-orig-status: 503` is the API's own 503.** DigitalOcean's edge rewrites every application 503 (database down, fail-closed degradation, a webhook without its signing secret) into an HTML 504, and the API's `Retry-After` never reaches the client. In DO dashboards and logs, read such 504s as the component's 503 and follow its row above.
- `/live` is not routed publicly: from outside it answers the SPA's 200. Use `/health`, which must return the API's JSON, and `/health/workers`.

## Around incidents

- Is a customer's problem an incident, and who owns it: [support model](../support-model.md) (urgent list, triage, bug → incident, safe diagnostics, incident log)
- Launch state and open blockers: [launch state](../launch-state.md), and `python3 scripts/neoh-launch-readiness.py --env production`
- Live UI check after a fix: `python3 scripts/audit-neoh-production.py --base-url https://<env> --output audit.json`
- Operations index: [docs/README.md](../README.md)
