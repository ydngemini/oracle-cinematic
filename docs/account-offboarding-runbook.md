# Account offboarding runbook

What happens when an agent leaves, an owner leaves, billing stops, or a
brokerage closes. The principle throughout: **open work changes hands;
history keeps its author.** A note an agent wrote still says they wrote it
after they leave, and so do a text they sent and an approval they gave.
That is the brokerage's record.

## 1. An agent leaves: Offboard

**Settings → Team → Offboard…** opens a form:
1. The owner picks a successor (any active member) and gives a reason.
2. **Preview** runs the whole offboarding inside a transaction that is then
   rolled back. The counts shown are exactly what the real run will do.
3. The owner confirms with their password.

API: `POST /api/privacy/offboard {agent_id, successor_agent_id, reason, preview, password}`.

**What it does, in one transaction:**

| Step | Effect |
|---|---|
| Lock out | `users.is_active=false`, session epoch bumped (every session and socket ends on its next request), membership `suspended` |
| Stop routing | `agent_routing_state` → not accepting, capacity 0; returning leads they owned are re-routed (`lead_routing_api`) |
| Cancel unsent work they authored | commands (draft → queued), pending approvals (`revoked`), queued/failed jobs, missions (paused), smart-plan enrollments and steps (paused), queued emails, prepared calls, OAuth flows in progress |
| Move open responsibility to the successor | contacts, clients, open tasks, handoff tasks, open milestones, smart plans, sites (published copy still shows the old agent until republished — a warning says so), record attachments, segments, routing-rule pools; site collaborator rows removed |
| Phone & text | If the successor has no line: the route moves to them, with forwarding to the departing agent's phone cleared. Otherwise the line stays (it is the brokerage's clients' number) but stops forwarding to the departing agent. **Numbers are never released here.** |
| Personal credentials | their Google/SMTP rows disabled in the transaction, then (after commit) the Google grant is revoked at Google and the rows deleted; a failed revoke keeps the row disabled and is reported |

**Never rewritten:** every `created_by` and `updated_by`, `audit_ledger`,
note authors, `sms_messages.agent_id`, approval requesters and deciders,
decision traces, invitation senders. **Never transferred:** their AI chat,
autonomy preferences, profile, licences, or writing-style model.

A `privacy_operations` row (`kind='offboard'`) records the counts, and the
audit ledger records `team.member.offboarded`.

**Things to tell the owner:**
- Calendar events the agent created live in the agent's own Google
  Calendar, and Neoh cannot move them.
- Their direct email history is in their own mailbox.

## 2. Suspend (temporary) and reinstate

Settings → Team → Suspend. API: `POST /api/brokerage/team/{id}/suspend {reason}`.

Since 2026-10-01, suspension also:
- cancels the agent's unsent work;
- stops their line forwarding to their personal phone;
- stops new leads routing to them.

Reinstating restores the login only. Cancelled work stays cancelled, and
forwarding has to be set up again.

## 3. An owner leaves

- **Last owner.** A brokerage always keeps at least one active owner.
  Suspending, offboarding or demoting the last one is refused (409).
- **Transfer.**
  1. Promote the successor via the two-person role change
     (`POST /api/admin/role-changes`, approved by a second owner or by
     platform support).
  2. Then offboard or demote the departing owner.

  A suspended account cannot be promoted (it must be reinstated first).
- **Single-owner brokerages.** Every transfer needs platform support, because
  nobody else can approve it. Support verifies the request out of band with
  the outgoing owner, or with company documents if the owner is unreachable.
- **Tenant-level identities stay with the brokerage:** the Stripe customer,
  10DLC brand and campaign, and shared provider credentials (label `default`).
  Update the brand's contact person at the carrier if it named the departing
  owner.

## 4. Billing cancelled

When Stripe sends `customer.subscription.deleted`, `subscriptions.status` is
set to `canceled`. The app then:
- blocks spend and outreach routes (402);
- lets owners still sign in, read and export;
- deletes nothing. `ORACLE_CANCELED_ACCOUNT_ERASURE_DAYS` (default: never)
  decides whether an abandoned, unclosed account is ever erased automatically.

## 5. Closing a brokerage

**Settings → Account & data → Close this brokerage.** The owner types the
brokerage's name exactly, gives a reason, and enters their password.

API: `POST /api/privacy/closure`.

**At once:**
- state `closing`;
- every agent signed out (owners stay, to export or reopen);
- unsent work cancelled;
- portal and upload links, invitations and lead connectors revoked;
- sites archived and marketplace listings withdrawn;
- phone and text routes off (STOP texts are still recorded);
- background work stops (`claim_next_job` skips non-active tenants);
- spend and outreach routes refused with 423;
- the Stripe subscription set to not renew.

**During the grace period** (`ORACLE_CLOSURE_GRACE_DAYS`, default 30):
- the owner can export;
- the owner can **Keep the brokerage** (`POST /api/privacy/closure/withdraw`).
  This restores the users and routes the closure switched off. Cancelled
  jobs and revoked links are not restored.

**After it**, erasure runs (`docs/privacy-request-runbook.md` §4), and the
owner receives the receipt. The receipt states:
- what was deleted;
- what Neoh keeps and why: billing for 7 years, the security log for 2
  years, opt-out hashes for 5 years, and the receipt itself for 7 years;
- when the backups expire;
- what Neoh cannot reach: delivered messages, carrier records, and the
  agents' own Google Calendars.

## 6. Platform-initiated suspension (non-payment, abuse)

Operators set `tenants.lifecycle_state='suspended'` from a platform session
(the database trigger refuses this from a tenant session; there is no button
yet). That stops background jobs and refuses every spend/outreach route (423),
and starts no erasure clock. It does **not** switch off phone and text routes
or sign anyone out — do those too if the suspension must be total
(`UPDATE telephony_routes/messaging_routes SET active=false`, bump
`users.session_epoch`). Lift it the same way.
