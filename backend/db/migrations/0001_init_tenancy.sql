-- ===========================================================================
-- 0001_init_tenancy.sql
-- Oracle — Multi-Tenant IAM + Row-Level Security (base layer).
-- Decision 006 (Hybrid Tenant Isolation); IAM-over-AD per obs #623.
--
--   Forest  -> this database (the Oracle platform)
--   Domain  -> a brokerage = one row in `tenants` (tenant_id)
--   Node    -> an agent/client, scoped to tenant_id + role
--
-- HYBRID isolation: private data (clients, leads, AI underwriting outputs) is
-- hard-walled per tenant; listings share via the public MLS pool (is_shared_mls)
-- or explicit co-broke grants (see 0002_listing_grants.sql). platform_admin is
-- the god-mode override.
--
-- The app connects as the NOLOGIN group role `oracle_app` (granted to the real
-- login role out-of-band). It is NOT the table owner and is NOT superuser, so
-- RLS actually binds it. Per request the Auth Gatekeeper sets:
--   SELECT set_config('app.current_tenant', '<uuid>', true);
--   SELECT set_config('app.current_role',   '<role>', true);
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        CREATE ROLE oracle_app NOLOGIN;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Session-context accessors. current_setting(key, true) yields NULL when unset
-- (e.g. an unauthenticated connection) rather than raising.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
    LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.current_tenant', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app_current_role() RETURNS text
    LANGUAGE sql STABLE AS
$$ SELECT COALESCE(NULLIF(current_setting('app.current_role', true), ''), 'anon') $$;

CREATE OR REPLACE FUNCTION app_is_platform_admin() RETURNS boolean
    LANGUAGE sql STABLE AS
$$ SELECT app_current_role() = 'platform_admin' $$;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql AS
$$ BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ---------------------------------------------------------------------------
-- Domains (brokerages) and Nodes (users)
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id    text NOT NULL UNIQUE,                 -- maps to JWT `sub`
    role        text NOT NULL CHECK (role IN ('platform_admin','broker_owner','agent')),
    is_active   boolean NOT NULL DEFAULT true,        -- admin can revoke instantly
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Private, walled-off resources — never cross a tenant boundary.
-- ---------------------------------------------------------------------------
CREATE TABLE clients (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    full_name   text NOT NULL,
    email       text,
    phone       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Scout motivated-seller leads + AI underwriting outputs, per tenant.
CREATE TABLE leads (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    parcel_id        text NOT NULL,
    state            text NOT NULL,
    motivation_score int  NOT NULL,
    underwriting     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- ARV / rehab / MAO
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Listings — private by default; public MLS opt-in via is_shared_mls.
-- Targeted co-broke sharing is layered on in 0002.
-- ---------------------------------------------------------------------------
CREATE TABLE listings (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    address       text NOT NULL,
    price         numeric(12,2),
    is_shared_mls boolean NOT NULL DEFAULT false,       -- public pool switch
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_tenant    ON users(tenant_id);
CREATE INDEX idx_clients_tenant  ON clients(tenant_id);
CREATE INDEX idx_leads_tenant    ON leads(tenant_id);
CREATE INDEX idx_listings_tenant ON listings(tenant_id);
CREATE INDEX idx_listings_shared ON listings(is_shared_mls) WHERE is_shared_mls;

CREATE TRIGGER trg_tenants_updated  BEFORE UPDATE ON tenants  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated    BEFORE UPDATE ON users    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_clients_updated  BEFORE UPDATE ON clients  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_leads_updated    BEFORE UPDATE ON leads    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_listings_updated BEFORE UPDATE ON listings FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ===========================================================================
-- Row-Level Security. FORCE so even a table owner is bound (defense in depth).
-- ===========================================================================
ALTER TABLE users    ENABLE ROW LEVEL SECURITY;  ALTER TABLE users    FORCE ROW LEVEL SECURITY;
ALTER TABLE clients  ENABLE ROW LEVEL SECURITY;  ALTER TABLE clients  FORCE ROW LEVEL SECURITY;
ALTER TABLE leads    ENABLE ROW LEVEL SECURITY;  ALTER TABLE leads    FORCE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;  ALTER TABLE listings FORCE ROW LEVEL SECURITY;

CREATE POLICY users_tenant_isolation ON users
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

CREATE POLICY clients_tenant_isolation ON clients
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

CREATE POLICY leads_tenant_isolation ON leads
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Listings read policy is (re)defined in 0002 to include co-broke grants.
-- Base read = own tenant or public MLS pool.
CREATE POLICY listings_read ON listings
    FOR SELECT
    USING (
        app_is_platform_admin()
        OR tenant_id = app_current_tenant()
        OR is_shared_mls = true
    );

-- Mutation: owning tenant only, even when shared.
CREATE POLICY listings_write ON listings
    FOR ALL
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- ---------------------------------------------------------------------------
-- Privileges. RLS restricts ROWS; GRANT still gates TABLE access.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO oracle_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oracle_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oracle_app;
