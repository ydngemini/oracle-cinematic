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

Reinstating restores the login. Cancelled work stays cancelled, and
forwarding has to be set up again. A suspension leaves the agent's numbers on
(forwarding cleared), so there is nothing else to restore, except numbers a
closure withdrawal left off because of this member: those come back on with
the reinstatement (L17-59, see section 5, *During the grace period*).

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
  else (the response counts `restored_users` and `restored_routes`), except
  members the owner has suspended (or offboarded) since the closure and the
  numbers they now hold: those stay off and are listed in `skipped_users`
  (id, reason and, in the response only, the name Team shows) and
  `skipped_routes`, also on the operation's `result` (ids and reasons, no
  names). Account & data lists them after the withdrawal and points the owner
  to Team to reinstate anyone they want back. Since L17-59, a reinstatement
  gives back the sign-in and, in the same transaction, switches back on the
  numbers a withdrawal left off for that member: routes listed in one of the
  brokerage's closures' `skipped_routes`, still held by that member (agent id,
  any case) and still off. The answer lists `restored_routes` (ids) and
  `routes_not_restored` (id, `call`/`text`, reason); the audit event counts
  both. A number is left off, and reported, when the member was offboarded
  since that closure (`offboarded`: offboarding parks a number, and a parked
  number stays parked), when its text route was disconnected
  (`disconnected`), while the brokerage is closing again
  (`brokerage_not_active`), or when a uniqueness constraint refuses it
  (`number_in_use`: its inbound number is active on another route now, possibly
  in another brokerage). A refused number never stops the reinstatement. A
  plain suspension switches no number off, so its reinstatement has nothing
  to restore. It happens once per withdrawal: the first reinstatement that
  restores or refuses a number records its id on that closure's
  `result.reinstate_consumed`, and a recorded number is never switched on
  again from that closure (the agent may since have switched it off on
  purpose). Only `brokerage_not_active` records nothing.
  Cancelled jobs and revoked links are not restored. Renewal is turned back
  on through the same durable job.

**Under a legal hold.** A hold stops the erasure when the grace period ends:
the brokerage stays `closing` and its closure operation becomes
`blocked_legal_hold`, so the owner can still keep the brokerage. The
withdrawal restores what that closure switched off (with the same
exception) and ends the operation `cancelled`, with
`result.withdrawn_from = blocked_legal_hold` and its `error`
(`legal hold <id>`) kept. The hold itself stays until an operator releases it.
Once the hold is released, the next erasure finishes that closure `succeeded`,
with `result.erasure_started_from = blocked_legal_hold` and the error kept
(L17-58).

*Closures an older withdrawal left behind.* Before L17-58, withdrawing a
blocked closure reopened the brokerage but restored nothing and left the
operation `blocked_legal_hold`: its agents stayed signed out and its numbers
off. The brokerage has been `active` since, so the owner may have offboarded
or suspended some of those agents in the meantime; nothing they switched off
may come back. All SQL here runs on the platform login (`psql` as
`oracle_platform_login`); without its `set_config` line the tables read empty.

1. Find the leftovers (read-only):

```sql
BEGIN READ ONLY;
SELECT set_config('app.current_role', 'platform_admin', true);
SELECT o.id AS operation_id, o.tenant_id, o.requested_at, o.error
  FROM privacy_operations o
  JOIN tenants t ON t.id = o.tenant_id
 WHERE o.kind = 'closure' AND o.state = 'blocked_legal_hold'
   AND t.lifecycle_state = 'active'
 ORDER BY o.requested_at;
ROLLBACK;
```

2. For one of them, list what its closure switched off, with what has
   happened since (read-only; `psql -v op=<operation_id> -v tenant=<tenant_id>`).
   A member is flagged *offboarded since the closure* (an offboarding
   operation, or the latest Team audit event is an offboarding), *suspended
   since the closure* (their membership is `suspended`, or the latest Team
   audit event is a suspension) or *changed since the closure* (their user
   row was updated after it). A route is flagged when the member now holding
   it is flagged, or when it was changed after the closure (offboarding parks
   a number; suspension clears its forwarding). Rows already on need nothing.

