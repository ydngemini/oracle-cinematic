# Data retention policy

Policy version **2026-10-01.1**. This is an engineering default and is not yet
reviewed by counsel; rows marked **counsel** need a lawyer's sign-off before
launch (`docs/privacy-counsel-checklist.md`). The source of truth is
`backend/retention_policy.py`, which the code enforces. Brokerage owners can
read this schedule in the app (Settings → Account & data), and `GET
/api/privacy/policy` publishes it, as the FTC expects of a service holding
customer data (the *Blackbaud* order: written, finite, published).

## Principles

1. **The brokerage's records are the brokerage's.** Real-estate licence law
   makes the broker keep transaction records and communications (TREC 22 TAC
   §535.2: four years; Maryland: five; Delaware and Pennsylvania: three). Neoh
   therefore never deletes a brokerage's messages, transcripts, notes or chat
   on a timer of its own. They live as long as the account does, the owner
   can export them at any time, and they are erased only when the brokerage
   closes or asks.
2. **Neoh's own records follow Neoh's obligations.** Billing is kept 7 years
   (IRS). The security audit log is kept 2 years. Evidence that a privacy
   request was honoured is kept 7 years.
3. **An opt-out outlives the person.** When a contact is erased, their
   do-not-contact status survives as a keyed hash for 5 years (47 CFR
   64.1200(d)(6) internal DNC; the TCPA's 4-year limitation period, 28 USC
   1658). The hash can block a re-import but cannot be read back as a phone
   number.
4. **Nothing is "indefinite" without an owner.** "No timer" below always
   means "until the brokerage deletes it or closes", never "forever".

## Schedule

Columns: retention in days ("none" = no timer, see principle 4), what
deletion does, whether a legal hold pauses it, and the environment override.

| Category | Clock starts | Days | Deletion | Hold | Override | |
|---|---|---|---|---|---|---|
| `account` | tenant erased | 0 | erase | yes | — | |
| `business_crm` | tenant erased | 0 | erase | yes | — | |
| `contact_pii` | record deleted | 0 | erase | yes | — | |
| `communication_content` | tenant erased | none | erase | yes | `ORACLE_MESSAGE_BODY_RETENTION_DAYS` | counsel |
| `communication_metadata` | tenant erased | 0 | erase | yes | — | |
| `call_audio` | call completed | 30 | erase | yes | `ORACLE_CALL_AUDIO_RETENTION_DAYS` | counsel |
| `call_transcript` | call completed | none | erase | yes | `ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS` | counsel |
| `consent_suppression` | last event | 1826 | keyed-hash tombstone | yes | `ORACLE_CONSENT_EVIDENCE_RETENTION_DAYS` | counsel |
| `financial_billing` | account closed | 2557 | anonymise | yes | `ORACLE_BILLING_RECORD_RETENTION_DAYS` | counsel |
| `ai_chat` | tenant erased | none | erase | yes | `ORACLE_AI_CHAT_RETENTION_DAYS` | counsel |
| `ai_memory` | source deleted | 0 | erase | yes | — | |
| `derived` | source deleted | 0 | erase | yes | — | |
| `document` | tenant erased | 0 | erase | yes | — | |
| `media` | tenant erased | 0 | erase | yes | — | |
| `spatial_source` | tenant erased | 0 | erase | yes | — | |
| `spatial_derived` | source deleted | 0 | erase | yes | — | |
| `audit_security` | created | 730 | retain, then expire (min 365) | yes | `ORACLE_AUDIT_RETENTION_DAYS` | counsel |
| `secret` | user offboarded | 0 | revoke at provider, then erase | no | — | |
| `capability_token` | expired | 30 | erase | no | `ORACLE_EXPIRED_TOKEN_RETENTION_DAYS` | |
| `public_data` | created | 730 | erase raw payload | yes | `ORACLE_RAW_SOURCE_RETENTION_DAYS` | |
| `licensed_mls` | licence ended | 0 | erase | yes | — | counsel |
| `cache` | tenant erased | 0 | erase | no | — | |
| `operational` | job finished | 90 | empty the payload | no | `ORACLE_FINISHED_JOB_RETENTION_DAYS` | |
| `export_artifact` | created | 7 | erase | no | `ORACLE_EXPORT_RETENTION_DAYS` | |
| `privacy_record` | created | 2557 | retain, then delete | yes | `ORACLE_PRIVACY_RECORD_RETENTION_DAYS` | counsel |

Brokerage lifecycle windows (business policy, not law):
- `ORACLE_CLOSURE_GRACE_DAYS` (default 30): the time between a closure request and irreversible erasure. During it the owner can export or reopen.
- `ORACLE_CANCELED_ACCOUNT_ERASURE_DAYS` (default none): how long an account that cancelled billing but never asked to close stays before erasure. "none" means it is never erased automatically. Cancellation alone does not delete anyone's business records.

## What enforces each row

| Mechanism | Runs | Covers |
|---|---|---|
| `privacy_lifecycle.run_erasure` (job `privacy:erase`) | when a closing tenant's grace ends (hourly check), or an operator starts it | every `tenant_erased` / `user_offboarded` row; providers, objects, tombstones, rows, caches |
| `privacy_requests` (`POST /api/privacy/requests`) | on a brokerage's instruction | `record_deleted` for one person |
| `privacy_retention_sweep` (hourly) | `periodic.privacy_lifecycle` | capability tokens, finished jobs, tombstones, privacy records |
| `privacy_expire_audit` (hourly; floor 365 days) | same | audit ledger, keeping the hash chain verifiable through an anchor |
| `purge_expired_platform_data` (daily) | `periodic.platform_retention_cleanup` | public-data raw payloads, negotiation transcripts if a timer is set, `di_cache` |
| `expire_exports` (hourly) | same task | export archives |
| `privacy_purge_mls_feed` | operator, at licence end | licensed MLS rows (runbook §R2) |
| Provider retention | the provider | see `docs/subprocessor-inventory.md` |

Not yet enforced by code, and stated plainly:
- **Call audio.** Neoh stores no call audio. Recordings live at the carrier (Plivo deletes them after 30 days). Walkthrough audio is nulled at job end.
- **`financial_billing` anonymisation after 7 years.** No account is that old; this needs building before 2033.
- **The SQLite audit fallback.** See the data map, §7.

## Backups and restores

Managed PostgreSQL backups are kept 7 days and cannot be edited. Every
erasure receipt states the date its data leaves the backups.

After any restore, run `python scripts/reapply-erasures.py --apply` before
the service takes traffic. It re-erases every brokerage that the restore
brought back.

## Legal holds

A platform operator places a hold on counsel's instruction (`POST
/api/admin/privacy/legal-holds`). A hold can cover a whole tenant, one user,
or one contact (by keyed reference). While it stands, erasure of that scope
stops: a tenant's erasure is left in state `blocked_legal_hold`, and a
subject deletion is refused with 423. Holds are only ever released, never
deleted, and the brokerage can see that its data is under hold.
