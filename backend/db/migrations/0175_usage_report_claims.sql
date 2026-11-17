-- ---------------------------------------------------------------------------
-- 0175 — a usage report to Stripe is claimed, attempted durably and resolved
-- inside Stripe's deduplication window (external audits 24.5 and 24.6).
--
-- 24.5: drain_usage_to_stripe selected reported_at IS NULL rows, sent the
-- meter event, then stamped reported_at. A network or database failure after
-- Stripe accepted the event left the row unreported, with nothing recording
-- that it may already have been billed, and two worker replicas could send
-- the same unclaimed rows at once. Stripe de-duplicates a meter event's
-- identifier for a rolling period of at least 24 hours only, so a resend days
-- later could bill the lead twice.
--
-- Now the drain claims each row (a short lease, FOR UPDATE SKIP LOCKED) and,
-- in the same statement, records that an attempt that may reach Stripe began
-- (report_uncertain_since) BEFORE calling Stripe. The same identifier may be
-- resent only while the first uncertain attempt is younger than 20 hours,
-- comfortably inside Stripe's window, where a resend is de-duplicated. A row
-- still unacknowledged after that is quarantined as 'uncertain' for a person
-- to reconcile against Stripe; it is never resent automatically.
--
-- 24.6: Stripe accepts meter events whose timestamp is within the past 35
-- days. Older unreported rows stayed eligible forever, failed every sweep and,
-- oldest first, could fill the per-tenant limit ahead of current usage. Rows
-- older than 34 days are now quarantined as 'expired' (a terminal state, kept
-- for a manual billing or credit decision) and drop out of the drain.
--
-- Billing data: same table, same RLS, no new personal data.
-- ---------------------------------------------------------------------------

ALTER TABLE billing_usage_events
    ADD COLUMN IF NOT EXISTS report_attempts        integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS report_claimed_until   timestamptz,
    ADD COLUMN IF NOT EXISTS report_uncertain_since timestamptz,
    ADD COLUMN IF NOT EXISTS report_state           text,
    ADD COLUMN IF NOT EXISTS report_quarantined_at  timestamptz;

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_report_state_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_report_state_chk CHECK (
        (report_state IS NULL AND report_quarantined_at IS NULL)
        OR (report_state IN ('expired', 'uncertain')
            AND report_quarantined_at IS NOT NULL
            AND reported_at IS NULL)
    );

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_report_attempts_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_report_attempts_chk CHECK (report_attempts >= 0);

-- The drain and the sweep read only rows still in play: unreported and not
-- quarantined.
DROP INDEX IF EXISTS idx_billing_usage_events_unreported;
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_unreported
    ON billing_usage_events (tenant_id, occurred_at)
    WHERE reported_at IS NULL AND report_state IS NULL;

-- Quarantined rows, for the operator's reconciliation list.
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_quarantined
    ON billing_usage_events (tenant_id, report_quarantined_at)
    WHERE report_state IS NOT NULL;
