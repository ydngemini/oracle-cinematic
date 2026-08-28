-- 0081 — the index the citable-sources listing needs
--
-- GET /api/intelligence/sources exists so a person can discover which immutable
-- source records an analysis may cite. Every POST on that router requires at
-- least one source_record_id, and nothing listed the table, so authoring any
-- intelligence through the product was impossible — thirteen routes unreachable
-- behind one absent SELECT.
--
-- The property path was already covered by idx_source_records_property from
-- 0027, which is (tenant_id, property_key, observed_at DESC) WHERE property_key
-- IS NOT NULL. The jurisdiction path had nothing, and it is the path a market
-- forecast needs: its subject is a market, so its evidence is jurisdiction-wide
-- records whose property_key is NULL — exactly the rows the existing partial
-- index excludes.
--
-- Without this, filtering by jurisdiction is a sequential scan of every raw
-- record a tenant has ever retained. That is the same shape of query that cost
-- this codebase a measured 13.5 s response in 0076, and the listing endpoint
-- refuses an unfiltered call for the same reason.
--
-- Retention rewrites raw_payload in place and sets purged_at, so purged rows
-- stay in the table and stay citable (the hash and provenance survive). They
-- are therefore deliberately NOT excluded here.
--
-- Idempotent. Not CONCURRENTLY: run_migrations.py wraps each file in a
-- transaction, and CREATE INDEX CONCURRENTLY cannot run inside one.

CREATE INDEX IF NOT EXISTS idx_source_records_jurisdiction
    ON source_records (tenant_id, jurisdiction, observed_at DESC)
    WHERE jurisdiction IS NOT NULL;
