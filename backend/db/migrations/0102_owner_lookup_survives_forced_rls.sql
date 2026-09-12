-- 0102 — the owner-name index 0086/0089/0090 built has never actually worked
--
-- Root cause, found while chasing a fresh TimeoutError() wave in
-- crm:client_reconcile: 0086 measured 0.93ms for
--     idx_public_property_owner_normalized
--         ON public_property_records ((regexp_replace(lower(COALESCE(owner_name,
--         '')),'[^a-z0-9]','','g')), record_refreshed_at DESC)
-- but never against the path the app actually runs on — the pool connects as
-- oracle_app_login, and public_property_records is FORCE ROW LEVEL SECURITY
-- (0050) with a role-only read policy:
--     app_current_role() = ANY(ARRAY['agent','broker_owner','platform_admin'])
--
-- Confirmed via EXPLAIN: with that policy in force, the planner will not use
-- the expression index at ANY setting tried (a literal predicate,
-- force_generic_plan, even `SET enable_seqscan=off`, which instead picks the
-- unrelated idx_public_property_recent and still filters row-by-row). The one
-- thing that made it use the index was `SET row_security=off` — a
-- superuser-only escape hatch the app can never take.
--
-- The mechanism: `lower` and `regexp_replace` are NOT leakproof
-- (pg_proc.proleakproof = false for both). Under FORCE RLS, Postgres will not
-- interleave a non-leakproof user qual with the security-barrier qual — doing
-- so could let a crafted expression observe rows through error side effects
-- before the security check runs. So the whole scan is forced through a
-- Filter, and an index built on that same non-leakproof expression is simply
-- never a candidate. This has nothing to do with statistics, VACUUM, or the
-- partial-predicate mistake 0089/0090 already fixed once — it is orthogonal,
-- and it means every expression index in this file's history that involves
-- lower()/regexp_replace() has been silently unusable under RLS the entire
-- time, on this table.
--
-- The fix is to stop asking the planner to combine a non-leakproof expression
-- with the RLS qual at all: compare a plain text column instead. `texteq` (the
-- `text = text` operator) IS leakproof, so it freely combines with the RLS
-- qual and a plain btree index works exactly as expected — verified against
-- this database with `owner_name_normalized` populated: EXPLAIN with RLS
-- enforced shows an Index Scan on idx_public_property_owner_lookup at
-- cost=0.56..~220 instead of a Seq Scan at cost=~1.2M.
--
-- ⚠ NOT a GENERATED ALWAYS AS ... STORED column. That was tried first and
-- reverted: adding a stored generated column forces an immediate full-table
-- rewrite under ACCESS EXCLUSIVE (no CONCURRENTLY option exists for it), and
-- on this table (9.8M rows, 6.7GB, actively growing from live harvesters) that
-- rewrite's WAL and second relfilenode exceeded 8.9GB of free disk mid-run —
-- confirmed live, caught before disk hit zero via pg_cancel_backend, no data
-- lost. A plain nullable column is metadata-only to add (no rewrite at all);
-- backfill_owner_name_normalized.py fills it in small resumable batches
-- instead of one giant transaction, and a trigger keeps every future row
-- correct going forward. This is the same reason the 0076/0086/0089/0090
-- family of migrations exists — the fix for a huge table can't be "the thing
-- that would work on an empty one."

BEGIN;

-- Instant: no DEFAULT, no rewrite, just a catalog change. Every existing row
-- starts NULL; backfill_owner_name_normalized.py fills them in batches.
ALTER TABLE public_property_records
    ADD COLUMN IF NOT EXISTS owner_name_normalized text;

-- Keeps every future INSERT/UPDATE correct without the app having to remember
-- to set it. Fires only when owner_name actually changes, so it never
-- interferes with unrelated updates (e.g. a coordinate backfill).
CREATE OR REPLACE FUNCTION set_owner_name_normalized() RETURNS trigger AS $BODY$
BEGIN
    NEW.owner_name_normalized := regexp_replace(
        lower(COALESCE(NEW.owner_name, '')), '[^a-z0-9]', '', 'g'
    );
    RETURN NEW;
END;
$BODY$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_owner_name_normalized ON public_property_records;
CREATE TRIGGER trg_owner_name_normalized
    BEFORE INSERT OR UPDATE OF owner_name ON public_property_records
    FOR EACH ROW EXECUTE FUNCTION set_owner_name_normalized();

-- Plain CREATE INDEX (no WHERE — that is the exact partial-predicate mistake
-- 0090 reverted; test_no_partial_index_predicates_the_planner_cannot_match
-- enforces this at the file level). --prebuild-indexes builds this
-- CONCURRENTLY like every other plain index in this repo; building it now,
-- while the column is still mostly NULL, is cheap and it fills in as the
-- backfill runs and as the trigger covers new/updated rows.
CREATE INDEX IF NOT EXISTS idx_public_property_owner_lookup
    ON public_property_records (owner_name_normalized, record_refreshed_at DESC);

-- Superseded: this index matched the query but the planner could never choose
-- it under RLS. Kept buildable in 0086's ledger entry; dropped for real here.
DROP INDEX IF EXISTS idx_public_property_owner_normalized;

COMMIT;
