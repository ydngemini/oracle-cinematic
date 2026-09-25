# Neoh — Disaster Recovery State Map

What Neoh keeps, where the truth lives, and what a restore actually gets back.

Every number here was measured against the live database on 2026-09-24, not
estimated. Where something is unproven, it says so.

---

## The two findings that matter most

**1. A perfect backup can restore to empty.**

26 columns across 18 tables hold ciphertext — leads, clients, contracts, chat,
calls, intake. They are encrypted with a key derived from
`ORACLE_ENCRYPTION_MASTER_KEY`, an environment variable that is deliberately
never written to the database, and therefore never in a backup.

`oracle_decrypt()` returns **NULL when the key is wrong or missing — it does not
raise**. Demonstrated on the restored drill database:

```
SELECT oracle_decrypt(oracle_encrypt('real-data','right-key'), 'wrong-key');
→ <NULL>
```

So a restore without the key succeeds, passes every row count, and hands back a
database where the business data reads as *blank* rather than as *encrypted*.
There is no error to notice.

**The master key is therefore a backup artifact in its own right.** It must be
escrowed separately, with the same seriousness as the dump, and a restore is not
verified until a decrypt round trip has been demonstrated against it.
`scripts/verify-restore.sh` refuses to claim verification without that.

**2. 98% of the database is not worth protecting at the same RPO as the rest.**

| Table | Rows | Size | Class |
|---|---:|---:|---|
| `leads` | 10,273,553 | 29 GB | **C** (bulk) / **A** (9 rows) |
| `public_property_records` | 10,651,309 | 17 GB | C |
| `di_cache` | — | 426 MB | C |
| everything else | — | **207 MB** | A |

Of those 10.27M leads, **two are under contract and three have a client
attached**. The rest are `draft` rows from the public-records harvester carrying
machine-generated underwriting. `public_property_records` is harvested open
data. `di_cache` is a provider-response cache.

The authoritative business state is ~207 MB. Backing up 47 GB nightly to
protect it makes every restore hours long and buys nothing.

---

## Classification

### A — Authoritative. Must restore.

| System | Source of truth | Backup | Recovery | Acceptable loss |
|---|---|---|---|---|
| tenants, users, roles | PostgreSQL | DO daily + `backup-postgres.sh` | `restore-postgres.sh` | none |
| clients, contacts | PostgreSQL | same | same | none |
| contracted/claimed leads (9 rows) | PostgreSQL | authoritative-row extract | applied mid-restore | none |
| audit ledger | PostgreSQL | same | same | none — regulatory |
| consent / TCPA state | PostgreSQL | same | same | **none — losing this means contacting someone who opted out** |
| subscriptions, billing rows | PostgreSQL + Stripe | same | reconcile against Stripe | Stripe is authoritative for money |
| provider configuration | PostgreSQL | same | same | none |
| AI durable memory, chat | PostgreSQL (ciphertext) | same | **needs master key** | none |
| call transcripts, voicemail | PostgreSQL (ciphertext) | same | **needs master key** | none |
| MLS feed entitlements | PostgreSQL | same | same | none — gates licensed data |

### B — Durable object storage. Must preserve.

Documents, contracts, uploaded media, recordings, 3D captures, splats,
floorplans, generated files — DigitalOcean Spaces.

**Spaces versioning is OFF by default and can only be enabled through the API**
(`aws s3api put-bucket-versioning`), and only against the *regional* endpoint;
the origin endpoint fails. Lifecycle rules support time-based expiration and
multipart cleanup only — no tag-based rules. **Object Lock / WORM is not
documented as supported**; the API reference lists the headers but no page
documents enabling it, so treat it as unavailable until tested.

Backups must live in a **separate private bucket** from property media. Mixing
database dumps into a bucket that serves public listing photos is how a backup
becomes public.

### C — Rebuildable. Must not drive a database restore.

`public_property_records`, `di_cache`, and the ~10.27M draft leads.

