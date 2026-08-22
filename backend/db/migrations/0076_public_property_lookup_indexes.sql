-- 0076 — indexes the read-only agent tools depend on
--
-- Two lookups the B1 agent tools need, both of which were full scans of a
-- ~7,000,000-row table before this migration:
--
--   1. zip -> state/county, for get_market_trends and get_days_on_market.
--      A zip that HAS rows exits early and looks fine (~8 ms); a zip with no
--      rows scans all 6.96M and took 13.5 s measured on dev. An agent can type
--      any five digits, so the pathological case is the reachable one.
--
--   2. a radius search around a subject property, for list_comparable_sales
--      and estimate_arv. latitude/longitude were entirely unindexed, so every
--      comp lookup would have been a seq scan.
--
-- Both are cheap. Only 41,292 of 6.96M rows carry BOTH a coordinate and a sale
-- price, so the comps index is tiny; the partial predicates are repeated in the
-- query WHERE clauses so the planner can actually match them.
--
-- The thinness of that 41k is itself a fact the tools must report: "no comps
-- within half a mile" and "this dataset has geocoded sale prices for 0.6% of
-- its records" are different answers, and only one of them is about the market.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_public_property_zip_state
    ON public_property_records (zip_code, state)
    WHERE zip_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_public_property_comps
    ON public_property_records (state, latitude, longitude)
    WHERE latitude IS NOT NULL
      AND longitude IS NOT NULL
      AND last_sale_price IS NOT NULL;

COMMIT;
