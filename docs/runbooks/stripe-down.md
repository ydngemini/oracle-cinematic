# Runbook: Stripe down or webhooks delayed

**What does not happen:** existing customers do **not** lose access. Entitlement is read from the local `subscriptions` table.

**What does:**
- New checkout and the billing portal say "temporarily unavailable".
- SDK calls run off the event loop with timeouts and a stable idempotency key, so a double click is one session.
- A second checkout while a plan is live is refused (409).

**Webhooks:**
- Deduplicated by event id, in the same transaction as their effect.
- An older event cannot overwrite a newer one (`last_event_created`).
- An `invoice.paid` or `subscription.updated` arriving before its `checkout.session.completed` gets a **503**, so Stripe redelivers it (for up to 3 days) instead of the event being lost.
- Known limit: two events in the **same second** apply in arrival order. If a subscription looks wrong after an incident, compare with the Stripe dashboard and replay the latest event from it.

**Do:**
1. Check status.stripe.com.
2. After a long outage, review the Stripe dashboard's failed webhook deliveries and resend them. Duplicates are harmless.