**"Rebuildable" is not "free."** Re-harvesting is hours of work and provider
quota, not a restore. It belongs in the RTO as a separate, slower track — see
below.

### D — Ephemeral. Expected to be lost.

Live WebSocket state, in-flight voice buffers, advisory locks, short-lived
session claims.

### E — External source of truth. Reconcile, never restore.

Stripe (money), Plivo/Telnyx (calls, messages, numbers), Google, MLS providers.
Restoring a database does not restore these; it creates a database whose beliefs
about them are stale. See "Reconciliation" below.

---

## Valkey / Redis — audited, not assumed

The spec says not to assume Redis can be discarded. It cannot, uniformly:

| Use | Module | On loss |
|---|---|---|
| Plivo live call state | `plivo_call_handler.py` | **Fails closed.** Raises `PlivoCallStateUnavailable`; calls are refused rather than mishandled. |
| Rate limiting | `rate_limit_middleware.py` | **Degrades.** Falls back to per-process in-memory limiting — so with `instance_count: 2` the effective limit doubles. Not fail-open, but quantifiably weaker. |
| Data-integration cache | `data_integrations/cache.py` | Rebuildable; PostgreSQL is the fallback. |

Valkey holds no authoritative state. Losing it drops in-flight calls and
loosens rate limits; it does not lose business data.

---

## RPO / RTO

Provider capability and Neoh's target, stated separately. No zero-loss claim is
made anywhere it is not guaranteed.

### PostgreSQL

| | Value | Source |
|---|---|---|
| DO automated backups | daily, **7-day retention** | DO docs |
| DO PITR window | **7 days**, no tier variation documented | DO docs |
| DO worst-case loss | **~5 minutes** — WAL ships every 5 min | DO docs |
| DO backups downloadable? | **No.** Restore only into a *new* DO cluster, in the same account | DO support doc |
| **Neoh RPO target** | **24 h** for class A via own logical backup; ~5 min via DO PITR within 7 days | |
| **Neoh RTO target (class A)** | **< 30 min** | measured below |
| **Neoh RTO (class C)** | **hours — a re-harvest, not a restore** | not yet measured |

The 7-day retention is why `scripts/backup-postgres.sh` exists. DO covers "the
cluster died." It does not cover corruption found on day eight, and it cannot
restore to a laptop, a staging cluster, or another cloud. Its backups are not
a copy of the data that Neoh possesses.

A DO restore **creates a new cluster** — it cannot restore in place, and
standby/read-only nodes are not carried over and must be re-added by hand. That
is the dominant term in production RTO, not the data load.

### Object storage

RPO 0 with versioning enabled; **unbounded without it, and it is off by
default.** RTO minutes per object, unmeasured in bulk.

### Application

DO App Platform retains the **10 most recent successful deployments** and can
roll back to any of them. Rollback restores code, config and app spec; it does
**not** touch database data. After a rollback the app is **pinned** — no further
deploys until the rollback is committed or reverted.

---

## Measured drill — 2026-09-24

Real backup, real restore into a genuinely empty PostgreSQL 16 cluster, real
verification. Not a simulation.

| Step | Result |
|---|---|
| Backup (`authoritative` scope, 47 GB source) | **152 s**, 24.6 MB artifact |
| Restore into clean cluster | **10 s** |
| Verification | **17/17 checks passed** |
| Migration head after restore | `0111_mls_drop_unfed_columns.sql` |
| RLS after restore | 134 tables, **FORCE** on all 134, 146 policies |
| Ciphertext columns | 26, round trip demonstrated |
| Data-loss window in test | **0 rows** against the scope's stated contract |

**Not measured: a full 47 GB restore.** The host disk is at 100% (524 MB free),
so no full dump could be written. Class-C RTO is therefore an estimate, and is
stated as unmeasured rather than guessed.

### What the drill found that review had not

