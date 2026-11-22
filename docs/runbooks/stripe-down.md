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

**Closures and withdrawals** (external audit 22.1): a brokerage that closes or
reopens during the outage is told its renewal change is *pending*, not done.
A `privacy:billing_renewal` job retries it for about 2.4 hours and confirms
it with a read-back. If Stripe is still down after that, the `external_cleanup`
alert fires: see [account offboarding](../account-offboarding-runbook.md) § 5.

**Do:**
1. Check status.stripe.com.
2. After a long outage, review the Stripe dashboard's failed webhook deliveries and resend them. Duplicates are harmless.
