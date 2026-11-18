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
-- A quarantined row waits on a person: report_resolution records what they
-- decided (billed by hand, credited, written off), by whom and when; the
-- quarantine itself stays on the row as history.
--
-- Rows the old drain failed on (report_error set, never reported) may have
-- reached Stripe — it stamped the error for ANY failure, including a failed
-- acknowledgement after Stripe accepted. They are carried over as uncertain
-- since their creation (the earliest the attempt could have been), so one
-- older than the window is quarantined on the first sweep, never resent.
--
-- Billing data: same table, same RLS, no new personal data (resolved_by is
-- the operator's agent id, as in the audit chain).
-- ---------------------------------------------------------------------------

ALTER TABLE billing_usage_events
    ADD COLUMN IF NOT EXISTS report_attempts        integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS report_claimed_until   timestamptz,
    ADD COLUMN IF NOT EXISTS report_uncertain_since timestamptz,
    ADD COLUMN IF NOT EXISTS report_state           text,
    ADD COLUMN IF NOT EXISTS report_quarantined_at  timestamptz,
    ADD COLUMN IF NOT EXISTS report_resolution      text,
    ADD COLUMN IF NOT EXISTS report_resolved_at     timestamptz,
    ADD COLUMN IF NOT EXISTS report_resolved_by     text,
    ADD COLUMN IF NOT EXISTS report_resolution_note text;

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_report_state_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_report_state_chk CHECK (
        (report_state IS NULL AND report_quarantined_at IS NULL AND report_resolution IS NULL)
        OR (report_state IN ('expired', 'uncertain')
            AND report_quarantined_at IS NOT NULL
            AND reported_at IS NULL)
    );

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_report_resolution_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_report_resolution_chk CHECK (
        (report_resolution IS NULL AND report_resolved_at IS NULL
         AND report_resolved_by IS NULL AND report_resolution_note IS NULL)
        OR (report_resolution IN ('billed', 'credited', 'written_off')
            AND report_resolved_at IS NOT NULL
            AND char_length(report_resolved_by) BETWEEN 1 AND 200
            AND char_length(coalesce(report_resolution_note, '')) <= 500)
    );

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_report_attempts_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_report_attempts_chk CHECK (report_attempts >= 0);

-- Carry the old drain's failed rows over as uncertain (see header).
UPDATE billing_usage_events
   SET report_uncertain_since = created_at,
       report_attempts = GREATEST(report_attempts, 1)
 WHERE reported_at IS NULL AND report_error IS NOT NULL
   AND report_uncertain_since IS NULL AND report_state IS NULL;

-- The drain and the sweep read only rows still in play: unreported and not
-- quarantined.
DROP INDEX IF EXISTS idx_billing_usage_events_unreported;
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_unreported
    ON billing_usage_events (tenant_id, occurred_at)
    WHERE reported_at IS NULL AND report_state IS NULL;

-- Quarantined rows still waiting on a person, for the operator and health.
DROP INDEX IF EXISTS idx_billing_usage_events_quarantined;
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_quarantined
    ON billing_usage_events (tenant_id, report_quarantined_at)
    WHERE report_state IS NOT NULL AND report_resolution IS NULL;
