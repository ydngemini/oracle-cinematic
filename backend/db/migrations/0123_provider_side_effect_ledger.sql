-- ---------------------------------------------------------------------------
-- 0123 — durable intent for billable provider purchases, and bounded
-- reconciliation of ambiguous command submissions (resilience 2026-10-02).
--
-- provider_purchases: a phone number was bought with no record written first;
-- its SID was saved only after webhook wiring and caller-ID verification, so
-- any failure in between (a timeout, a restart) lost a number that kept
-- billing, and the next attempt bought another. A double-click bought two.
-- Now the intent is written before the purchase, the provider id the moment
-- it is known, and a partial unique index allows only one purchase in flight
-- per agent and provider.
--
-- command_executions.reconcile_*: reconciliation_required had no resolver.
-- The reconciliation sweep (reconciliation.py) asks the provider by id, at
-- most a bounded number of times, then leaves the row for a human.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_purchases (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id      text NOT NULL,
    provider      text NOT NULL CHECK (provider IN ('twilio','plivo')),
    kind          text NOT NULL CHECK (kind IN ('phone_number')),
    state         text NOT NULL DEFAULT 'intended' CHECK (state IN (
                      'intended','confirmed','attached','failed','unknown','released')),
    provider_ref  text,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (state NOT IN ('confirmed','attached','released') OR provider_ref IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_purchases_in_flight
    ON provider_purchases (tenant_id, lower(agent_id), provider, kind)
    WHERE state IN ('intended','unknown','confirmed');
CREATE INDEX IF NOT EXISTS idx_provider_purchases_open
    ON provider_purchases (state, created_at) WHERE state IN ('intended','unknown','confirmed');

ALTER TABLE provider_purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_purchases FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS provider_purchases_tenant_isolation ON provider_purchases;
CREATE POLICY provider_purchases_tenant_isolation ON provider_purchases
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
REVOKE ALL ON provider_purchases FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON provider_purchases FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON provider_purchases TO oracle_app;

ALTER TABLE command_executions
    ADD COLUMN IF NOT EXISTS reconcile_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reconcile_checked_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_command_executions_unresolved
    ON command_executions (updated_at)
    WHERE state IN ('executing','reconciliation_required');

-- The scheduler loop beats on its own row (role 'scheduler'); `detail` carries
-- its tick so a reader knows how stale is too stale. Additive.
ALTER TABLE process_heartbeats ADD COLUMN IF NOT EXISTS detail jsonb NOT NULL DEFAULT '{}'::jsonb;

-- ── ops_alerts ─────────────────────────────────────────────────────────────
-- There was no alerting at all: a dead worker or scheduler was found only by
-- a person. One row per component incident: opened when a component leaves
-- HEALTHY, resolved when it returns. The open→notify gap is the measured
-- time to detect. Platform-level, no tenant data, no RLS.
CREATE TABLE IF NOT EXISTS ops_alerts (
    id            bigserial PRIMARY KEY,
    component     text NOT NULL,
    state         text NOT NULL,
    summary       text NOT NULL,
    opened_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz,
    notified_at   timestamptz,
    notify_error  text
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ops_alerts_open ON ops_alerts (component) WHERE resolved_at IS NULL;
REVOKE ALL ON ops_alerts FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON ops_alerts FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON ops_alerts TO oracle_app;
GRANT USAGE ON SEQUENCE ops_alerts_id_seq TO oracle_app;

-- A call's outcome. busy / no-answer / failed / canceled were all recorded as
-- "completed" (with ended_at overwritten by each duplicate callback), so a
-- call nobody answered read the same as a conversation. Additive.
ALTER TABLE live_call_sessions ADD COLUMN IF NOT EXISTS outcome text;

-- Whether the invitation email actually left. SMTP failure was only logged;
-- the owner saw a "Pending" invite nobody received. Additive.
ALTER TABLE brokerage_invitations
    ADD COLUMN IF NOT EXISTS delivery_status text,
    ADD COLUMN IF NOT EXISTS delivery_error text;
