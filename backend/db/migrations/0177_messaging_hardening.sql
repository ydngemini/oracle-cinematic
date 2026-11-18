-- ---------------------------------------------------------------------------
-- 0177 — hosted-SMS hardening (external audits 25.6, 25.7, 25.8).
--
-- 25.6: hosted-order status advanced only when someone pressed refresh (the
-- webhook acknowledged order events without acting). The 10DLC sweep now also
-- re-reads every non-final hosted order; hosted_order_checked_at records the
-- last read and hosted_order_status_at when the status last changed, so a
-- long-pending order is alerted on.
--
-- 25.7: the business-number ownership code (OTP) could be guessed without
-- limit: _MAX_VERIFICATION_ATTEMPTS / _VERIFICATION_LOCKOUT_SECONDS existed
-- but nothing used them. A per-route attempt counter and lockout now gate
-- each submission, reserved atomically before the provider is asked.
--
-- 25.8: a later delivery receipt that contradicted an earlier final one was
-- dropped without a trace. sms_delivery_receipts journals every receipt and
-- marks a contradicting final one; the stored status is not changed by it —
-- a person reconciles (with the provider's own record).
-- No personal data: provider message ids, statuses and carrier error text.
-- ---------------------------------------------------------------------------

ALTER TABLE messaging_routes
    ADD COLUMN IF NOT EXISTS verification_attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS verification_locked_until  timestamptz,
    ADD COLUMN IF NOT EXISTS verification_attempted_at  timestamptz,
    ADD COLUMN IF NOT EXISTS hosted_order_checked_at    timestamptz,
    ADD COLUMN IF NOT EXISTS hosted_order_status_at     timestamptz;

ALTER TABLE messaging_routes
    DROP CONSTRAINT IF EXISTS messaging_routes_verification_attempts_chk;
ALTER TABLE messaging_routes
    ADD CONSTRAINT messaging_routes_verification_attempts_chk CHECK (verification_attempt_count >= 0);

-- Existing routes: their status has held since at least their last update.
-- The updated_at trigger is paused for the backfill so it does not reset
-- every route's updated_at to the migration time (the table's owner may).
ALTER TABLE messaging_routes DISABLE TRIGGER trg_messaging_routes_updated;
UPDATE messaging_routes SET hosted_order_status_at = updated_at WHERE hosted_order_status_at IS NULL;
ALTER TABLE messaging_routes ENABLE TRIGGER trg_messaging_routes_updated;
ALTER TABLE messaging_routes ALTER COLUMN hosted_order_status_at SET DEFAULT now();

-- Every path that changes the hosting status stamps when.
CREATE OR REPLACE FUNCTION messaging_routes_hosted_status_at() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.hosted_order_status IS DISTINCT FROM OLD.hosted_order_status THEN
        NEW.hosted_order_status_at := now();
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS trg_messaging_routes_hosted_status_at ON messaging_routes;
CREATE TRIGGER trg_messaging_routes_hosted_status_at
    BEFORE UPDATE ON messaging_routes
    FOR EACH ROW EXECUTE FUNCTION messaging_routes_hosted_status_at();

CREATE INDEX IF NOT EXISTS idx_messaging_routes_hosted_pending
    ON messaging_routes (hosted_order_checked_at NULLS FIRST)
    WHERE active AND hosted_order_id IS NOT NULL
      AND hosted_order_status IN ('pending_verification','processing','pending_documents','manual_action_required');

CREATE TABLE IF NOT EXISTS sms_delivery_receipts (
    id                  bigserial PRIMARY KEY,
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider            text NOT NULL CHECK (provider IN ('telnyx','twilio')),
    provider_message_id text NOT NULL CHECK (length(provider_message_id) BETWEEN 1 AND 128),
    status              text NOT NULL CHECK (length(status) BETWEEN 1 AND 40),
    error_reason        text CHECK (error_reason IS NULL OR length(error_reason) <= 500),
    -- the provider's own event id and time (a redelivered event is one row)
    provider_event_id   text CHECK (provider_event_id IS NULL OR length(provider_event_id) <= 128),
    occurred_at         timestamptz,
    received_at         timestamptz NOT NULL DEFAULT now(),
    -- a final status that disagrees with the final status already stored
    contradicts         text CHECK (contradicts IS NULL OR length(contradicts) <= 40),
    reconciled_at       timestamptz
);

CREATE INDEX IF NOT EXISTS idx_sms_delivery_receipts_message
    ON sms_delivery_receipts (tenant_id, provider, provider_message_id, received_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_delivery_receipts_event
    ON sms_delivery_receipts (provider, provider_event_id) WHERE provider_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sms_delivery_receipts_contradictions
    ON sms_delivery_receipts (received_at) WHERE contradicts IS NOT NULL AND reconciled_at IS NULL;

COMMENT ON TABLE sms_delivery_receipts IS
    'Every SMS delivery receipt, with contradicting final receipts marked for reconciliation (0177, audit 25.8).';

ALTER TABLE sms_delivery_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sms_delivery_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sms_delivery_receipts_tenant_isolation ON sms_delivery_receipts;
CREATE POLICY sms_delivery_receipts_tenant_isolation ON sms_delivery_receipts
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

REVOKE ALL ON sms_delivery_receipts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON sms_delivery_receipts TO oracle_app;
GRANT USAGE, SELECT ON SEQUENCE sms_delivery_receipts_id_seq TO oracle_app;
