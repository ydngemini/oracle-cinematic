-- ===========================================================================
-- 0022_media_blobs.sql
--
-- Backing store for 2D property-image UPLOAD (media_api.py). Agents (JWT) and
-- homeowners (passwordless portal token) attach photos to a lead/listing; the
-- metadata row lives in the existing tenant-scoped, FORCE-RLS `property_media`
-- table, but the raw bytes need a home that:
--
--   1. survives a read-only / uid-1001 container filesystem (the bind mount at
--      /app is not reliably writable — see CLAUDE.md / prod-readiness audit), so
--      we store bytes in Postgres rather than on disk; and
--   2. can be streamed by the PUBLIC, unauthenticated GET /api/media/{id} that
--      backs <img src> references. That serve path carries NO tenant context, so
--      it cannot satisfy property_media's FORCE RLS. The bytes therefore live in
--      a SEPARATE, deliberately NON-tenant-scoped table keyed by the media id (a
--      122-bit random uuid → effectively unguessable), exactly like di_cache
--      (0021) holds public, non-private payloads with no row-level security.
--
-- Privacy: the only thing readable by id is the image itself, which is meant to
-- be served publicly. The FK to property_media (ON DELETE CASCADE) guarantees a
-- blob never outlives its tenant-scoped metadata row, so DELETE /api/crm/media
-- and any tenant/lead/listing cascade also reaps the bytes.
--
-- Idempotent + forward-only: IF NOT EXISTS everywhere; re-applying is a no-op.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS media_blobs (
    media_id     uuid PRIMARY KEY
                 REFERENCES property_media(id) ON DELETE CASCADE,
    content_type text        NOT NULL,
    byte_size    integer     NOT NULL,
    bytes        bytea       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- The app connects as the non-owner group role `oracle_app` (see 0001); grant it
-- the DML it needs. Guarded so it is a no-op where the role is absent (bare local
-- DB connecting as the postgres superuser already has everything).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON media_blobs TO oracle_app';
    END IF;
END $$;
