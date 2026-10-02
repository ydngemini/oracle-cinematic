# Runbook: messaging provider (Telnyx) down

**You see:**
- `outbound_side_effects` DEGRADED;
- texts `failed` (definite) or `reconciliation_required`.

**What Neoh does:**
- SDK retries are off. A 4xx is a definite failure; a 5xx or timeout goes to reconciliation by message id, never resent.
- Delivery receipts only move status forward. A receipt that arrives before our row exists gets a 503, so Telnyx redelivers it.
- **STOP is never lost.** If recording the suppression fails, the webhook answers 503 and Telnyx redelivers. A STOP to an inactive number still suppresses.

**Do:**
1. Check Telnyx status.
2. After recovery, review `reconciliation_required` SMS commands, and resend only after confirming non-delivery.
3. If many STOPs arrived during an outage, check that `outreach_suppression` grew accordingly (`SELECT count(*) … WHERE created_at > <outage start>`).
