-- Stop asserting agency law nobody researched.
--
-- 0013 created state_regulatory_profiles with:
--
--     designated_agency_permitted boolean NOT NULL DEFAULT true,
--     sub_agency_permitted        boolean NOT NULL DEFAULT true,
--
-- and its 51-state seed (0013:573-576) populates neither. Nothing anywhere in
-- the codebase writes them either. So every row holds `true` — not because
-- anyone checked, but because that is what the column default was — and
-- GET /api/states/{state} has been reporting both forms of agency as permitted
-- in all 51 jurisdictions as though it were a finding.
--
-- Designated agency and sub-agency are genuinely prohibited or restricted in
-- several states, and an agent reading this endpoint is making a licensing
-- decision. "We don't know" has to be representable, and it has to be the
-- value these columns actually carry.
--
-- Note the contrast with dual_agency_permitted in the same table: that one IS
-- in the seed's column list and IS researched per state (Alaska is false), so
-- it keeps its NOT NULL default and is deliberately untouched here.
--
-- Depends on 0013 (state_regulatory_profiles).

BEGIN;

ALTER TABLE state_regulatory_profiles
    ALTER COLUMN designated_agency_permitted DROP DEFAULT,
    ALTER COLUMN designated_agency_permitted DROP NOT NULL,
    ALTER COLUMN sub_agency_permitted        DROP DEFAULT,
    ALTER COLUMN sub_agency_permitted        DROP NOT NULL;

-- Every current value is the column default rather than a researched one —
-- there has never been a writer for either column. NULL them so the API stops
-- reporting an unverified `true`. A deployment that has since researched these
-- should re-apply its own values after this migration.
UPDATE state_regulatory_profiles
   SET designated_agency_permitted = NULL,
       sub_agency_permitted        = NULL;

COMMENT ON COLUMN state_regulatory_profiles.designated_agency_permitted IS
    'NULL = not researched for this state. Do not coalesce to true; the API '
    'surfaces NULL as "unknown" so callers can tell it apart from a finding.';

COMMENT ON COLUMN state_regulatory_profiles.sub_agency_permitted IS
    'NULL = not researched for this state. Do not coalesce to true; the API '
    'surfaces NULL as "unknown" so callers can tell it apart from a finding.';

COMMIT;
