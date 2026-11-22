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
| Personal credentials | their Google/SMTP rows disabled in the transaction and marked `revocation_pending` (`validation_error`); a `privacy:revoke_credentials` job is queued in the same transaction. After commit the Google grant is revoked at Google and the rows deleted. A row whose revoke failed, or whose token cannot be read, is kept disabled and never deleted as revoked (external audit 22.6) |

**Never rewritten:** every `created_by` and `updated_by`, `audit_ledger`,
note authors, `sms_messages.agent_id`, approval requesters and deciders,
decision traces, invitation senders. **Never transferred:** their AI chat,
autonomy preferences, profile, licences, or writing-style model.

A `privacy_operations` row (`kind='offboard'`) records the counts, and the
audit ledger records `team.member.offboarded`.

**Google revocation is confirmed, not assumed (external audit 22.7).** The
operation is `running` until every disabled credential is confirmed revoked
(`result.external_revocation`: `pending` → `complete`); the API response and
the Offboard form say "pending" until then. Local access ends at once either
way. The `privacy:revoke_credentials` job retries for about 2.4 hours (12
attempts, revoking only rows still disabled). On its last attempt it marks the
operation `failed`, marks the rows `revocation_failed`, emits
`offboarding.external_cleanup_pending`, and the `external_cleanup` alert
fires. To resolve it:
1. Read the operation's `result.credential_revocation`: `revoke_failed`
   (Google refused or was unreachable) or `revocation_unverified` (the token
   could not be decrypted, the encryption key was missing, or Google rejected
   the only token held, an access token, as `invalid_token`: an expired
   access token proves nothing about the grant).
2. Ask the departed agent to remove Neoh under Google Account > Security >
   *Your connections to third-party apps & services*, or confirm the Google
   account is closed.
3. Delete the kept row (`provider_credentials`, `validation_error =
   'revocation_failed'`) on the platform login. The alert closes on its own.
There is no operator button for this yet.

While a row waits for revocation the same account cannot reconnect Google: the
OAuth callback refuses (`?google=revocation_pending`) and revokes the grant it
was just given, and storing a credential over it answers 409. Overwriting the
row would lose the only handle on the old grant. A connect that finishes, or a
credential stored, for a member who is no longer active is refused the same
way (`?google=member_inactive`, 409), so a Google flow that started before the
offboarding cannot leave the departed agent an active credential.

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

`canceled` is final for that subscription (T18.05): a later `invoice.paid`
(an open dunning invoice paid afterwards), `customer.subscription.updated` or
checkout event does not reopen it, because Stripe will never bill it again. To
resume, the owner subscribes again through checkout, which reuses the
brokerage's Stripe customer.

