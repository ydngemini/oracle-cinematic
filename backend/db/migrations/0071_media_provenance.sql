-- Record whether a splat came from a real capture or was synthesised.
--
-- RECONSTRUCTION_PROVIDER defaults to 'stub' (reconstruction_providers.py), and
-- StubProvider.reconstruct synthesises a 4.0 x 2.6 x 4.0 m checkerboard box —
-- floor, ceiling, four walls, zero capture, zero compute. The worker then runs
-- that through the normal pipeline and inserts it as an ordinary
-- property_media(kind='splat') row.
--
-- Nothing on that row said where it came from, so tour_api's resolver counted
-- it as tier 3 and told the user:
--
--     "Free-roam the actual home in a photoreal 3D reconstruction."
--
-- about a generated cube. On a default deployment that sentence was false for
-- every property, and the variable holding it is named `honest_note`.
--
-- The demo splat is genuinely useful — it exercises the viewer, the bounds
-- clamp and the controls without a GPU — so this does not remove it. It makes
-- it declare itself, and the resolver stops promoting it to "the actual home".
--
-- Defaults to 'captured' because every row that exists before this migration
-- was written by the real worker path on a deployment that had configured a
-- real provider; rows made by the stub are dev artefacts. New rows always pass
-- the value explicitly (reconstruction_worker._record_media).
--
-- Depends on 0012 (property_media), 0066 (chk_video_is_s3_backed).

BEGIN;

ALTER TABLE property_media
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'captured';

ALTER TABLE property_media
    ADD COLUMN IF NOT EXISTS generator text;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_media_provenance'
    ) THEN
        ALTER TABLE property_media
            ADD CONSTRAINT chk_media_provenance
            CHECK (provenance IN ('captured', 'synthetic', 'imported', 'ai_generated'));
    END IF;
END $$;

COMMENT ON COLUMN property_media.provenance IS
    'captured = a real capture of this property. synthetic = generated, not this '
    'building (e.g. the stub provider''s demo room). imported = supplied by a '
    'third party. ai_generated = model-produced imagery. Only ''captured'' may '
    'support a claim that the media shows the actual home.';

COMMENT ON COLUMN property_media.generator IS
    'Which provider or tool produced a non-captured row, for support and audit.';

-- The resolver filters on (lead_id|listing_id, kind) and orders by sort_order;
-- provenance now joins that predicate on the tier-3 path.
CREATE INDEX IF NOT EXISTS idx_property_media_lead_kind
    ON property_media (lead_id, kind, sort_order)
    WHERE lead_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_property_media_listing_kind
    ON property_media (listing_id, kind, sort_order)
    WHERE listing_id IS NOT NULL;

-- Phase 2's batch lead lookup in mls_portal joins public records to tenant
-- leads on exactly this pair, once per page.
CREATE INDEX IF NOT EXISTS idx_leads_parcel_state
    ON leads (parcel_id, state)
    WHERE parcel_id IS NOT NULL;

COMMIT;
