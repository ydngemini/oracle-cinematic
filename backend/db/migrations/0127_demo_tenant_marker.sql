-- 0127: an explicit marker for demo brokerages.
--
-- The sales demo runs against a seeded brokerage on staging
-- (scripts/seed-demo-tenant.py). Its reset tool deletes and re-creates that
-- tenant's CRM rows, so it must be impossible to point it at a real tenant by
-- mistake. Naming conventions (a slug prefix) are a guess; this column is a
-- fact the reset and preflight tools require IN ADDITION to the explicit
-- tenant id they are given. Nothing in the product treats a demo tenant
-- differently; it is a safety interlock for operator tooling only.
--
-- Additive, defaulted, no backfill: every existing tenant is not a demo.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN tenants.is_demo IS
    'Seeded demo brokerage (scripts/seed-demo-tenant.py). Reset/preflight '
    'tooling refuses any tenant where this is false.';
