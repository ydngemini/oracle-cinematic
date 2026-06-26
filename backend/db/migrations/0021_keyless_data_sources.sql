-- ===========================================================================
-- 0021_keyless_data_sources.sql
--
-- Backing store for the KEYLESS data-integration cache layer
-- (data_integrations/cache.py — IntegrationCache L2). The Census geocoder,
-- OpenFEMA and EPA Envirofacts sources added in the keyless-data-sources
-- program all read/write `di_cache` via that two-tier (Redis L1 / PG L2) cache;
-- the table was referenced by the code but never created by a migration, so the
-- PG tier was silently degrading to a no-op (every call hit the upstream).
--
-- di_cache holds ONLY public, non-tenant data (geocodes, FEMA declarations, EPA
-- facility lists) keyed by an opaque cache_key, so it is intentionally NOT
-- tenant-scoped and carries NO row-level security (nothing private lives here).
--
-- The harvested distress rows (NYC HPD + Chicago violations) need no new table:
-- they persist through the existing RLS-forced `leads` path (harvesters/base.py
-- persist_leads, idempotent via leads UNIQUE(tenant_id, parcel_id) from 0018).
--
-- Idempotent + forward-only: re-applying is a no-op, and IF NOT EXISTS makes it
-- safe to co-exist with any other migration that also provisions di_cache.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS di_cache (
    cache_key   text PRIMARY KEY,
    payload     jsonb       NOT NULL,
    expires_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Supports TTL sweeps / "live rows" filtering (cache.py reads WHERE expires_at > now()).
CREATE INDEX IF NOT EXISTS di_cache_expires_at_idx ON di_cache (expires_at);

-- The app connects as the non-owner group role `oracle_app` (see 0001); grant it
-- the DML it needs on this public cache. Guarded so it is a no-op where the role
-- is absent (e.g. a bare local DB).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON di_cache TO oracle_app';
    END IF;
END $$;
