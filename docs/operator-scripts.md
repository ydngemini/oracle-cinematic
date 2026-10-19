# Operator scripts — one-shot jobs that are not part of any deploy

Scripts here are run by hand, deliberately, usually once. Nothing imports them
and no deploy invokes them, which means they are invisible unless written down —
this file is that record.

Migrations are **not** in this category: they run through
`backend/run_migrations.py`, which `scripts/dev-start.sh` invokes automatically.
See `docs/production-blockers.md` for operator actions that need vendor or
billing access rather than a command.

---

## `backend/backfill_contact_search.py` — make existing contacts searchable by name

### When this is needed

Migration `0059_contact_search_and_assignment_integrity.sql` added
`agent_contacts.name_search_tokens` with a `DEFAULT ARRAY[]::text[]`. Every row
that existed before that migration therefore has **zero tokens**.

Name search (`contacts_api.py`) matches on `name_search_tokens @> $4`, so a
contact with no tokens is **permanently invisible to name search**. It is not a
degraded result — the contact simply never appears.

The live write paths tokenize on create and update only. Nothing else in the
codebase repairs an untokenized row. **This script is the only remedy.**

This particularly affects contacts seeded by
`0054_agent_contacts_and_intake.sql`, which populates `agent_contacts` from
`clients` *metadata only* — no plaintext PII is copied — leaving
`pii_ciphertext = NULL`.

### Check whether you need it

```sql
SELECT count(*) FROM agent_contacts
 WHERE deleted_at IS NULL AND cardinality(name_search_tokens) = 0;
```

`0` means the cutover is complete for that database. Run this against
**production**, not a dev database — a dev database with a handful of seeded
contacts tells you nothing about production.

### Running it

Requires `ORACLE_ENCRYPTION_MASTER_KEY` (it decrypts `pii_ciphertext` to derive
the tenant-keyed blind index) plus the usual `ORACLE_DB_*` connection vars.

```bash
docker compose run --rm --no-deps \
  -e ORACLE_ENCRYPTION_MASTER_KEY="$ORACLE_ENCRYPTION_MASTER_KEY" \
  backend python /app/backfill_contact_search.py
```

Optional: `ORACLE_CONTACT_SEARCH_BACKFILL_BATCH` (default `250`, range 1–2000).

### What to expect

Restart-safe — it selects only rows with zero tokens and takes
`FOR UPDATE ... SKIP LOCKED`, so it can be re-run and can run beside live
traffic. It logs counts only, never names.

| Exit | Meaning |
|---|---|
| `0` | Every contact that could be indexed was indexed. |
| `1` | Indexing completed, but some contacts have **no canonical name** and were skipped. They stay invisible to name search until a name is supplied. The count is on stderr. |
| `2` | Refused to start — missing `ORACLE_ENCRYPTION_MASTER_KEY`, or an out-of-range batch size. |

Exit `1` is a report, not a failure: the work that could be done has been
committed. A contact reaches that state by having neither encrypted PII nor a
legacy `clients.full_name`, which is what the 0054 seed can produce.

> Until 2026-08-26 the first such contact raised and aborted the entire run —
> across every remaining tenant — so a single nameless row could prevent the
> whole estate from ever becoming searchable. Skipped rows are now excluded from
> subsequent batches, which is also what keeps the loop terminating: a skipped
> row still matches `cardinality(...) = 0` and would otherwise be re-selected
> forever.

## `backend/backfill_client_notes.py` — seal the client notes written before 0132

### When this is needed

Migration `0132_client_note_body_sealed.sql` (audit A01) made every app write
store a note's text only in `client_notes.body_ciphertext`, under the tenant
key. Rows written before it — including AI call notes since `aec14c4` — keep
their **plaintext** `body` until this script seals them. They still read
correctly meanwhile (`backend/client_note_crypto.py` opens both shapes).

### Check whether you need it

```sql
SELECT count(*) FROM client_notes WHERE body_ciphertext IS NULL AND body <> '';
```

`0` means that database is done. Check **production**, not a dev database.

### Running it

Requires `ORACLE_ENCRYPTION_MASTER_KEY` plus the usual `ORACLE_DB_*` vars
(including `ORACLE_DB_PLATFORM_USER`: it runs in the platform context).

```bash
docker compose run --rm --no-deps \
  -e ORACLE_ENCRYPTION_MASTER_KEY="$ORACLE_ENCRYPTION_MASTER_KEY" \
  backend python /app/backfill_client_notes.py
```

Optional: `ORACLE_CLIENT_NOTE_BACKFILL_BATCH` (default `500`, range 1–5000).

### What to expect

Restart-safe: each batch commits on its own and selects only rows still in
plaintext, with `FOR UPDATE ... SKIP LOCKED`, so it can be stopped, re-run and
run beside live traffic. A seal-only rewrite keeps `updated_at` (the 0132
trigger), so no note starts showing as "edited". It prints counts and tenant
ids, never note text.

