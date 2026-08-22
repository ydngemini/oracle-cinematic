-- 0075 — floor-plan dimension provenance
--
-- `auto-dimensions` (and extract-parcel/extract-image) already compute a
-- manifest attributing every dimension to measured/sourced/estimated/default,
-- but RehabEditorDrawer discarded everything except a toast count — the UI
-- had no way to show an agent WHICH numbers were guesses. This persists that
-- manifest so it survives a save and a reload, and adds scaffold_sha256 so a
-- later training step (P10(b), floor-plan pairs) can tell "the agent accepted
-- this scaffold unchanged" (poison — the model's own output fed back as
-- ground truth) from "the agent corrected it" (the actual signal).
--
-- Both columns are nullable and mean "provenance unknown" when NULL — a
-- manually-drawn plan, or any row saved before this migration, has neither,
-- and the UI must render that as "provenance unknown", never as "measured".

BEGIN;

ALTER TABLE property_floorplans
    ADD COLUMN IF NOT EXISTS dimension_manifest jsonb,
    ADD COLUMN IF NOT EXISTS scaffold_sha256     char(64)
        CHECK (scaffold_sha256 IS NULL OR scaffold_sha256 ~ '^[0-9a-f]{64}$');

ALTER TABLE property_floorplan_revisions
    ADD COLUMN IF NOT EXISTS dimension_manifest jsonb,
    ADD COLUMN IF NOT EXISTS scaffold_sha256     char(64)
        CHECK (scaffold_sha256 IS NULL OR scaffold_sha256 ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN property_floorplans.dimension_manifest IS
    'Per-dimension provenance from the last machine pipeline call (auto-dimensions, '
    'extract-parcel, extract-image) that produced or touched this layout: '
    'measured/sourced/estimated/default per field, with a human-readable basis '
    'for each. NULL means unknown provenance (manual draw, or pre-migration row) '
    '— render as "provenance unknown", never as "measured".';

COMMENT ON COLUMN property_floorplans.scaffold_sha256 IS
    'sha256 of the machine-produced document at the moment it was generated, '
    'before any edit. Compare against a saved revision''s own document hash to '
    'tell an accepted-unchanged scaffold (the model''s own output, poison for '
    'training on) from a human correction (the actual signal). NULL when the '
    'current document did not originate from a machine pipeline call.';

COMMENT ON COLUMN property_floorplan_revisions.dimension_manifest IS
    'Same manifest as property_floorplans.dimension_manifest, snapshotted at '
    'this revision so provenance stays correct when browsing history.';

COMMENT ON COLUMN property_floorplan_revisions.scaffold_sha256 IS
    'Same as property_floorplans.scaffold_sha256, snapshotted at this revision. '
    'This is the field P10(b) filters on: a revision whose own document hashes '
    'to this value was accepted unchanged and must be excluded from training.';

COMMIT;
