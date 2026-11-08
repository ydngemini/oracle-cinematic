# Runbook: email (SMTP) down

**You see:**
- `platform_email_relay` AUTH_FAILED (the relay's login was refused) or DEGRADED (sends failing), from the relay's own last sends (`provider_health` 'platform_smtp', 0164); UNKNOWN only means no send has been recorded since deploy;
- the delivery page's Email card reading "Last send failed" or "Not yet verified by a send" instead of "Ready";
- `email_outbox` DEGRADED (emails waiting, or delivery unknown);
- invitations showing "Email not delivered";
- the operator sign-in code not arriving.

**What Neoh does:**
- Composer emails wait in `email_outbox` (`queued`) and are sent when SMTP returns. The sweep re-enqueues them; each is claimed once.
- **Lost connection after the message was handed over** leaves the email `sending` with `delivery_unknown`. It is never resent automatically.
- Approved email commands follow the same rule (`reconciliation_required`).
- Password reset still answers neutrally, so no account enumeration is possible. The failure is in the log.
- Invitations exist even when the mail failed; the owner sees it and can Resend, which issues a new link.

**Do:**
1. Check the relay: credentials, app password, provider status.
2. Afterwards, find unknown deliveries: `SELECT id, to_email, updated_at FROM email_outbox WHERE status='sending' AND error LIKE 'delivery_unknown%'`. Ask the agent before resending (set `status='queued'` to resend deliberately).
