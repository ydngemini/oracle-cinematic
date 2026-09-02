-- 0090 — put the owner-name index back the way 0086 had it
--
-- 0089 narrowed idx_public_property_owner_normalized to a partial index on
--     length(regexp_replace(lower(COALESCE(owner_name,'')),'[^a-z0-9]','','g')) >= 5
-- reasoning that _property_candidates returns early below that floor
-- (client_ai_automation.py:549-550), so 956,555 of 8,623,748 entries — 11.1% of
-- a 365 MB index — could never be reached.
--
-- The arithmetic was right and the conclusion was wrong: the planner cannot use
-- it. A partial index is usable only when the query's WHERE clause IMPLIES the
-- index predicate, and the query says only
--
--     regexp_replace(lower(COALESCE(owner_name,'')),'[^a-z0-9]','','g') = $1
--
-- Proving that implies `length(<same expression>) >= 5` would mean folding the
-- bound constant through length(), which predicate_implied_by does not do. So
-- the index was silently skipped:
--
--     -> Index Scan using idx_public_property_recent  (cost=0.56..13759366.17)
--        Filter: regexp_replace(...) = 'johnsmith'
--
-- which is the per-row regexp over 8.6M rows that 0086 removed, and back over
-- the pool's 30s command_timeout. Measured: EXPLAIN ANALYZE did not return
-- within 100s, against 0.93ms on the full index.
--
-- Same trap as the schema-version literal in 0086: a predicate the planner
-- cannot match is not a smaller index, it is no index. The 11% is real but
-- unreachable, and 21 MB is not worth a regression of that size.
--
-- The comparison that misled: idx_public_property_address carries
-- WHERE address IS NOT NULL, which DOES work, because a strict operator on a
-- column proves that column is not null. A predicate over a derived expression
-- proves nothing about that expression's value.

CREATE INDEX IF NOT EXISTS idx_public_property_owner_normalized
    ON public_property_records (
        (regexp_replace(lower(COALESCE(owner_name, '')), '[^a-z0-9]', '', 'g')),
        record_refreshed_at DESC
    );

DROP INDEX IF EXISTS idx_public_property_owner_lookup;
