-- 0020_di_cache.sql — Persistent L2 store for the data-integrations cache.
-- Backs IntegrationCache (backend/data_integrations/cache.py) and the AVM
-- valuation cache (backend/avm_client.py). The table was referenced by the
-- cache's get()/set() SQL but never had a migration, so to_regclass(...) was
-- null and nothing persisted — every RentCast/ATTOM/geocode/etc. lookup hit
-- the live provider. This creates it so those callers finally cache.
--
-- This is a GLOBAL, source-namespaced cache (keys are prefixed by source,
-- e.g. 'avm:...', 'geocode:...'). It holds no tenant data and is intentionally
-- NOT under RLS — matching cache.py, whose get()/set() use a plain pool
-- connection (no tenancy.apply_rls_context) and never reference tenant_id.
--
-- Schema must match cache.py EXACTLY:
--   get():  SELECT payload FROM di_cache
--           WHERE cache_key = $1 AND (expires_at IS NULL OR expires_at > now())
--   set():  INSERT INTO di_cache (cache_key, payload, expires_at, updated_at)
--           VALUES ($1, $2::jsonb, now() + ($3 || ' seconds')::interval, now())
--           ON CONFLICT (cache_key) DO UPDATE ...
--
-- Additive + idempotent.
--
-- Apply manually (compose initdb only runs on first boot):
--   docker exec -i oracle-db-1 psql -U postgres -d oracle < 0020_di_cache.sql

BEGIN;

CREATE TABLE IF NOT EXISTS di_cache (
    cache_key   text        PRIMARY KEY,            -- source-namespaced key; ON CONFLICT target
    payload     jsonb       NOT NULL,               -- cached value (set() writes $2::jsonb)
    expires_at  timestamptz,                        -- nullable: NULL = never expires (get() checks)
    updated_at  timestamptz NOT NULL DEFAULT now()  -- last write; set() refreshes to now()
);

-- Lets a future janitor purge expired rows cheaply (DELETE ... WHERE expires_at < now()).
CREATE INDEX IF NOT EXISTS idx_di_cache_expires_at ON di_cache (expires_at);

COMMENT ON TABLE di_cache IS
    'Global source-namespaced integration cache (L2 for IntegrationCache + AVM). No tenant data; no RLS.';

COMMIT;
