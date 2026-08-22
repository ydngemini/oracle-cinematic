-- 0078 — clear ZIP codes that belong to a different state than their record
--
-- Four harvesters mapped the OWNER's mailing city/ZIP into the PROPERTY's
-- fields. tn_shelby's own field map said so plainly — `OwnerCityStZip → city +
-- zip_code` — right below a note that OwnerAddress is the mailing address. For
-- an owner-occupied parcel the two coincide; for an absentee owner they do not,
-- and that is how 48 parcels in Dover, Tennessee came to carry 19901, the ZIP
-- for Dover, Delaware. wy_parcels.py already had the correct rule in a comment;
-- tn_shelby, nh_granit and mt_cadastral now follow it.
--
-- Those fixes only affect future harvests. This clears the rows already stored.
--
-- The test for a bad row is that its ZIP3 prefix belongs to a different state.
-- A ZIP3 is allocated to one state by USPS, so disagreement is an error rather
-- than a real ambiguity — and the record's `state` is the trustworthy half,
-- since it comes from the harvester's own scope, not from a parsed field.
--
-- Evidence rule: a ZIP3 is treated as belonging to a state when at least 50
-- rows carry it, the leading state holds at least 80% of them, AND it leads the
-- runner-up by 5x. A flat percentage alone was wrong here — ZIP3 199 is 82%
-- Delaware with 1,319 rows against a 62-row runner-up, unmistakable, yet a 95%
-- threshold refused it and left Dover, Tennessee holding 19901. The margin is
-- what separates "one state with a polluted tail" from "genuinely muddy".
--
-- Measured before writing: 96.94% of 4,953,099 geocoded rows already agree with
-- their ZIP3's leading state. 204 ZIP3s clear the bar; the rest are left alone.
--
-- The ZIP is set to NULL, not corrected. The true ZIP is not recoverable from
-- what was stored, and a guessed one would be the same error with a different
-- value. Consumers already treat a missing ZIP as missing, and
-- ai_tools_read._resolve_zip refuses a ZIP its own agreement check cannot
-- settle rather than reporting the wrong state's market.

BEGIN;

WITH zip3_state AS (
    SELECT substring(zip_code, 1, 3) AS zip3, state, count(*) AS n
      FROM public_property_records
     WHERE zip_code ~ '^[0-9]{5}$' AND state IS NOT NULL
     GROUP BY 1, 2
),
ranked AS (
    SELECT zip3, state, n,
           row_number() OVER (PARTITION BY zip3 ORDER BY n DESC) AS rank,
           sum(n) OVER (PARTITION BY zip3) AS total
      FROM zip3_state
),
leader AS (SELECT zip3, state, n, total FROM ranked WHERE rank = 1),
runner_up AS (SELECT zip3, n AS runner FROM ranked WHERE rank = 2),
authoritative AS (
    SELECT l.zip3, l.state
      FROM leader l
      LEFT JOIN runner_up r USING (zip3)
     WHERE l.total >= 50
       AND l.n::float / l.total >= 0.80
       AND l.n >= 5 * coalesce(r.runner, 0)
)
UPDATE public_property_records p
   SET zip_code = NULL,
       updated_at = now()
  FROM authoritative a
 WHERE a.zip3 = substring(p.zip_code, 1, 3)
   AND p.zip_code ~ '^[0-9]{5}$'
   AND p.state IS NOT NULL
   AND p.state <> a.state;

COMMIT;