```sql
BEGIN READ ONLY;
SELECT set_config('app.current_role', 'platform_admin', true);
WITH c AS (SELECT o.requested_at AS since, o.progress->'freeze' AS f
             FROM privacy_operations o
            WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.kind = 'closure'),
     members AS (
       SELECT u.id, u.agent_id, u.is_active, u.updated_at > c.since AS changed,
              EXISTS (SELECT 1 FROM privacy_operations p
                       WHERE p.tenant_id = u.tenant_id AND p.kind = 'offboard'
                         AND p.subject_ref = u.id::text AND p.requested_at > c.since) AS offboard_op,
              EXISTS (SELECT 1 FROM team_memberships m
                       WHERE m.tenant_id = u.tenant_id AND m.user_id = u.id AND m.status = 'suspended') AS membership_suspended,
              (SELECT a.action FROM audit_ledger a
                WHERE a.tenant_id = u.tenant_id AND a.target_id = u.id AND a.created_at > c.since
                  AND a.action IN ('team.member.suspended', 'team.member.reinstated', 'team.member.offboarded')
                ORDER BY a.created_at DESC LIMIT 1) AS last_team_event
         FROM users u, c
        WHERE u.tenant_id = :'tenant'
          AND u.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'signed_out_user_ids', '[]')))),
     flagged AS (
       SELECT id, lower(agent_id) AS agent,
              CASE WHEN offboard_op OR last_team_event = 'team.member.offboarded' THEN 'offboarded since the closure'
                   WHEN membership_suspended OR last_team_event = 'team.member.suspended' THEN 'suspended since the closure'
                   ELSE 'changed since the closure' END AS flag
         FROM members
        WHERE NOT is_active
          AND (offboard_op OR membership_suspended OR changed
               OR last_team_event IN ('team.member.suspended', 'team.member.offboarded')))
SELECT 'user' AS kind, m.id::text AS id, NOT m.is_active AS still_off, f.flag
  FROM members m LEFT JOIN flagged f ON f.id = m.id
UNION ALL
SELECT 'telephony_route', r.id::text, NOT r.active,
       CASE WHEN r.active THEN NULL
            WHEN lower(r.agent_id) IN (SELECT agent FROM flagged) THEN 'held by a member switched off since the closure'
            WHEN r.updated_at > c.since THEN 'changed since the closure' END
  FROM telephony_routes r, c
 WHERE r.tenant_id = :'tenant'
   AND r.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'routes'->'telephony_routes', '[]')))
UNION ALL
SELECT 'messaging_route', r.id::text, NOT r.active,
       CASE WHEN r.active THEN NULL
            WHEN lower(r.agent_id) IN (SELECT agent FROM flagged) THEN 'held by a member switched off since the closure'
            WHEN r.updated_at > c.since THEN 'changed since the closure' END
  FROM messaging_routes r, c
 WHERE r.tenant_id = :'tenant'
   AND r.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'routes'->'messaging_routes', '[]')))
 ORDER BY 1, 2;
ROLLBACK;
```

3. Agree the list with the brokerage owner: the ids, among those still off
   and unflagged, that they want back. A flagged member is never restored
   here: if the owner wants one back, they reinstate them in Team (which
   checks seats and records it). Leave a flagged number off and record it in
   the incident. The repair does not write `skipped_routes`, so a later Team
   reinstatement does not switch those numbers on (L17-59 restores only what
   a withdrawal recorded); if the owner wants one back after reinstating, it
   is a support case. If the owner wants nobody back, the list is empty (step 4,
   `finish=none`).

