# Brokerage go-live checklist

Complete one copy of this for **each** brokerage, before its agents rely on
Neoh. Every item says how to verify it, either in the product or in the operator console. "Operator
console" means the platform-admin views behind the profile sheet → **Admin**,
backed by:

- `GET /api/admin/brokerages`
- `/api/admin/brokerages/{id}/diagnostics`
- `/api/admin/billing/exceptions`
- `/api/admin/mls/feeds`
- `/api/admin/comms`
- `/api/admin/ai`
- `/api/admin/pilot-metrics`

None of them shows message bodies, transcripts, documents or credentials.
The brokerage's own setup state is `GET /api/brokerage/setup`, which the owner
sees as the setup screen. Its `capabilities` and `readiness` drive most of this
list.

**Before the first brokerage, the platform itself must be ready.** Run
`python3 scripts/neoh-launch-readiness.py --env production`: no BLOCKED rows
(see [`launch-state.md`](launch-state.md)).

Brokerage: ____________  Tenant id: ____________  Owner: ____________  Date: ________

| # | Item | Verify | Who | ✓ |
|---|---|---|---|---|
| 1 | **Brokerage created** | Operator console → Brokerages lists it. The diagnostics bundle shows tenant id, plan and status | operator | ☐ |
| 2 | **Owner account verified** | The owner signs in, completes the profile, and sees the setup screen. Setup `brokerage_profile` = READY. Diagnostics shows 1 owner | owner | ☐ |
| 3 | **Agents invited** | Setup → Team: invitations sent (`POST /api/brokerage/invitations`, up to 25 at once). Each agent opens the invite and signs in. Setup `agent_invites` = READY. Diagnostics agent count matches the roster | owner | ☐ |
| 4 | **Billing active** | Setup `billing` = READY. Operator console → Billing exceptions has **no** row for this tenant. Stripe shows an active (or trialing) subscription on the live price | owner / operator | ☐ |
| 5 | **Phone identity configured** | Setup `phone` = READY: business number assigned, and caller ID and forwarding set. Operator console → Communications shows voice "configured" and no routing error | operator | ☐ |
| 6 | **Messaging capability verified** | Operator console → Communications: messaging "configured", and the sender/10DLC verification is **not** pending. If pending, texting stays off. See [`telnyx-hosted-sms-runbook.md`](telnyx-hosted-sms-runbook.md) | operator | ☐ |
| 7 | **Email / calendar status** | Setup `email_calendar` (optional) is READY, or deliberately skipped. If connected, the agent's calendar shows in Work. Platform email (invites, resets) works: item 3 proves it | owner | ☐ |
| 8 | **MLS authorization status** | Setup `mls`. Operator console → MLS feeds shows the board, **licensed vs test**, last sync and freshness, and this tenant as entitled. If the brokerage does not need MLS yet, record "not required" (a WARN, never a blocker) | operator | ☐ |
| 9 | **Contacts imported** | Setup `contact_import` (optional). Work → People shows the brokerage's contacts, with the count matching the source file. Spot-check 3 records for name, phone and email | owner | ☐ |
| 10 | **Consent rules understood** | The owner confirms which contacts may be called or texted, and how opt-outs (STOP) work. Neoh blocks texting a contact who opted out (TCPA gate). Record who explained it | operator + owner | ☐ |
| 11 | **Neoh autonomy level confirmed** | Profile sheet → AI controls: each capability set to *observe*, *assist* or *autopilot* (`GET /api/autonomy`). Default to **assist**: Neoh prepares, a person releases. Some categories can never reach autopilot | owner | ☐ |
| 12 | **Test call completed** | From a person's page, call **the owner's own phone**. It connects and appears in that person's timeline as a durable result ("Call connected"). Operator console → Communications shows no new failure | owner | ☐ |
| 13 | **Test SMS completed** (if messaging enabled) | Text **the owner's own phone**: it is delivered, a reply lands in the conversation, and the timeline shows "Text sent" | owner | ☐ |
| 14 | **Test email completed** | Send an email from Neoh to **the owner's own address**. It is received, and appears in the timeline. The outbox has no `delivery_unknown` | owner | ☐ |
| 15 | **Test property workflow** | Open a property in Work. It shows data and photos, buyer matches where available, and asking Neoh about it gets an answer that names it. 3D appears only if a tour exists | owner | ☐ |
| 16 | **Data export / offboarding explained** | The owner knows: export is `POST /api/privacy/exports` (Settings); an agent leaving goes through offboarding with a successor; closing the account stops renewal and starts the retention schedule. See [`account-offboarding-runbook.md`](account-offboarding-runbook.md) and [`privacy-request-runbook.md`](privacy-request-runbook.md) | operator | ☐ |
| 17 | **Support contact established** | The owner has the support address and the urgent route from [`support-model.md`](support-model.md), and knows what counts as urgent. The operator has the owner's preferred contact | operator | ☐ |

**Go-live** when items 1–6, 10–12, 14, 16 and 17 are checked. Items 7, 8, 9, 13 and
15 may be "not required", but must be written down as such. Record the result in
[`pilot-feedback.md`](pilot-feedback.md) under the brokerage's name.

Never test communications on a real client. Test sends go only to the owner's
or operator's own numbers and addresses.
