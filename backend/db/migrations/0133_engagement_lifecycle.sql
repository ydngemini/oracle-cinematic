-- 0133 — engagement is metered at delivery, not at the draft (audit A05,
-- prompt 18 T18.06 + AC18.04, prompt 04 T04.07 + AC04.05).
--
-- speed_to_lead.py metered `lead_engaged` — the only metric reported to the
-- Stripe meter — right after it STAGED a draft: before a human approved it and
-- before any provider saw it. The first-response ledger had the same blur:
-- 'staged' counted as a response, and 'sent' was written at provider
-- acceptance while 0067's comment called it delivery.
--
-- The shared vocabulary is backend/outreach_lifecycle.py:
--   staged → approved → accepted (provider took it) → delivered (carrier
--   receipt / answered call) → responded; terminal: cancelled, blocked,
--   skipped, failed.
--
-- 1. lead_response_events gains the stages it lacked and a timestamp per
--    stage, so latency can be measured to acceptance instead of to the draft.
--    'sent' stays allowed: it is what a pre-0133 worker writes during a
--    rolling deploy, and outreach_lifecycle reads it as accepted.
-- 2. billing_usage_events gains lifecycle_stage + source_table/source_id: the
--    durable event a usage row bills for. A lead_engaged row is billable only
--    when it names a delivered command (CHECK below). Rows the old code wrote
--    at the staged step get NULL here by construction (ADD COLUMN, no UPDATE),
--    so they are non-billable without depending on any backfill succeeding
--    under FORCE RLS; billing_usage.BILLABLE_PREDICATE keeps them out of the
--    drain and out of every "pending billing" count. Such a row is promoted in
--    place if its lead is later delivered (same idempotency key), never
--    duplicated. A legacy row a drain already pushed to Stripe is listed by
--    billing_usage.reconcile_lead_engagements for a human — 0133 cannot
--    un-send a meter event.
--
-- No new functions (nothing to GRANT). No new tables. New columns hold stage
-- names, timestamps and a command id — no personal data.
--
-- Rollback: the columns and constraints are additive. To roll back the code
-- only, leave them; the pre-0133 writers ignore them, and the legacy rows they
-- write stay non-billable. To drop: ALTER TABLE … DROP CONSTRAINT
-- billing_usage_lead_engaged_delivered_chk, DROP COLUMN …; restore the 0067
-- disposition CHECK after mapping 'accepted'/'delivered'/'approved'/'cancelled'
-- rows back to 'sent'/'staged'.

BEGIN;

-- ── 1. First-response ledger ───────────────────────────────────────────────
ALTER TABLE lead_response_events
    ADD COLUMN IF NOT EXISTS approved_at  timestamptz,
    ADD COLUMN IF NOT EXISTS accepted_at  timestamptz,
    ADD COLUMN IF NOT EXISTS delivered_at timestamptz;

COMMENT ON COLUMN lead_response_events.responded_at IS
    'When this attempt row was written (the draft, block or skip) — NOT when anyone was reached. See accepted_at/delivered_at (0133).';
COMMENT ON COLUMN lead_response_events.accepted_at IS
    'When a provider accepted the approved message (command_executions.provider_submitted_at). Latency is measured to this.';
COMMENT ON COLUMN lead_response_events.delivered_at IS
    'When the carrier reported delivery (text) or an answer (call). The only stage that meters lead_engaged.';

ALTER TABLE lead_response_events
    DROP CONSTRAINT IF EXISTS lead_response_events_disposition_check;
ALTER TABLE lead_response_events
    ADD CONSTRAINT lead_response_events_disposition_check CHECK (disposition IN (
        'staged','approved','accepted','delivered','cancelled',
        'blocked','skipped','failed',
        'sent'  -- pre-0133 spelling of accepted; read as accepted
    ));

-- Backfill (best effort, like 0131's: a migrator subject to FORCE RLS updates
-- nothing, and that is safe — outreach_lifecycle reads 'sent' as accepted and
-- an untimed acceptance is reported as such, never guessed).
UPDATE lead_response_events r
   SET disposition = 'accepted',
       accepted_at = COALESCE(r.accepted_at, c.provider_submitted_at)
  FROM command_executions c
 WHERE r.disposition = 'sent'
   AND c.id = r.command_id AND c.tenant_id = r.tenant_id;
UPDATE lead_response_events SET disposition = 'accepted' WHERE disposition = 'sent';
UPDATE lead_response_events r
   SET disposition = 'cancelled'
  FROM command_executions c
 WHERE r.disposition = 'staged'
   AND c.id = r.command_id AND c.tenant_id = r.tenant_id
   AND c.state = 'cancelled';

-- ── 2. Usage rows name the event they bill for ─────────────────────────────
ALTER TABLE billing_usage_events
    ADD COLUMN IF NOT EXISTS lifecycle_stage text,
    ADD COLUMN IF NOT EXISTS source_table    text,
    ADD COLUMN IF NOT EXISTS source_id       uuid;

ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_lifecycle_stage_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_lifecycle_stage_chk CHECK (
        lifecycle_stage IS NULL
        OR lifecycle_stage IN ('staged','approved','accepted','delivered','responded')
    );

-- The A05 invariant, in the schema: a lead_engaged row with a stage is a
-- delivered command. NULL stage = written before 0133 = never billable.
ALTER TABLE billing_usage_events
    DROP CONSTRAINT IF EXISTS billing_usage_lead_engaged_delivered_chk;
ALTER TABLE billing_usage_events
    ADD CONSTRAINT billing_usage_lead_engaged_delivered_chk CHECK (
        metric <> 'lead_engaged'
        OR lifecycle_stage IS NULL
        OR (lifecycle_stage = 'delivered'
            AND source_table = 'command_executions'
            AND source_id IS NOT NULL)
    );

-- Reconciliation joins usage → command (billing_usage.reconcile_lead_engagements).
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_source
    ON billing_usage_events (tenant_id, source_id)
    WHERE source_id IS NOT NULL;

DO $$
DECLARE staged_rows bigint; reported_rows bigint;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE reported_at IS NOT NULL)
      INTO staged_rows, reported_rows
      FROM billing_usage_events
     WHERE metric = 'lead_engaged' AND lifecycle_stage IS NULL;
    IF staged_rows > 0 THEN
        RAISE NOTICE '0133: % lead_engaged row(s) were metered at the staged step and are now non-billable', staged_rows;
    END IF;
    IF reported_rows > 0 THEN
        RAISE WARNING '0133: % staged-step lead_engaged row(s) were ALREADY reported to Stripe; reconcile them (billing_usage.reconcile_lead_engagements)', reported_rows;
    END IF;
END $$;

COMMIT;