4. Preview, then repair, with exactly that list:
   `psql -v ON_ERROR_STOP=1 -v op=<operation_id> -v tenant=<tenant_id> -v ids=<id,id,...> -v operator=<your name>`,
   first **without** `finish`, then again with `-v finish=yes`. The script is
   safe to run with `psql -f`: its closing `COMMIT` keeps only what the one
   gated statement did, and that statement changes nothing unless `finish`
   is exactly `yes` or `none` and its condition holds.
   - **Preview** (no `finish`, or any other value): nothing changes. It
     returns `outcome` `preview: nothing changed`, how many members
     (`users`) and routes (`telephony_routes`, `messaging_routes`) the list
     would switch on, and `not_restorable`: the ids in the list that would
     not be (not in that closure's freeze list, already on, flagged, or
     mistyped). The leftover stays in step 1's list.
   - **`-v finish=yes`**: switches on the listed ids and finishes the
     operation `cancelled` (with `withdrawn_from`, `repaired_by`, `finish` and
     the ids it restored, the error kept), in the same statement, only when
     the list has at least one id and **every** id in it is restorable (the
     preview's `not_restorable` is empty). Otherwise it changes nothing
     (`outcome` `refused: nothing changed`, with `not_restorable`): an empty,
     mistyped or partly mistyped list never closes the case. Fix the list
     with the owner and run it again.
   - **`-v finish=none -v ids=`** (an empty list): the owner wants nobody back.
     It finishes the operation `cancelled` restoring nothing. With any id in
     the list it changes nothing.

   An id is restorable when it is in that closure's freeze list, still off
   and unflagged (the same flags as step 2, computed again inside the
   statement). Everything requires the operation still `blocked_legal_hold`
   and the brokerage `active`, so once it is finished a re-run changes
   nothing (every id is then `not_restorable`). The first `SELECT` shows the
   re-check under lock; anything but `active | blocked_legal_hold` means stop.
   - If the `finish=yes` run fails with `seat limit reached` (SQLSTATE
     `NSEAT`, the `users_seat_limit` trigger), nothing changed: the
     brokerage's plan has no seat for everyone in the list (a preview cannot
     see this). The owner frees a seat or raises the plan, or confirms a
     shorter list; run it again.
   - If a route fails on a uniqueness constraint, the number has been reused
     since: nothing changed; take that id out of the list and tell the owner.
   - The repair writes no `audit_ledger` row. Do not insert one by hand: the
     ledger is hash-chained, and a hand-written row breaks verification.
     The record is the operation's `result` (`repaired_by`, `finish`,
     `restored`) and the incident record. The legal hold is not touched.

