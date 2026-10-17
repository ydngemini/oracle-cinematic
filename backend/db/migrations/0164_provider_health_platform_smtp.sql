-- ---------------------------------------------------------------------------
-- 0164 — the platform email relay reports its last real outcome (prompt 10,
-- T10.09, AC10.05; ledger L10-11).
--
-- sales_api._provider_snapshot counted the email channel as ready whenever
-- ORACLE_SMTP_FROM_EMAIL / ORACLE_SMTP_USERNAME were set (L10-08): a typo in
-- the password, a revoked app password or a blocked port all still read
-- "Ready", and the first sign was a failed customer email. The relay now
-- writes its outcome to provider_health (0160) after every send it makes for
-- a tenant with no mail server of its own, and the snapshot counts it only
-- after a success with no failure since.
--
-- 'platform_smtp' is one row for the platform relay: a provider name,
-- timestamps, a fixed failure kind and a counter. No recipient, subject or
-- error text.
--
-- Previous release on this schema: unaffected (it never writes the new value).
--
-- Rollback: DELETE FROM provider_health WHERE provider = 'platform_smtp';
-- restore the 0160 CHECK (provider IN ('rentcast', 'attom')).
-- ---------------------------------------------------------------------------

ALTER TABLE provider_health DROP CONSTRAINT IF EXISTS provider_health_provider_check;
ALTER TABLE provider_health ADD CONSTRAINT provider_health_provider_check
    CHECK (provider IN ('rentcast', 'attom', 'platform_smtp'));

COMMENT ON TABLE provider_health IS
    '0160/0164: last observed outcome per provider (RentCast, ATTOM, the platform SMTP relay), written after each call; read by component_health and the delivery snapshot. No tenant or customer data.';
