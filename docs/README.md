# Operating Neoh: start here

One page to open first. Everything else is linked from it.

| I need to… | Open |
|---|---|
| **Respond to an alert or outage** | [`runbooks/README.md`](runbooks/README.md): alert → component → runbook → mitigation → verification |
| Tell customers about an incident or maintenance | [`status-communication.md`](status-communication.md) |
| Know whether Neoh is ready for customers | [`launch-state.md`](launch-state.md), then `python3 scripts/neoh-launch-readiness.py --env production` |
| **Deploy Neoh** | [`deploy-digitalocean.md`](deploy-digitalocean.md) (the only deploy path); [`release-checklist.md`](release-checklist.md) |
| Create or fix staging | [`staging-setup.md`](staging-setup.md) |
| Know which infra files are live and which are legacy | [`infrastructure-status.md`](infrastructure-status.md) |
| Onboard a brokerage | [`brokerage-go-live-checklist.md`](brokerage-go-live-checklist.md), then [`first-brokerage-launch.md`](first-brokerage-launch.md) (T-7 … T+7) |
| Help a brokerage | [`support-model.md`](support-model.md): contacts, what is urgent, triage, safe diagnostics |
| Record what a pilot customer said | [`pilot-feedback.md`](pilot-feedback.md) |
| Read the pilot numbers honestly | [`pilot-metrics.md`](pilot-metrics.md) |
| Audit the live UI | `python3 scripts/audit-neoh-production.py --base-url https://<env> --output audit.json` |
| Roll back a release | `scripts/rollback.sh` ([`deploy-digitalocean.md`](deploy-digitalocean.md) § Rollback) |
| Restore the database | [`disaster-recovery-state-map.md`](disaster-recovery-state-map.md) |
| Handle a security event or rotate a credential | [`security-incident-response.md`](security-incident-response.md), [`credential-rotation.md`](credential-rotation.md) |
| Offboard an agent, close an account, or answer a data request | [`account-offboarding-runbook.md`](account-offboarding-runbook.md), [`privacy-request-runbook.md`](privacy-request-runbook.md) |
| Run MLS, hosted SMS or GPU reconstruction | [`mls-production-runbook.md`](mls-production-runbook.md), [`telnyx-hosted-sms-runbook.md`](telnyx-hosted-sms-runbook.md), [`runpod-pods-runbook.md`](runpod-pods-runbook.md) |
| Understand capacity and connection limits | [`capacity-plan.md`](capacity-plan.md), [`database-connection-budget.md`](database-connection-budget.md), [`performance-capacity-runbook.md`](performance-capacity-runbook.md) |
| See what fails how | [`provider-failure-matrix.md`](provider-failure-matrix.md), [`dependency-resilience-map.md`](dependency-resilience-map.md), [`resilience-drill-report.md`](resilience-drill-report.md) |
| Review security or privacy posture | [`security-launch-review.md`](security-launch-review.md) + [`security-launch-gate.json`](security-launch-gate.json); [`privacy-data-map.md`](privacy-data-map.md), [`subprocessor-inventory.md`](subprocessor-inventory.md) |

Two facts that catch operators out on DigitalOcean:

- **An HTML 504 from `server: cloudflare` is usually the app's own 503.** DO's
  edge rewrites it, and keeps the real status in `x-do-orig-status`.
- **`doctl apps update` with a blank secret wipes it.** Only CI or
  `scripts/rollback.sh` should apply specs, because they carry secrets. Never
  apply a rendered spec by hand. ([`staging-setup.md`](staging-setup.md) step 13)
