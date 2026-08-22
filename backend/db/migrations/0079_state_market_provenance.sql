-- 0079 — provenance for state market aggregates
--
-- `state_market_stats` has been serving migration 0025's static seed since it
-- was written: 51 rows, every one dated 2024-10-01. routes_market.py says so in
-- its own docstring — "nothing refreshes it" — and points callers at
-- /api/data/market-research for live figures.
--
-- Meanwhile the scheduled Zillow/Redfin/FHFA/HUD sync has been landing fresh
-- state-level observations in `public_market_metrics` all along. The data was
-- already arriving; nothing connected it to the table the compliance and market
-- routes actually read. 0080's loader does that, and these columns are what
-- make the result readable:
--
--   source_key           which feed a row came from ('migration_0025_seed' for
--                        anything still carrying the original values)
--   source_url           where it can be checked
--   source_fetched_at    when the platform retrieved it — distinct from
--                        as_of_date, which is the period the figures DESCRIBE.
--                        Redfin publishes with roughly a three-month lag, so
--                        conflating the two would overstate freshness by ~80
--                        days on every row.
--   verification_status  machine | human_verified | stale
--
-- Machine-harvested market data must never be indistinguishable from a figure
-- a person checked. Default 'machine' says what it is.

BEGIN;

ALTER TABLE state_market_stats
    ADD COLUMN IF NOT EXISTS source_key          text,
    ADD COLUMN IF NOT EXISTS source_url          text,
    ADD COLUMN IF NOT EXISTS source_fetched_at   timestamptz,
    ADD COLUMN IF NOT EXISTS verification_status text NOT NULL DEFAULT 'machine';

ALTER TABLE state_market_stats
    DROP CONSTRAINT IF EXISTS chk_state_market_verification;
ALTER TABLE state_market_stats
    ADD CONSTRAINT chk_state_market_verification
        CHECK (verification_status IN ('machine', 'human_verified', 'stale'));

-- Existing rows are the 0025 seed. Naming that is the point: without it a
-- refreshed row and a two-year-old seed look identical in the response.
UPDATE state_market_stats
   SET source_key = 'migration_0025_seed',
       verification_status = 'stale'
 WHERE source_key IS NULL;

COMMIT;
