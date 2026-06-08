-- ===========================================================================
-- 0004_subscriptions.sql
-- Stripe subscription tracking per tenant.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS subscriptions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    stripe_customer_id   text NOT NULL,
    stripe_subscription_id text UNIQUE NOT NULL,
    status          text NOT NULL DEFAULT 'incomplete',
    plan            text NOT NULL DEFAULT 'oracle_swarm',
    current_period_end timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant ON subscriptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_sub ON subscriptions(stripe_subscription_id);

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY subscriptions_tenant_isolation ON subscriptions
    USING (tenant_id = app_current_tenant() OR app_is_platform_admin());

GRANT SELECT, INSERT, UPDATE ON subscriptions TO oracle_app;

CREATE TRIGGER set_subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
