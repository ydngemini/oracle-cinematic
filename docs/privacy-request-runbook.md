# Privacy request runbook

Who does what when someone asks Neoh, or a brokerage, about personal data.
Neoh is the brokerage's **processor**: a person's request about a brokerage's
records goes to the brokerage, which decides, and Neoh carries it out.
Requests about a Neoh account (an agent's or owner's own login, billing) are
Neoh's to answer as controller.

| Request | Who decides | Tool | Deadline to plan for |
|---|---|---|---|
| "What do you hold on me?" (a client/lead) | the brokerage | Settings → Account & data, or `POST /api/privacy/requests {kind: dsr_access}` | 45 days (CCPA/VA/TX; DE: 45, and 30 from 2027-01-01 under HB 380 — **counsel**) |
| "Delete me" (a client/lead) | the brokerage | `POST /api/privacy/requests {kind: dsr_delete}` | same |
| "Stop contacting me" | automatic | STOP keyword → `outreach_suppression`; any other channel: add a suppression | 10 business days (TCPA revocation; CAN-SPAM) — STOP is immediate |
| Export of the whole brokerage | the owner | Settings → Account & data → Export | on demand; archive kept 7 days |
| Agent leaves | the owner | Settings → Team → Offboard… (`docs/account-offboarding-runbook.md`) | same day |
| Brokerage closes | the owner | Settings → Account & data → Close | erased after the grace period (30 days) |
| A request about a Neoh account | Neoh support | platform admin | 45 days |
| Litigation / regulator hold | counsel | `POST /api/admin/privacy/legal-holds` | immediately |

## 1. A person asks a brokerage for their data (access)

1. The owner identifies the person by email and/or phone. Verifying who is
   asking is the brokerage's job; Neoh does not verify identities for it.
2. Preview: `POST /api/privacy/requests {"kind":"dsr_access","email":…,"phone":…,"reason":…,"preview":true}`.
   The response counts matches per table, and lists any broker records
   (transactions) that name the person.
3. Run it with `"preview": false`. The response contains the records:
   the client row, decrypted contact details, messages, emails, consent and
   opt-out records, and call summaries. A `privacy_operations` row records
   the request (counts only).
4. **Read the caveat aloud to the brokerage.** Discovery is by identifier.
   A name typed into a note, a chat message or a document is not found
   automatically, so the brokerage searches for those itself.

## 2. A person asks a brokerage to delete their data

1. Preview as above with `"kind":"dsr_delete"`.
2. If `retained_broker_records` is non-empty, the person appears in a
   transaction, offer or call session. **Nothing is deleted**, and the API
   answers 409. Those are records the broker must keep by law, so the
   brokerage decides with counsel. If counsel says to delete, a platform
   operator does it by hand with counsel's written instruction, recorded in
   the request.
3. Otherwise run it (`"preview": false`, with the owner's password). In one
   transaction, the person's clients and contacts are deleted along with:
   - their messages, calls, emails and consent rows;
   - their dependent tasks, intake sessions and nurture jobs.

   Their opt-out is kept as a keyed hash, so a re-import of the same number
   or address stays blocked. The receipt lists counts per table.
4. A legal hold on the tenant, or on that person, answers 423 and deletes
   nothing.
5. Tell the brokerage what stays: copies in the 7-day backups, and messages
   already delivered to phones and inboxes.

## 3. Brokerage export

The owner clicks Settings → Account & data → Prepare export and enters
their password. The job (`privacy:export`) then writes a zip to private
storage:
- one JSON-lines file per table, with encrypted PII decrypted;
- the photos and documents those rows reference, up to
  `ORACLE_EXPORT_MAX_MEDIA_BYTES`;
- a manifest with a row count and SHA-256 for every file, and every
  omission with its reason.

Passwords, provider credentials, link tokens and lookup hashes are never
included. Downloading needs the password again. The archive is deleted after
7 days (`expire_exports`). Every request and download is in the audit
ledger.

## 4. Closure and erasure (operators)

The owner's closure starts the grace period (see the offboarding runbook).
When it ends, the hourly task moves the tenant to `erasing` and queues
`privacy:erase`. The phases run in order, each checkpointed in
`privacy_operations.progress`, so a crash resumes where it stopped:

`freeze` → `providers` (release numbers, disconnect hosted SMS, revoke Google, stop Stripe renewal) → `objects` → `tombstones` → `rows` (table by table, children first, reference cycles broken through nullable columns) → `pseudonymize` → `caches` → `tenant_row` (scrubbed tombstone) → `verify` → `receipt`.

- **Start early**: when the owner asks in writing for immediate deletion, call
  `POST /api/admin/privacy/erasures/{tenant_id}/start`. This works only for a
  closing tenant.
- **Check progress**: `GET /api/admin/privacy/operations?tenant_id=…`.
- **When verification fails** (state `failed`), the receipt says why:
  - rows left in a table;
  - objects left under a prefix;
  - a provider step that failed, for example Twilio down or recovery mode
    on.

  Fix the cause and re-queue by re-running the job with the same operation
  id. Every phase is idempotent.
- **The receipt** is `privacy_operations.receipt`. It holds counts and
  statuses only, never data, and its SHA-256 is stored with it. Send it to
  the owner.

## 5. After a database restore

**Before** the restored service takes traffic:

```
python scripts/reapply-erasures.py          # lists erased brokerages the restore brought back
python scripts/reapply-erasures.py --apply  # erases them again; exit 1 if any need attention
```

It reads the erasure directives kept in object storage, which a database
restore cannot roll back. A directive for a tenant under legal hold is
skipped and reported. Also re-run any MLS purge (`docs/mls-production-runbook.md`
§R2) that is younger than the backup.

## 6. Orphaned objects

`python scripts/privacy-orphan-audit.py [--keys]` lists the objects no row
accounts for, per prefix, with the owning tenant where the key has one. It
deletes nothing.
- Objects of erased tenants can always go.
- `recon-inputs/` and `recon-outputs/` are reconstruction staging copies of
  property photos. Delete them once older than the longest running job
  (24 h).
- Anything else: find out why first.

## 7. Breach

This is not a privacy request, but the clocks start together. If customer
data may have been exposed, the notification deadlines are:
- tell the brokerage (the controller) within **10 days** (Maryland's processor
  rule is the shortest);
- Delaware residents: 60 days;
- Texas: 60 days, and the Attorney General within 30 days if 250 or more
  Texans are affected;
- New Jersey: the State Police before the residents.

`docs/security-incident-response.md` has the process. Counsel confirms each
deadline.
