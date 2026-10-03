# Runbooks — start here

An alert arrives as an email titled **`[Neoh] <component> <STATE>`** (from `backend/ops_alerts.py`, sent to `ORACLE_ALERT_EMAIL`) and as a row in `ops_alerts`. Use the component name to find the runbook below.

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
| `email_outbox` | "Some emails are waiting to send." | [email-provider-down](email-provider-down.md) | outbox `queued` drains; no `delivery_unknown` growth |
| `mls` | "Listing data may be out of date." | [mls-provider-down](mls-provider-down.md) · [MLS production runbook](../mls-production-runbook.md) | the feed shows READY; `last_success_at` recent |

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
