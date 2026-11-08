-- ---------------------------------------------------------------------------
-- 0169 — one open Stripe Checkout session per brokerage (prompt 18, T18.05,
-- AC18.03).
--
-- POST /billing/create-checkout-session keyed Stripe's idempotency on a
-- ten-minute wall-clock bucket. A double click across a bucket boundary, a
-- second tab, or a return after ten minutes opened another session, each
-- payable for 24 hours, and the one-plan guard could not see any of them: it
-- reads only subscriptions rows, which only the webhook writes. Paying two
-- sessions made two Stripe subscriptions billing one brokerage.
--
-- This table holds the brokerage's current session. Checkout (billing.py),
-- under a per-brokerage advisory lock, hands that session back while it is
-- open (completed_at IS NULL and not about to expire) and creates a new one
-- only after it completed or expired; the Stripe idempotency key then names
-- the session it replaces, not the clock. checkout.session.completed stamps
-- completed_at. One row per tenant (the primary key): a new session replaces
-- the old row.
--
-- The url is a payable link to Stripe's hosted page — a capability, not
-- customer data; the row holds no card, address or contact detail.
-- Erasure deletes it (privacy_data_map: capability_token, erase).
--
-- Previous release on this schema: unaffected (it never reads the table).
--
-- Rollback: DROP TABLE billing_checkout_sessions;
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS billing_checkout_sessions (
    tenant_id          uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    stripe_session_id  text NOT NULL,
    url                text NOT NULL,
    -- Stripe's own expiry for the session (Checkout's default is 24 hours).
    expires_at         timestamptz NOT NULL,
    -- Set by checkout.session.completed; a completed session is never reused.
    completed_at       timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_checkout_sessions_session
    ON billing_checkout_sessions (stripe_session_id);

ALTER TABLE billing_checkout_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_checkout_sessions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS billing_checkout_sessions_tenant_isolation ON billing_checkout_sessions;
CREATE POLICY billing_checkout_sessions_tenant_isolation ON billing_checkout_sessions
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Default privileges (0001) give oracle_app SELECT, INSERT, UPDATE and DELETE
-- on every new table. The application reads, records and replaces a session
-- and never deletes one (erasure and the tenant cascade run as owner), so
-- DELETE is taken back.
GRANT SELECT, INSERT, UPDATE ON billing_checkout_sessions TO oracle_app;
REVOKE DELETE ON billing_checkout_sessions FROM oracle_app;

COMMENT ON TABLE billing_checkout_sessions IS
    '0169 (T18.05): the brokerage''s current Stripe Checkout session, reused until it completes or '
    'expires so one brokerage never holds two payable sessions.';
