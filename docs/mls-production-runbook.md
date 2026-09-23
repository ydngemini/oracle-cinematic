# MLS production runbook

What an operator needs to connect a real board feed, verify it, and know when
something is wrong — without SSH and SQL.

## The blocker, stated plainly

**Neoh has no licensed MLS feed today.** Everything below the licensing step is
built and tested; the licensing step itself is paperwork that cannot be
automated away.

What is actually in the database right now:

| mls_id | dataset | rows | classification | newest source timestamp |
|---|---|---|---|---|
| `actris` | `actris_ref` | 52,622 | `developer_listing_dataset` | **2020-11-08** |
| `bridge_dev` | `test` | 0 | `developer_listing_dataset` | — |

`actris_ref` is a Bridge **reference** dataset: frozen sample inventory for
integration work. Its newest `ModificationTimestamp` is six years old. It was
previously stamped `licensed_property_listing` because classification inferred
"licensed" from the dataset name not being one of three known developer names.
Migration 0110 reclassified all 52,622 rows.

A brokerage connected only to this data sees:

```
MLS
Not connected — no licensed MLS feed is connected for this brokerage.
```

and every search response carries `coverage.state = "developer_data_only"`.
That is the correct answer and it is what customers will see until a board
agreement exists.

## A. Obtain licensed feed authorization  [MANUAL — cannot be automated]

1. Choose the board (e.g. Bright MLS, ACTRIS/Unlock MLS).
2. Apply through the provider. For Bridge Interactive this is a per-dataset
   approval on the Bridge dashboard; approval for one board does not grant any
   other. Real boards return **401** until approved.
3. The brokerage must usually be a member of that board, and the agreement is
   between the board and the brokerage or the platform — read which.
4. Record the agreement reference. You will need it in step C; a feed cannot be
   marked licensed without one.

## B. Configure credentials  [OPERATOR, server-side only]

DigitalOcean App Platform → the API and worker components → encrypted env vars.
Never `VITE_*`; never in the frontend bundle; never in logs.

```
ORACLE_BRIDGE_TOKEN=...            # provider token
ORACLE_BRIDGE_DATASET=brightmls    # the approved dataset slug
ORACLE_BRIDGE_MLS_ID=bright        # the mls_id rows are written under
```

## C. Declare the licence  [OPERATOR — deliberate, two switches]

```
ORACLE_MLS_LICENSED=1
ORACLE_MLS_AGREEMENT_REF=BRIGHT-2026-001
```

Both are required. `mls_licensing.classify_dataset` fails closed: a dataset is
developer data unless explicitly declared **and** an agreement is named. One
boolean is too easy to flip by accident; naming the agreement makes the
declaration auditable.

A dataset whose slug ends `_ref`, `_reference`, `_sample` or `_demo` stays
developer data **even when declared licensed** — a reference dataset is frozen
sample inventory and no agreement changes that.

## D. Grant the brokerage entitlement  [OPERATOR]

Licensed listings are visible only to tenants explicitly entitled to that feed.
Developer data stays visible to everyone, clearly labelled, so staging and
demos keep working.

```sql
INSERT INTO mls_feed_entitlements (tenant_id, mls_id, granted_by, note)
VALUES ('<tenant-uuid>', 'bright', 'ops@neoh', 'BRIGHT-2026-001');
```

Nothing a browser sends can widen this set; a request may only narrow it.

## E. Run and observe the initial backfill

The feed's health is `BACKFILLING` until the first full walk completes, and a
half-finished backfill never reports `READY` — half a board looks exactly like
a whole board to a searcher.

```sql
SELECT mls_id, health, backfill_complete, backfill_records,
       listings_synced, last_success_at, last_error_class, last_error
  FROM mls_sync_status WHERE mls_id = 'bright';
```

## F–H. Verify counts, inspect rejections

```sql
SELECT license_classification, count(*), max(source_modified_at)
  FROM oracle_mls_listings WHERE mls_id = 'bright' GROUP BY 1;

SELECT last_run->'rejected' FROM mls_sync_status WHERE mls_id = 'bright';
```

Rejections are aggregated by reason, never logged per record — a bad feed
should not produce fifty thousand identical log lines.

## I. Verify brokerage readiness

`GET /api/brokerage/setup` for a user in the entitled tenant:

```json
"capabilities": { "mls": "READY" },
"mls": { "status": "READY", "detail": "Licensed MLS feed synced and fresh.",
         "feeds": [{ "mls_id": "bright", "licensed": true, "health": "READY" }] }
```

If it says `BLOCKED`, the connected feeds are developer/reference data.
If `NOT_STARTED`, no entitlement row exists.

## J–L. Search, open a listing, check buyer matches

`GET /api/mls/search?state=DE` — check `coverage.state` is `fresh` and
`degraded` is `false`.

## M–P. Failure and recovery drill

Rotate the token to an invalid value and run a sync. Expect:

- `health = AUTH_ERROR`, `last_error_class = 'auth'` — auth failures are not
  retried aggressively; retrying a rejected credential does not fix it.
- **Cached listings remain searchable**, marked `degraded` with a staleness
  note. A provider outage must never turn into "no listings".
- Brokerage readiness moves to `ERROR`, not `NOT_STARTED` — the feed exists
  and is broken, which is a different thing from absent.

Restore the token, run a sync, confirm `health` returns to `READY`.

## Q. Token rotation

Update the secret in DigitalOcean, redeploy the worker, run one sync, confirm
`last_success_at` advances. The durable cursor is unaffected by rotation.

## R. Disable a feed

Remove the entitlement rows first (customers stop seeing it immediately), then
unset the provider credentials. Do **not** delete `oracle_mls_listings` rows:
historical listing data has value and deletion is not reversible.

```sql
DELETE FROM mls_feed_entitlements WHERE mls_id = 'bright';
```

## What is NOT built

Honest list, so nobody discovers these during an incident:

- **Media/photo rights.** Provider media URLs are stored as given. No rehosting,
  no expiry handling, no redistribution-rights check. Verify the agreement
  before displaying photos to consumers.
- **Deletion semantics.** Bridge does not expose a deletion feed on the
  endpoints in use, so a record disappearing from a delta is NOT treated as a
  delete. Listings go stale rather than vanish.
- **Scheduler leader election.** Sync is not yet guarded by a distributed lock,
  so horizontally scaled replicas could each run a backfill. Run exactly one
  worker until that lands.
- **Change events** (price drop, back on market) and Home integration.