```sql
\if :{?finish}
\else
\set finish preview
\endif
BEGIN;
SELECT set_config('app.current_role', 'platform_admin', true),
       set_config('app.current_tenant', :'tenant', true),
       set_config('app.current_agent', :'operator', true);
-- Re-check under lock: expect active | blocked_legal_hold, else stop.
SELECT t.lifecycle_state, o.state
  FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
 WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.kind = 'closure'
   FOR UPDATE;
WITH c AS (SELECT o.id, o.requested_at AS since, o.progress->'freeze' AS f
             FROM privacy_operations o JOIN tenants t ON t.id = o.tenant_id
            WHERE o.id = :'op' AND o.tenant_id = :'tenant' AND o.kind = 'closure'
              AND o.state = 'blocked_legal_hold' AND t.lifecycle_state = 'active'),
     confirmed AS (SELECT DISTINCT btrim(x) AS id FROM unnest(string_to_array(:'ids', ',')) AS x
                    WHERE btrim(x) <> ''),
     members AS (
       SELECT u.id, u.agent_id, u.is_active, u.updated_at > c.since AS changed,
              EXISTS (SELECT 1 FROM privacy_operations p
                       WHERE p.tenant_id = u.tenant_id AND p.kind = 'offboard'
                         AND p.subject_ref = u.id::text AND p.requested_at > c.since) AS offboard_op,
              EXISTS (SELECT 1 FROM team_memberships m
                       WHERE m.tenant_id = u.tenant_id AND m.user_id = u.id AND m.status = 'suspended') AS membership_suspended,
              (SELECT a.action FROM audit_ledger a
                WHERE a.tenant_id = u.tenant_id AND a.target_id = u.id AND a.created_at > c.since
                  AND a.action IN ('team.member.suspended', 'team.member.reinstated', 'team.member.offboarded')
                ORDER BY a.created_at DESC LIMIT 1) AS last_team_event
         FROM users u, c
        WHERE u.tenant_id = :'tenant'
          AND u.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'signed_out_user_ids', '[]')))),
     flagged AS (
       SELECT id, lower(agent_id) AS agent FROM members
        WHERE NOT is_active
          AND (offboard_op OR membership_suspended OR changed
               OR last_team_event IN ('team.member.suspended', 'team.member.offboarded'))),
     users_ok AS (
       SELECT m.id FROM members m
        WHERE NOT m.is_active AND m.id NOT IN (SELECT id FROM flagged)
          AND m.id::text IN (SELECT id FROM confirmed)),
     voice_ok AS (
       SELECT r.id FROM telephony_routes r, c
        WHERE r.tenant_id = :'tenant' AND NOT r.active AND r.updated_at <= c.since
          AND r.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'routes'->'telephony_routes', '[]')))
          AND r.id::text IN (SELECT id FROM confirmed)
          AND lower(r.agent_id) NOT IN (SELECT agent FROM flagged)),
     sms_ok AS (
       SELECT r.id FROM messaging_routes r, c
        WHERE r.tenant_id = :'tenant' AND NOT r.active AND r.updated_at <= c.since
          AND r.id::text IN (SELECT jsonb_array_elements_text(COALESCE(c.f->'routes'->'messaging_routes', '[]')))
          AND r.id::text IN (SELECT id FROM confirmed)
          AND lower(r.agent_id) NOT IN (SELECT agent FROM flagged)),
     not_restorable AS (
       SELECT id FROM confirmed
        WHERE id NOT IN (SELECT id::text FROM users_ok UNION ALL SELECT id::text FROM voice_ok
                         UNION ALL SELECT id::text FROM sms_ok)),
     go AS (
       SELECT EXISTS (SELECT 1 FROM c)
              AND ((:'finish' = 'yes' AND EXISTS (SELECT 1 FROM confirmed)
                    AND NOT EXISTS (SELECT 1 FROM not_restorable))
                   OR (:'finish' = 'none' AND NOT EXISTS (SELECT 1 FROM confirmed))) AS ok),
     users_on AS (
       UPDATE users u SET is_active = true, updated_at = now()
        WHERE u.id IN (SELECT id FROM users_ok) AND (SELECT ok FROM go)
       RETURNING u.id),
     voice_on AS (
       UPDATE telephony_routes r SET active = true, updated_at = now()
        WHERE r.id IN (SELECT id FROM voice_ok) AND (SELECT ok FROM go)
       RETURNING r.id),
     sms_on AS (
       UPDATE messaging_routes r SET active = true, updated_at = now()
        WHERE r.id IN (SELECT id FROM sms_ok) AND (SELECT ok FROM go)
       RETURNING r.id),
     done AS (
       UPDATE privacy_operations o
          SET state = 'cancelled', completed_at = now(), updated_at = now(),
              result = o.result || jsonb_build_object(
                'withdrawn_from', 'blocked_legal_hold', 'repaired_by', :'operator', 'repair', 'L17-58',
                'finish', :'finish',
                'restored', jsonb_build_object(
                  'users', COALESCE((SELECT jsonb_agg(id) FROM users_on), '[]'),
                  'telephony_routes', COALESCE((SELECT jsonb_agg(id) FROM voice_on), '[]'),
                  'messaging_routes', COALESCE((SELECT jsonb_agg(id) FROM sms_on), '[]')))
         FROM c WHERE o.id = c.id AND (SELECT ok FROM go)
       RETURNING o.id)
SELECT CASE WHEN (SELECT ok FROM go) THEN 'repaired and finished'
            WHEN :'finish' IN ('yes', 'none') THEN 'refused: nothing changed'
            ELSE 'preview: nothing changed' END AS outcome,
       (SELECT count(*) FROM users_ok) AS users,
       (SELECT count(*) FROM voice_ok) AS telephony_routes,
       (SELECT count(*) FROM sms_ok) AS messaging_routes,
       (SELECT string_agg(id, ',' ORDER BY id) FROM not_restorable) AS not_restorable,
       (SELECT count(*) FROM users_on) AS users_restored,
       (SELECT count(*) FROM voice_on) AS telephony_routes_restored,
       (SELECT count(*) FROM sms_on) AS messaging_routes_restored,
       (SELECT count(*) FROM done) AS operation_finished;
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