**The scoped backup was referentially incoherent.** Excluding `leads` data while
keeping the 13 tables that carry a `lead_id` foreign key meant `pg_restore`
loaded the children against an empty parent table. 580 rows — 567
`property_media`, 7 `reconstruction_jobs`, 5 `interaction_logs`, 2
`client_portals`, 1 `live_call_sessions` — were rejected by their foreign keys.
pg_restore reported the errors and carried on, so the restore **"completed"**
having silently dropped real call sessions and portals.

Two fixes, both in the scripts now:

1. The authoritative row set is **closed under foreign keys**, derived from
   `pg_constraint` rather than a hand-written table list, so a child table added
   later is covered without anyone remembering.
2. The restore **interleaves**: `pre-data` → authoritative rows → `data` →
   `post-data`. Applying the extract after the dump cannot work, because the
   children load during the dump.

**A manifest parser that returns empty is more dangerous than one that fails.**
The stock `postgres` image has neither `jq` nor `python3`. The first parser
returned `""` for every field — and an empty checksum compares equal to an empty
checksum, so the integrity check would have *passed*. Now: jq, then python3,
then sed, and a hard abort if a required key reads empty.

**Nested manifest keys defeat the fallback.** `source_rows.leads` and
`expected_after_restore.leads` both end in `leads`; sed matched the first and
reported 10,273,553 where 9 was correct. The manifest is now flat and unique —
a DR artifact should be parseable by the dumbest tool available.

### A ledger that had quietly stalled

The drill's first manifest recorded migration head `0107` while the schema was
at `0111`. Migrations 0108–0111 were applied but never recorded.

Worse, the reconciler could not fix it: it probes for objects a migration
creates, and 0111 *drops* four objects 0110 created — so 0110 read as PARTIAL
forever and could never be recorded. `run_migrations.py --reconcile` is now
drop-aware:

- declarations a **later** migration drops are excluded from the probe;
- a **pure-DROP** migration is verified by confirming those objects are absent,
  recorded with wording that says absence is weaker evidence than presence.

Result: 111 recorded, head `0111`, **0 pending, 0 unverifiable, 0 partial**.

This matters for DR specifically: the ledger head is what every manifest
records and what every restore is validated against. A stalled head means a
restore verifies *green* against the wrong schema version.

---

## Validation drill — 2026-09-24

`scripts/dr-drill.sh` performs the recovery rather than reviewing it:

    seed → assert → back up → destroy → restore → validate → isolate → prove access

Everything runs in disposable containers the script creates and destroys. It
never touches production, uses no real customer data, and contacts nothing —
the fixture's numbers are in the reserved `+1555` fictional range and the
Stripe ids are literals. It runs the **real** backup/restore/verify scripts,
because the point is to find out whether those work.

**Result: 43/43, repeatable, ~25 seconds.**

| | |
|---|---|
| Fixture | 2 brokerages, 4 users, 5 contacts, accepted + pending invitation, lead + child media row, MLS feed + entitlement, subscription, telephony/messaging routes |
| Disaster | A's contacts deleted, subscription corrupted to `canceled`, media deleted, pending invitation deleted |
| Backup | ~1 s, 852 KB |
| Restore into clean cluster | 5 s |
| Recovered | every assertion, including the object checksum and the subscription reverting `canceled` → `active` |
| Isolation | proven as `oracle_app_login` under RLS: A sees 3, B sees 2, neither sees 5 |

### ⚠ What the drill found: a restore the application could not use

**`pg_dump` does not dump roles.** They are cluster-level objects. The first
restore into a fresh cluster produced a database with every row, all 146
policies, FORCE RLS on 134 tables — and **none of the three roles that 566
table grants point at.** Every check that existed at the time passed. The
application could not open a connection, let alone read a table.

Three fixes, all now covered by tests:

1. `backup-postgres.sh` captures role definitions and memberships, **without
   passwords** — `pg_dumpall --roles-only` embeds the SCRAM verifier for every
   login role, and a backup artifact gets copied into buckets and incident
   channels. The operator sets the password from the secret store afterwards;
   the restore says so and the verifier warns until it is done.
2. Neither script passes `--no-privileges` any more. The original reasoning —
   "migration 0003 is the authority on grants" — was wrong in one specific way:
   **migrations do not re-run on a restore.** The drill then found the fix had
   been applied to the backup side only, so the dump carried 350 ACL entries
   and `pg_restore` discarded all of them.
3. `restore-postgres.sh` applies roles **before** anything else, because a
   GRANT to a role that does not exist is an error — 566 times over.

Re-verified against the real 47 GB database: 4 roles, 566 grants, 19/19 checks,
restore 13 s.

### Two drill bugs worth keeping

**`grep -c` prints `0` and also exits 1**, so the idiomatic `|| echo 0` appends
a second zero and yields the two-line string `"0\n0"` — which `[` rejects, and
the drill reported a failure whose entire message was `0`.

**A drill that cannot be run repeatedly is not a drill.** `docker rm -f` leaves
a container's anonymous volume behind, and the postgres image declares its data
directory as one: ~300 MB stranded per run. Twenty runs took the host from
2.2 GB free to **35 MB**, at which point no container could start and every
assertion came back empty. Every removal now passes `-v`.

**Testing isolation with an invented role proves less and misleads more.** A
`drill_app` with blanket `SELECT` still cannot execute `app_current_tenant()`,
because 0003 revokes EXECUTE from PUBLIC — so the policy errored and the
failure read as a cross-tenant leak. The drill now uses `oracle_app_login`,
which is both correct and the role that actually matters.

## Recovery mode

A restored Neoh is a *complete* Neoh — same Telnyx key, same Plivo credentials,
same Stripe key, same SMTP settings, because they came from the same
environment. Start it to check the data and its scheduler will text real clients
about deals that may already have closed.

`ORACLE_RECOVERY_MODE=1` blocks every outbound side effect at once: 25 provider
egress methods, SMTP, both Stripe mutation paths, and the periodic scheduler
(which it outranks — it is checked *before* `ORACLE_SCHEDULER_ENABLED`).

It fails closed: anything set and not recognisably "off" counts as ON, so
`ORACLE_RECOVERY_MODE=ture` protects the customer.

The guard is on the **egress methods**, not the provider factory, because
`messaging_api.py` constructs `TelnyxMessagingProvider()` directly and a factory
guard would have had an invisible hole in it. `tests/test_recovery_mode.py`
asserts by AST that every method in `MUST_BE_GUARDED` calls it.

**Always set it before pointing an application at a restored database.**

---

## Reconciliation after recovery

Restoring the database does not restore the world. It produces a system whose
beliefs are as of the backup.

| Provider | Authoritative for | After restore |
|---|---|---|
| Stripe | subscription + payment state | Stripe wins. Re-read subscription status; never replay charges. |
| Plivo / Telnyx | numbers, hosted orders, campaigns, in-flight calls | Provider wins. In-flight calls are lost (class D). Re-verify number ownership before sending. |
| MLS feeds | listing data | Re-sync. `backfill_complete=false` makes health report BACKFILLING rather than READY, so a partial feed cannot read as complete. |
| Google | calendar, maps | Re-authorise if tokens were lost. |

---

## Still manual

- Escrowing `ORACLE_ENCRYPTION_MASTER_KEY` outside the database.
- Enabling Spaces versioning (API-only, regional endpoint).
- Creating the private backup bucket and its lifecycle policy.
- Uploading backups off-host (they are currently written locally only).
- A full-scale 47 GB restore timing.
- DigitalOcean PITR is operator-run by design — see §53 of the brief: never
  auto-restore production.

---

See [[Neoh_MLS_Production]] for the feed-health states referenced above, and
`docs/deploy-digitalocean.md` for the platform this assumes.
