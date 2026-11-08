-- ---------------------------------------------------------------------------
-- 0160 — the last observed outcome of each property-data provider call
-- (prompt 19 T19.07 / AC19.07, ledger L19-05).
--
-- RentCast and ATTOM (avm_client) answered 401/429/503 into a log line and an
-- in-process cooldown only, so health could not tell a working provider from a
-- rejected key (L19-04 reported them UNKNOWN). One row per provider, upserted
-- after every call by provider_health.record: when it last worked, when it last
-- failed and how, and how many calls in a row have failed.
--
-- Platform reference data like mls_sync_status: no tenant, no customer data,
-- no address, no error text — a provider name, timestamps, a fixed failure
-- kind and a counter. So no RLS (rls_security_review checks tenant tables).
--
-- Rollback: DROP TABLE provider_health.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_health (
    provider              text PRIMARY KEY CHECK (provider IN ('rentcast', 'attom')),
    last_ok_at            timestamptz,
    last_failure_at       timestamptz,
    last_failure_kind     text CHECK (last_failure_kind IS NULL OR last_failure_kind IN (
                              'credential_rejected', 'rate_limited', 'http_error', 'timeout',
                              'unavailable', 'bad_response')),
    consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON provider_health TO oracle_app;

COMMENT ON TABLE provider_health IS
    '0160 (L19-05): last observed outcome per property-data provider (RentCast, ATTOM), written by provider_health.record after each call; read by component_health. No tenant or customer data.';
