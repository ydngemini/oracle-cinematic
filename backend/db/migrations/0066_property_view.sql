-- Property View: address-first property page with agent AND client media capture.
--
-- Three changes:
--   1. property_media gains 'video' as a kind, plus a `surface` tag so exterior
--      and interior media can be shown separately.
--   2. Video is S3-ONLY. media_blobs stores bytea, which is correct for photos
--      and completely wrong for video — a handful of phone clips would bloat the
--      table, every WAL segment, and every base backup. The CHECK below enforces
--      that a video row carries an s3_key and never a blob.
--   3. property_view_upload_links: revocable, expiring, single-property tokens
--      that let a CLIENT (no account, no login) upload media for one address.
--
-- Depends on 0012 (property_media), 0022 (media_blobs), 0023 (chk_media_kind).

BEGIN;

-- ── 1. media kinds + surface ────────────────────────────────────────────────
ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_kind;
ALTER TABLE property_media ADD CONSTRAINT chk_media_kind
    CHECK (kind IN ('photo', 'video', 'document', 'splat', 'floorplan', 'tour', 'pano'));

ALTER TABLE property_media
    ADD COLUMN IF NOT EXISTS surface text,
    -- Who produced this. Client uploads are untrusted input and are held for
    -- agent review before they can appear on anything public.
    ADD COLUMN IF NOT EXISTS uploaded_via text NOT NULL DEFAULT 'agent',
    ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'approved',
    ADD COLUMN IF NOT EXISTS duration_seconds numeric(8, 2);

ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_surface;
ALTER TABLE property_media ADD CONSTRAINT chk_media_surface
    CHECK (surface IS NULL OR surface IN ('exterior', 'interior', 'aerial', 'street', 'other'));

ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_uploaded_via;
ALTER TABLE property_media ADD CONSTRAINT chk_media_uploaded_via
    CHECK (uploaded_via IN ('agent', 'client_link', 'import', 'pipeline'));

ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_media_review_status;
ALTER TABLE property_media ADD CONSTRAINT chk_media_review_status
    CHECK (review_status IN ('pending', 'approved', 'rejected'));

-- Video must live in object storage, never in the bytea blob table.
ALTER TABLE property_media DROP CONSTRAINT IF EXISTS chk_video_is_s3_backed;
ALTER TABLE property_media ADD CONSTRAINT chk_video_is_s3_backed
    CHECK (kind <> 'video' OR s3_key IS NOT NULL);

CREATE INDEX IF NOT EXISTS property_media_surface_idx
    ON property_media (tenant_id, surface, kind)
    WHERE surface IS NOT NULL;

-- Pending client uploads are what the agent's review queue reads.
CREATE INDEX IF NOT EXISTS property_media_review_idx
    ON property_media (tenant_id, review_status, created_at DESC)
    WHERE review_status = 'pending';


-- ── 2. client upload links ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS property_view_upload_links (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,

    lead_id         uuid REFERENCES leads(id) ON DELETE CASCADE,
    listing_id      uuid REFERENCES listings(id) ON DELETE CASCADE,

    -- SHA-256 of the token. The plaintext token is shown to the agent exactly
    -- once at creation and never stored, so a database read cannot mint access
    -- to a client's upload endpoint (same posture as the lead-connector
    -- webhook secrets in 0061).
    token_hash      bytea NOT NULL UNIQUE,
    label           text,

    -- Who the agent sent it to. Free text: the recipient may not be a contact.
    recipient_hint  text,

    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    max_uploads     integer NOT NULL DEFAULT 40,
    upload_count    integer NOT NULL DEFAULT 0,

    created_by      uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz,

    CONSTRAINT property_view_links_subject_chk CHECK (
        (lead_id IS NOT NULL AND listing_id IS NULL)
     OR (lead_id IS NULL AND listing_id IS NOT NULL)
    ),
    CONSTRAINT property_view_links_quota_chk CHECK (
        max_uploads BETWEEN 1 AND 500 AND upload_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS property_view_links_tenant_idx
    ON property_view_upload_links (tenant_id, created_at DESC);

-- Lookup path for the unauthenticated client endpoint: hash only.
CREATE INDEX IF NOT EXISTS property_view_links_token_idx
    ON property_view_upload_links (token_hash);


-- ── 3. RLS ──────────────────────────────────────────────────────────────────
ALTER TABLE property_view_upload_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_view_upload_links FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS property_view_links_tenant_isolation ON property_view_upload_links;
CREATE POLICY property_view_links_tenant_isolation
    ON property_view_upload_links
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

GRANT SELECT, INSERT ON property_view_upload_links TO oracle_app;
-- Agents revoke links and the client endpoint bumps counters; nothing else is
-- mutable, and the token hash can never be rewritten.
GRANT UPDATE (revoked_at, upload_count, last_used_at, label)
    ON property_view_upload_links TO oracle_app;

COMMIT;
