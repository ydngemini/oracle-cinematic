-- Make tier 2 (360° walkthrough) reachable.
--
-- 'pano' has been a legal property_media.kind since 0023, and tour_api's
-- resolver has always selected tier 2 for it. But nothing anywhere writes a
-- pano row — every writer hardcodes 'photo', 'video' or 'splat' — and the
-- frontend has no pano viewer at all. The rung has been in the ladder,
-- structurally unreachable, the whole time.
--
-- A ladder with a permanently empty rung is a latent lie: the resolver claims
-- "Walk room-to-room through 360° captures of the actual home" for a tier that
-- cannot occur. This gives it a writer and a shape.
--
-- Why a scenes table rather than reusing property_media alone: one equirect
-- photo is a *view*, not a walkthrough. Walking means moving between linked
-- vantage points, which needs per-scene placement (which floor, where on it,
-- which way you are facing) and adjacency. Those are properties of the scene,
-- not of the image bytes, so they do not belong on the media row.
--
-- Floor numbering is deliberately the same integer space as
-- tour_api._floors_from_plan, so a pano tour inherits the saved floor plan's
-- level navigation instead of carrying a second, disagreeing floor model.
--
-- Depends on 0001 (app_current_tenant/app_is_platform_admin), 0012
-- (property_media), 0023 (the 'pano' kind).

BEGIN;

CREATE TABLE IF NOT EXISTS property_pano_scenes (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,
    media_id      uuid        NOT NULL REFERENCES property_media(id) ON DELETE CASCADE,

    -- Mirrors property_media's subject columns so the resolver can filter
    -- scenes by property without a join back through media.
    lead_id       uuid,
    listing_id    uuid,

    -- Which storey, in _floors_from_plan's index space. 0 is the ground floor.
    floor_index   int         NOT NULL DEFAULT 0,
    label         text        NOT NULL DEFAULT '',

    -- Placement within the floor, in metres from the plan origin, and the
    -- compass bearing the camera should open facing. All nullable: an agent who
    -- has simply uploaded two 360s in order has given us adjacency but not
    -- coordinates, and that is a usable tour. Fabricating positions to fill
    -- these in would put rooms in places nobody surveyed.
    position_x    double precision,
    position_y    double precision,
    position_z    double precision,
    heading_deg   double precision,

    -- Scenes you can move to from here. Empty means "no links recorded yet";
    -- the viewer then falls back to sort_order as the walk sequence.
    neighbour_ids uuid[]      NOT NULL DEFAULT '{}',

    sort_order    int         NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),

    -- A scene belongs to exactly one property, matching property_media's rule.
    CONSTRAINT chk_pano_scene_subject
        CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL),
    -- One scene per media row: the image is the scene's identity.
    CONSTRAINT uq_pano_scene_media UNIQUE (media_id)
);

CREATE INDEX IF NOT EXISTS idx_pano_scenes_lead
    ON property_pano_scenes (lead_id, floor_index, sort_order)
    WHERE lead_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pano_scenes_listing
    ON property_pano_scenes (listing_id, floor_index, sort_order)
    WHERE listing_id IS NOT NULL;

ALTER TABLE property_pano_scenes ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_pano_scenes FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'property_pano_scenes_tenant_isolation'
    ) THEN
        CREATE POLICY property_pano_scenes_tenant_isolation ON property_pano_scenes
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON property_pano_scenes TO oracle_app';
    END IF;
END $$;

COMMENT ON TABLE property_pano_scenes IS
    'Vantage points in a 360 walkthrough. Two or more linked scenes is what '
    'distinguishes a walkthrough from a single 360 view; tour_api only reports '
    'walkable_interior once that threshold is met.';

COMMENT ON COLUMN property_pano_scenes.floor_index IS
    'Same index space as tour_api._floors_from_plan, so pano tours reuse the '
    'saved floor plan''s level navigation rather than defining their own.';

COMMIT;
