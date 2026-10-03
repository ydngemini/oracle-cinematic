# Support model: how a brokerage gets help

Small and explicit. This is for the first ten brokerages, not a help-desk
product. There is no ticketing system: support is an inbox, an urgent route,
and the operator console.

## Contacts — **OWNER DECISION, NOT YET MADE**

| | Value |
|---|---|
| Support email | **`<<SUPPORT_EMAIL — owner to decide; must be on a provider with a DPA>>`** |
| Urgent route (phone or text) | **`<<URGENT_ROUTE — owner to decide>>`** |
| Hours | **`<<e.g. Mon–Fri 9–6 local, urgent route 24/7 during the pilot>>`** |
| Who is on point | **`<<name>>`** (first responder), **`<<name>>`** (backup) |

Until these are filled, the owner gate `support_contact` keeps the production
readiness check BLOCKED (`docs/launch-readiness/owner-gates.json`). Give the
brokerage owner these values at checklist item 17.

## What is urgent

**Urgent** means reply within 30 minutes, inside the hours above:

- Agents cannot sign in, or Neoh is down for the brokerage.
- Calls or texts are failing, or going to the **wrong** person.
- Anything that looks like another brokerage's data, or data that should not be visible.
- A message or call that Neoh sent which nobody approved.
- Billing charged wrongly.

**Not urgent** means reply the next business day: a slow page, one sync
late, a confusing screen, a feature request, an import question.

When unsure, treat it as urgent.

## Ownership

| Area | Owner |
|---|---|
| First response and triage | the person on point |
| Platform, releases and incidents | the operator ([`runbooks/README.md`](runbooks/README.md)) |
| Security or privacy events | the operator, under [`security-incident-response.md`](security-incident-response.md) |
| Product decisions from feedback | the owner, via [`pilot-feedback.md`](pilot-feedback.md) |

## Triage: the same four questions every time

1. **Who and what?** The brokerage, the agent, what they tried, and when (timezone).
2. **Is it one person, one brokerage, or everyone?** Open the operator console
   → the brokerage's **diagnostics**, and `GET /api/admin/health/components`.
3. **Is a component degraded?** If yes, follow its runbook ([`runbooks/README.md`](runbooks/README.md)),
   and tell affected customers using [`status-communication.md`](status-communication.md).
4. **Otherwise:** reproduce it on staging, or with the owner's own test data. Then fix it,
   or log it in [`pilot-feedback.md`](pilot-feedback.md).

## When a bug becomes an incident

A customer-impacting bug becomes an **incident** when any of these is true:

- it is urgent (list above);
- it affects more than one agent;
- it involves data visibility or an unapproved outbound action.

Once it is an incident:

1. Declare it to yourself and the backup. Note the start time.
2. Follow the component runbook: alert → component → runbook → mitigation → verification.
3. Communicate with the templates: Investigating, Identified, Monitoring, Resolved
   ([`status-communication.md`](status-communication.md)).
4. Close it when verified. Add three lines to the **Incident log** at the
   end of this page: what happened, what fixed it, and what prevents a repeat.
   If it came from customer feedback, also link it from that entry in
   [`pilot-feedback.md`](pilot-feedback.md).

Security or data exposure always goes to [`security-incident-response.md`](security-incident-response.md) first.

## How support sees diagnostics, safely

Support uses the **operator console**, which shows state and never content:

| To see | Use |
|---|---|
| Tenant capability health, plan, agent count, failed/retrying jobs, release, recent provider error categories | operator console → Brokerages → **diagnostics** (`/api/admin/brokerages/{id}/diagnostics`) |
| Billing state | operator console → Billing exceptions (`/api/admin/billing/exceptions`) |
| MLS board, licensed/test, last sync, freshness, entitled tenants | operator console → MLS (`/api/admin/mls/feeds`) |
| Voice and messaging configured, number, verification, routing errors | operator console → Communications (`/api/admin/comms`) |
| AI provider, health, latency, tool failure rate | operator console → AI (`/api/admin/ai`) |
| Platform health and open alerts | `/api/admin/health/components` |

The diagnostics bundle **excludes** message bodies, call transcripts,
documents, password hashes, tokens and credentials. If support ever needs
content to solve a problem, ask the customer to share that one item. The
operator does not go and get it.

**There is no impersonation.** Support never signs in as a customer, and
there is no "view as user" tool. That is deliberate: unrestricted
impersonation would be the most dangerous capability in the system, and the
operator console answers the support questions without it. Operator sign-in
needs its own second factor. Operator actions are audited.

## Incident log

Newest first. Three lines each: what happened (start/end, who was affected), what fixed it (runbook used), what prevents a repeat.

_None yet: no brokerage is live._
