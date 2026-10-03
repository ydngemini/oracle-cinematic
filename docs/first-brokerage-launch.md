# First brokerage launch: the day-by-day sequence

Operational on purpose. Each day has a short list. The per-brokerage detail is
[`brokerage-go-live-checklist.md`](brokerage-go-live-checklist.md), the
platform gate is `scripts/neoh-launch-readiness.py`, and incidents start at
[`runbooks/README.md`](runbooks/README.md).

**Precondition:** `python3 scripts/neoh-launch-readiness.py --env production`
shows **no BLOCKED**. Today it shows 30; see [`launch-state.md`](launch-state.md).
Do not start T-7 until it is clean.

## T-7 days: providers and licensing

- [ ] Agree the pilot scope with the owner: how many agents, which
      capabilities they need (calling, texting, MLS, calendar), and the start date.
- [ ] **Phone:** buy or port the business number. Start the carrier/10DLC
      registration now, because verification takes days ([`telnyx-hosted-sms-runbook.md`](telnyx-hosted-sms-runbook.md)).
- [ ] **MLS:** if the brokerage needs MLS, confirm the board's data licence
      covers Neoh. Request feed credentials, and add the tenant's entitlement only once the licence
      is signed ([`mls-production-runbook.md`](mls-production-runbook.md)).
      Otherwise record "MLS not required".
- [ ] **Billing:** confirm the plan and the price. Make sure the production Stripe webhook is
      registered and the live key is set (readiness: `billing` PASS, `stripe_live_mode` confirmed by hand).
- [ ] **Privacy:** give the owner the subprocessor list. Make sure the DPA items in
      [`launch-state.md`](launch-state.md) are closed.
- [ ] Create the brokerage, invite the owner, and confirm the owner can sign in (checklist 1–2).

## T-2 days: import and validation

- [ ] The owner imports contacts. Spot-check counts and 3 records (checklist 9).
- [ ] The owner sends agent invites. Watch Setup → Team (checklist 3).
- [ ] Operator console → the brokerage's **diagnostics**: capability states,
      agent count, failed or retrying jobs = 0, release = the current production SHA.
- [ ] Confirm consent and opt-out handling with the owner (checklist 10). Set the Neoh
      autonomy level, defaulting to *assist* (checklist 11).
- [ ] Re-run the readiness check: still no BLOCKED.

## T-1 day: test communications

All tests go to the **owner's or operator's own** phone and email, never a client.

- [ ] Test call (checklist 12). Then test SMS, once 10DLC is approved (13). Then test email (14).
- [ ] Each shows as a durable result in the timeline, and Operator console →
      Communications shows no failure.
- [ ] Test property workflow (15).
- [ ] Run the UI audit against production as the owner's test agent:
      `python3 scripts/audit-neoh-production.py --base-url https://<domain> --output /tmp/audit.json`. It must have no BLOCKED.
- [ ] Explain export and offboarding (16). Hand over the support contact and urgent route (17).
- [ ] Freeze: no production deploy from now until T+1 unless it fixes this launch.

## GO-LIVE: owner first, then agents

- [ ] Morning: readiness check, then `/health/workers` (a live worker on the current release).
      Operator console: open alerts = none.
- [ ] Owner session (30 min): Home → what matters today; Work → find a real client;
      Neoh → ask about that client, take one action, see the result in the timeline.
- [ ] Agents sign in, each doing the same first five minutes on one of their own contacts.
- [ ] The operator stays watchful for the day: the operator console, the alert email, and
      the support inbox. Anything customer-visible is an incident
      ([`support-model.md`](support-model.md) § Urgent).

## T+1: review failures and usage

- [ ] Operator console → the brokerage's diagnostics: failed or retrying jobs,
      provider error categories, setup still incomplete.
- [ ] Billing exceptions: none for the tenant.
- [ ] Pilot metrics day 1 ([`pilot-metrics.md`](pilot-metrics.md)): active agents, Neoh
      conversations, actions completed.
- [ ] Ask the owner for anything blocking or confusing. Log it in [`pilot-feedback.md`](pilot-feedback.md).
- [ ] Lift the deploy freeze if nothing is open.

## T+7: pilot review

- [ ] Review with the owner: the pilot metrics for the week, plus "revenue influenced by Neoh",
      presented with the evidence rule in [`pilot-metrics.md`](pilot-metrics.md). That means associated, not caused.
- [ ] Go through the feedback log by category: what blocks, what confuses, and what to fix first.
      A request is not automatically a roadmap item.
- [ ] Decide continue, adjust or stop. If expanding, start the checklist for the next brokerage.
- [ ] Note any incident in the review, with its runbook and what changed.