| Exit | Meaning |
|---|---|
| `0` | No plaintext note remains. |
| `1` | Rows locked by a concurrent writer were skipped; run it again. |
| `2` | Refused to start — missing `ORACLE_ENCRYPTION_MASTER_KEY`, or an out-of-range batch size. |

**Rollback:** `--unseal` writes the plaintext back (needed only before rolling
the code back to before 0132). Drop `trg_client_notes_before_write` first, as
the 0132 header describes: it refuses plaintext AI call notes.

## `backend/backfill_sealed_text.py` — seal communication text written before 0152 / 0154

### When this is needed

Migration `0152_sealed_communication_text.sql` (audit A01, ledger L17-21) made
every app write store `sms_messages.body`, `negotiation_events.transcript_excerpt`
and `user_interactions.content` only in their new `*_ciphertext` column, under
the tenant key. Rows written before the 0152 code served keep their
**plaintext** until this script seals them; they still read correctly
meanwhile (`backend/sealed_text.py` opens both shapes). It is an operator
script, not a periodic job: staging runs `ORACLE_RECOVERY_MODE`, where no
periodic job runs (L17-03). **Run it only after the 0152 code serves** — the
previous release writes plaintext and cannot read a sealed row.

Migration `0154_sealed_text_slice_two.sql` (L17-22) added five more fields to
the same script and the same rule: `email_outbox` subject and body
(`--field email`), `interaction_logs` subject and payload text keys
(`interaction_log`, through the 0154 `seal_interaction_logs` function),
`ai_tool_operations.result` (`tool_result`), `beliefs.source_quote`
(`belief_quote`) and `user_profiles.profile_summary` (`profile_summary`).
The periodic `sealed_text_seal` task (internal-only, also in recovery mode)
runs every field automatically. Sealing an `email_outbox` row bumps its
`updated_at`, so a row still `queued` restarts its 10-minute reconciliation
window once.

### Check whether you need it

```bash
docker compose run --rm --no-deps backend python /app/backfill_sealed_text.py --count
```

or in SQL:

```sql
SELECT count(*) FROM sms_messages WHERE body_ciphertext IS NULL AND COALESCE(body, '') <> '';
SELECT count(*) FROM negotiation_events WHERE transcript_ciphertext IS NULL AND COALESCE(transcript_excerpt, '') <> '';
SELECT count(*) FROM user_interactions WHERE content_ciphertext IS NULL AND content <> '';
-- 0154
SELECT count(*) FROM email_outbox WHERE body_html IS NULL
   AND ((subject_ciphertext IS NULL AND subject <> '') OR (body_text_ciphertext IS NULL AND body_text <> ''));
SELECT count(*) FROM interaction_logs WHERE (subject_ciphertext IS NULL AND COALESCE(subject, '') <> '')
    OR (content_ciphertext IS NULL AND payload ?| ARRAY['body','text','transcript','summary','extraction']);
SELECT count(*) FROM ai_tool_operations WHERE result_ciphertext IS NULL AND result IS NOT NULL;
SELECT count(*) FROM beliefs WHERE source_quote_ciphertext IS NULL AND COALESCE(source_quote, '') <> '';
SELECT count(*) FROM user_profiles WHERE profile_summary_ciphertext IS NULL AND COALESCE(profile_summary, '') <> '';
```

### Running it

Requires `ORACLE_ENCRYPTION_MASTER_KEY` plus the usual `ORACLE_DB_*` vars
(including `ORACLE_DB_PLATFORM_USER`: it runs in the platform context).

```bash
docker compose run --rm --no-deps \
  -e ORACLE_ENCRYPTION_MASTER_KEY="$ORACLE_ENCRYPTION_MASTER_KEY" \
  backend python /app/backfill_sealed_text.py \
    [--field sms|transcript|interaction|email|interaction_log|tool_result|belief_quote|profile_summary] [--max-rows N]
```

Optional: `ORACLE_SEALED_TEXT_BACKFILL_BATCH` (default `500`, range 1–5000).

### What to expect

Bounded and restart-safe: each batch commits on its own, selects only rows
still in plaintext with `FOR UPDATE ... SKIP LOCKED`, and prints its row count
and time, never the text. A second run changes nothing. `negotiation_events`
and `user_interactions` are append-only to the app role, so their batches go
through the 0152 `SECURITY DEFINER` functions, which only move a row's own
text between its two columns. Measured cost (dev, 2026-10-14): about 0.5 ms of
pgcrypto per row, so 10,000 rows take about 5 s.

| Exit | Meaning |
|---|---|
| `0` | No plaintext row remains (or `--count` / `--max-rows` was given). |
| `1` | Rows were skipped (locked by a writer, or a `user_interactions.tenant_id` that is not a UUID); run it again. |
| `2` | Refused to start — missing `ORACLE_ENCRYPTION_MASTER_KEY`, or an out-of-range batch size. |

**Rollback:** `--unseal` writes the plaintext back. Run it before rolling the
code back to a release older than 0152 (or 0154 for its five fields).

