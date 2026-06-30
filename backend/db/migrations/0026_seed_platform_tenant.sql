-- 0026_seed_platform_tenant.sql
-- The nil/sentinel UUID (00000000-0000-0000-0000-000000000000) is the platform-admin
-- tenant the operator account and the FE serve under, and the RLS bypass keys on it.
-- But it was never inserted as a real `tenants` row, so leads.tenant_id -> tenants(id)
-- (and every other tenant FK) rejected harvested inventory ingested under it. Seed it
-- so the lead firehose (ORACLE_INGEST_TENANT_ID=00000000-…) can persist leads.
-- Idempotent; does not alter RLS behaviour (the bypass checks the GUC value, not the row).
INSERT INTO tenants (id, slug, name)
VALUES ('00000000-0000-0000-0000-000000000000', 'platform', 'Neoh Platform')
ON CONFLICT (id) DO NOTHING;
