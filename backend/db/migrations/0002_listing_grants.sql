-- ===========================================================================
-- 0002_listing_grants.sql
-- Explicit co-broke grant mechanism — the join table the prototype lacked.
--
-- `is_shared_mls` (from 0001) is the blunt instrument: publish to the *whole*
-- pool. This adds targeted sharing — Brokerage A grants Brokerage B view (or
-- co-broke) rights on ONE specific listing, without exposing it publicly.
--
-- Visibility precedence for a listing now:
--   own tenant  >  platform_admin  >  public MLS (is_shared_mls)  >  explicit grant
-- Writes remain owner-only regardless of any grant (see 0001 listings_write).
-- ===========================================================================

CREATE TABLE listing_grants (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id        uuid NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    grantor_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,  -- listing owner
    grantee_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,  -- co-broke partner
    permission        text NOT NULL DEFAULT 'view'
                          CHECK (permission IN ('view','co_broke')),  -- co_broke ⇒ may submit offers
    granted_by        uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (listing_id, grantee_tenant_id),
    CHECK (grantor_tenant_id <> grantee_tenant_id)   -- no self-grants
);

CREATE INDEX idx_grants_grantee ON listing_grants(grantee_tenant_id);
CREATE INDEX idx_grants_listing ON listing_grants(listing_id);

-- ---------------------------------------------------------------------------
-- SECURITY DEFINER lookup. Runs as the function owner so it bypasses RLS on
-- listing_grants — this avoids the listings_read policy recursing into a table
-- that itself has RLS. Marked STABLE; only reads.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_has_listing_grant(p_listing_id uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$
    SELECT EXISTS (
        SELECT 1 FROM listing_grants g
        WHERE g.listing_id = p_listing_id
          AND g.grantee_tenant_id = app_current_tenant()
    )
$$;

-- ---------------------------------------------------------------------------
-- Replace the base listings read policy with the grant-aware version.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS listings_read ON listings;
CREATE POLICY listings_read ON listings
    FOR SELECT
    USING (
        app_is_platform_admin()
        OR tenant_id = app_current_tenant()
        OR is_shared_mls = true
        OR app_has_listing_grant(id)
    );

-- ---------------------------------------------------------------------------
-- RLS on the grant table itself: a tenant sees a grant row if it is either
-- side of it (grantor managing shares, or grantee seeing what it received).
-- Only the grantor (listing owner) may create/revoke its own grants.
-- ---------------------------------------------------------------------------
ALTER TABLE listing_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE listing_grants FORCE  ROW LEVEL SECURITY;

CREATE POLICY grants_visible ON listing_grants
    FOR SELECT
    USING (
        app_is_platform_admin()
        OR grantor_tenant_id = app_current_tenant()
        OR grantee_tenant_id = app_current_tenant()
    );

CREATE POLICY grants_managed_by_grantor ON listing_grants
    FOR ALL
    USING (app_is_platform_admin() OR grantor_tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR grantor_tenant_id = app_current_tenant());

GRANT EXECUTE ON FUNCTION app_has_listing_grant(uuid) TO oracle_app;