## `backend/client_contact_index.py` — keyed lookup hashes and name tokens for clients (0156)

### When this is needed

Normally never: the worker's internal-only periodic task `client_contact_index`
(every 15 minutes, also under `ORACLE_RECOVERY_MODE`) fills it. Run it by hand
to fill a large brokerage at once after deploying 0156, or to check progress.
It writes only `client_contact_index` (HMAC lookup hashes of the normalised
email and phone, keyed word-prefix name tokens: the `agent_contacts` scheme).
It never changes a client, and nothing searches the index yet (L17-23).

### Running it

Requires `ORACLE_ENCRYPTION_MASTER_KEY` plus the usual `ORACLE_DB_*` vars
(including `ORACLE_DB_PLATFORM_USER`: it runs in the platform context).

```bash
docker compose run --rm --no-deps \
  -e ORACLE_ENCRYPTION_MASTER_KEY="$ORACLE_ENCRYPTION_MASTER_KEY" \
  backend python /app/client_contact_index.py [--count] [--max-rows N]
```

Optional: `ORACLE_CLIENT_CONTACT_INDEX_BATCH` (default `500`, range 1–5000).

### What to expect

Bounded and restart-safe: each batch is one tenant's pending clients (no index
row, or marked stale by an edit), locked `FOR SHARE ... SKIP LOCKED` while the
values are computed, then written in the same short transaction. A client being
edited at that moment is skipped and picked up next time. Prints counts and
timings, never a name, email or phone. Measured (local PostgreSQL 16, 20,000
synthetic clients, 2026-10-15): about 1,550 clients/s with 500-client batches.

| Exit | Meaning |
|---|---|
| `0` | Nothing pending (or `--count` / `--max-rows` was given). |
| `1` | Clients remain (locked by a writer); run it again. |
| `2` | Refused to start — missing `ORACLE_ENCRYPTION_MASTER_KEY`. |

**Rollback:** none needed; the table is derived. `DROP TABLE` per the 0156 header.

## `backfill_property_coordinates.py` — geocode public property records

**Why:** 8.59M `public_property_records` carry an address (93.8%) and an owner
(89.3%), but only **4.3%** carry latitude/longitude. Without a coordinate the
map view has nothing to plot, and `list_comparable_sales` / `estimate_arv` —
whose radius search migration 0076 added an index for — can only consider the
4% that happen to have one. The addresses were always there; nobody resolved
them.

The Census batch geocoder is keyless and free, and was already wired
(`data_integrations/census_geocoder.py`). This is a backfill, not a purchase.

```bash
# match rate only, writes nothing — always run this first
python backfill_property_coordinates.py --state DE --batches 1 --dry-run

# one state, then the rest
python backfill_property_coordinates.py --state DE
python backfill_property_coordinates.py
```

**Resumable.** Progress is the id cursor and the filter is `latitude IS NULL`,
so an interrupted run restarts where it stopped and a row that already has a
coordinate is never re-fetched. Safe to re-run.

**Unmatched rows are not failures.** A demolished parcel or a rural route has no
coordinate to find. The script exits non-zero only when a run resolves *nothing*,
which is what an endpoint outage looks like — `geocode_batch` never raises, it
degrades every row to unmatched, so "0 matched" is the only signal available.

**Courtesy.** Default batch is 5,000 against an API limit of 10,000, with a
one-second pause between calls. This is a free public endpoint doing real work;
raise the batch deliberately, not by default.

**Measured match rate:** 4,494 of 5,000 (89.9%) on Delaware, 2026-08-29.

**A preflight probes the geocoder before touching the database.** Without it an
unreachable endpoint is indistinguishable from a genuine zero-match run —
`geocode_batch` never raises, it degrades every row to unmatched, so an operator
would watch `0/5000 matched` scroll past for hours and conclude the addresses
were bad. Exit codes: `0` wrote something, `1` resolved nothing, `2` the
geocoder was unreachable and nothing was read or written.

Census both has outages and refuses some datacenter egress ranges, and the two
look identical from here (TCP connects, then the connection dies). If the
preflight fails, retry first; if it persists, run from a residential connection
or set `HTTPS_PROXY` to one. `--skip-preflight` forces a run anyway, since the
probe hits a different endpoint than the batch API.

**Census is intermittent, and the script is built around that.** The batch
endpoint can fail while the preflight endpoint answers. A page that resolves
NOTHING is retried in place — the cursor does not advance — because real data
matches ~80-90%, so a whole batch resolving nothing is far more likely to be the
endpoint than the addresses. After three attempts the run stops and exits 1,
leaving the remaining rows queued for the next run. That matters at this scale:
without it, one outage would walk the cursor through all 8.2M rows asking
nothing and report 0% as though the addresses were unmatchable.

Long runs should be detached (`docker compose run -d`), not held open by a shell
whose timeout will SIGTERM them. Resuming is free, but eight hours is not.