A brokerage with two live subscriptions (a second checkout paid before the
first one's webhook arrived) raises a `second_live_subscription` alert
(severity high) in the operator console's anomaly list. That list is newest
first and shared with routine security alerts, so the brokerage is also listed
under **Billing exceptions → Two live subscriptions**
(`GET /api/admin/billing/exceptions`, `duplicate_subscriptions`) with both
Stripe subscription ids, read from the subscriptions themselves; it stays
there until one of the two is cancelled. Both plans keep the brokerage
entitled and both bill: decide with the owner which one to keep, then cancel
and refund the other in Stripe. Nothing does that automatically.

An owner whose plan is `unpaid`, `incomplete` or `paused` is locked out and
refused a second checkout (409); the locked billing overlay offers **Manage
billing**, which opens the Stripe portal on that plan's customer.

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
- the Stripe subscription set to not renew, through a durable job (below).

**During the grace period** (`ORACLE_CLOSURE_GRACE_DAYS`, default 30):
- the owner can export;
- the owner can **Keep the brokerage** (`POST /api/privacy/closure/withdraw`).
  This restores the users and routes the closure switched off, and nothing
  else (the response counts `restored_users` and `restored_routes`). Cancelled
  jobs and revoked links are not restored. Renewal is turned back on through
  the same durable job.

**Under a legal hold.** A hold stops the erasure when the grace period ends:
the brokerage stays `closing` and its closure operation becomes
`blocked_legal_hold`, so the owner can still keep the brokerage. The
withdrawal restores what that closure switched off and ends the operation
`cancelled`, with `result.withdrawn_from = blocked_legal_hold` and its `error`
(`legal hold <id>`) kept. The hold itself stays until an operator releases it.
Once the hold is released, the next erasure finishes that closure `succeeded`,
with `result.erasure_started_from = blocked_legal_hold` and the error kept
(L17-58).

*Closures an older withdrawal left behind.* Before L17-58, withdrawing a
blocked closure reopened the brokerage but restored nothing and left the
operation `blocked_legal_hold`: its agents stayed signed out and its numbers
off. Find them, read-only, on the platform login (`psql` as
`oracle_platform_login`):

```sql
BEGIN READ ONLY;
SELECT set_config('app.current_role', 'platform_admin', true);
SELECT o.id AS operation_id, o.tenant_id, o.requested_at, o.error,
       jsonb_array_length(COALESCE(o.progress->'freeze'->'signed_out_user_ids', '[]')) AS users_frozen,
       jsonb_array_length(COALESCE(o.progress->'freeze'->'routes'->'telephony_routes', '[]'))
     + jsonb_array_length(COALESCE(o.progress->'freeze'->'routes'->'messaging_routes', '[]')) AS routes_frozen
  FROM privacy_operations o
  JOIN tenants t ON t.id = o.tenant_id
 WHERE o.kind = 'closure' AND o.state = 'blocked_legal_hold'
   AND t.lifecycle_state = 'active'
 ORDER BY o.requested_at;
ROLLBACK;
```

To repair one:
1. Tell the brokerage owner first. They asked for the brokerage back as it
   was when they withdrew; anyone they now want kept off can be deactivated
   in Team afterwards.
2. Run the repair below for that operation, in one transaction:
   `psql -v op=<operation_id> -v tenant=<tenant_id> -v operator=<your name>`.
   It restores only the users and routes listed in that operation's freeze
   list that are still off, then finishes the operation `cancelled` with
   `withdrawn_from`, as the withdrawal does now. If the re-check does not show
   `active | blocked_legal_hold`, `ROLLBACK` and stop. If a route update fails
   on a uniqueness constraint, the number has been reused since: `ROLLBACK`,
   add `AND id <> '<that route id>'` to that statement, run it again and tell
   the owner which number stays off.
3. Read the counts each `UPDATE` reports before `COMMIT`. Each statement
   acts only while that operation is still `blocked_legal_hold` and the
   brokerage `active`, so a second run changes nothing. The legal hold is not
   touched.

```sql
BEGIN;
SELECT set_config('app.current_role', 'platform_admin', true),
       set_config('app.current_tenant', :'tenant', true),
       set_config('app.current_agent', :'operator', true);
-- Re-check under lock: expect active | blocked_legal_hold, else ROLLBACK.
SELECT t.lifecycle_state, o.state
  FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
 WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.kind = 'closure'
   FOR UPDATE;
UPDATE users SET is_active = true, updated_at = now()
 WHERE tenant_id = :'tenant' AND NOT is_active
   AND id::text IN (SELECT jsonb_array_elements_text(COALESCE(o.progress->'freeze'->'signed_out_user_ids', '[]'))
                      FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
                     WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.state = 'blocked_legal_hold'
                       AND t.lifecycle_state = 'active');
UPDATE telephony_routes SET active = true, updated_at = now()
 WHERE tenant_id = :'tenant' AND NOT active
   AND id::text IN (SELECT jsonb_array_elements_text(COALESCE(o.progress->'freeze'->'routes'->'telephony_routes', '[]'))
                      FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
                     WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.state = 'blocked_legal_hold'
                       AND t.lifecycle_state = 'active');
UPDATE messaging_routes SET active = true, updated_at = now()
 WHERE tenant_id = :'tenant' AND NOT active
   AND id::text IN (SELECT jsonb_array_elements_text(COALESCE(o.progress->'freeze'->'routes'->'messaging_routes', '[]'))
                      FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
                     WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.state = 'blocked_legal_hold'
                       AND t.lifecycle_state = 'active');
UPDATE privacy_operations
   SET state = 'cancelled', completed_at = now(), updated_at = now(),
       result = result || jsonb_build_object('withdrawn_from', 'blocked_legal_hold',
                                             'repaired_by', :'operator', 'repair', 'L17-58')
 WHERE id = :'op' AND tenant_id = :'tenant' AND state = 'blocked_legal_hold'
   AND EXISTS (SELECT 1 FROM tenants WHERE id = :'tenant' AND lifecycle_state = 'active');
COMMIT;
```

**Subscription renewal is confirmed, not assumed (external audit 22.1).**
Closing and withdrawing each queue a `privacy:billing_renewal` job in their
own transaction. The job reads `tenants.lifecycle_state` when it runs
(`closing`/`erasing`/`erased`: stop renewal; `active`: renew), so a close
followed by a withdrawal ends on the owner's last action whatever order the
jobs run in. It sets `cancel_at_period_end` and reads it back with
`Subscription.retrieve`. The request tries once inline; the response says
`billing: confirmed | pending | no_subscription`, and Account & data shows the
same from `billing_renewal` (on `GET /api/privacy/lifecycle`), never an
unconditional "will not renew". A Stripe outage, recovery mode or a worker
without `STRIPE_SECRET_KEY` (`stripe_not_configured` with a live
subscription) is retried for about 2.4 hours (12 attempts). The result is
recorded on the brokerage's **latest** closure operation, whatever its state
(for a closure an erasure left `blocked_legal_hold` and the owner then
withdrew, that blocked operation), and only that record counts for the alert:
an older closure's `pending` is marked `superseded` as soon as a newer result
is recorded, and an older `divergent` once Stripe is confirmed to match. The
last attempt records `progress.billing.state = 'divergent'` (with the
`desired` direction kept even when the attempt itself raised), emits
`closure.billing_divergent`, and the `external_cleanup` alert fires. An
unreadable `tenants` row is retried like any unconfirmed change, never
counted as done. To resolve it:
1. Read the latest closure operation's `progress.billing` (`desired`, `detail`).
2. Set the subscription in the Stripe dashboard: *Cancel at period end* for
   `stop_renewal`, or resume it for `renew`.
3. The divergence clears when a later billing job confirms Stripe matches
   (the owner closing or reopening again queues one). Otherwise set
   `progress.billing.state` to `superseded` on that operation (platform
   login); the alert closes on its own.
Erasure's provider phase sets and reads back the renewal too: anything but a
confirmed stop (or no subscription) fails that step.

**Pending with nothing retrying it.** `external_cleanup` also fires
(`detail.stuck_pending`) when an offboarding is still `running`, or a closure's
`progress.billing.state` still `pending`, 6 hours on: its job ended without
its final attempt recording anything (a lease reaped after a crash, or a
worker without the handler during a deploy, `NO_JOB_HANDLER`). Find the job
(`automation_jobs`, `job_type` `privacy:revoke_credentials` with the
operation id in its payload, or `privacy:billing_renewal` for the
brokerage), and queue it again on the platform login (`state='queued'`,
`attempt_count=0`, `next_retry_at=NULL`).

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
