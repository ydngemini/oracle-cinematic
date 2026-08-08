-- Speed-to-lead response tracking + usage metering for billing.
--
-- Two independent additions that share a migration because both exist to make
-- revenue measurable:
--
--   1. lead_response_events — the first-response ledger. 0061 built signed lead
--      intake and deterministic routing, but routing ENDS at "assigned to an
--      agent"; nothing ever contacts the lead. The market evidence that made
--      this a priority is in the vault (Research/Deep/2026-08-06 —
--      proptech-revenue-patterns): sub-90-second first response is the single
--      lever with a measured conversion effect. You cannot improve a latency you
--      do not record, so the ledger lands before the automation that feeds it.
--
--      One row per response ATTEMPT, not per success. A compliance block is a
--      first-class outcome here: "we did not call because the state's calling
--      window was closed" is the honest record, and the disposition column keeps
--      it distinguishable from "we never tried". Suppressing blocked rows would
--      make the latency metric a survivorship-biased lie.
--
--   2. billing_usage_events — the metered dimension. billing.py ships one
--      STRIPE_PRICE_ID at quantity=1, which cannot express usage or outcomes.
--      This table is the local source of truth; pushing to a Stripe meter is a
--      separate, optional, idempotent step (STRIPE_METERED_PRICE_ID). Recording
--      locally even when Stripe metering is unconfigured is deliberate: the
--      usage history must survive a pricing-model decision that has not been
--      made yet (see the open question in the vault note).
--
-- Depends on 0012 (clients, leads), 0061 (lead_intake_events, agent_contacts).

BEGIN;

-- ── 1. First-response ledger ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_response_events (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Any of these may be null: a manual CRM lead has no intake event, a
    -- webhook lead has no `leads` row. At least one anchor is required.
    intake_event_id   uuid,
    contact_id        uuid,
    client_id         uuid,
    lead_id           uuid,

    -- 'speed_to_lead' = the automation. 'manual' = an agent got there first,
    -- backfilled from interaction_logs so the metric measures the LEAD's
    -- experience rather than the robot's performance.
    origin            text NOT NULL DEFAULT 'speed_to_lead'
                      CHECK (origin IN ('speed_to_lead','manual')),
    channel           text CHECK (channel IN ('sms','email','voice')),

    -- staged  → a command was created and awaits approval (the human-in-the-loop
    --           default every major CRM ships; see the vault research).
    -- sent    → delivery confirmed downstream.
    -- blocked → the compliance gate denied it. Honest, and counted.
    -- skipped → nothing to contact (no phone/email) or the feature was off.
    -- failed  → the attempt errored.
    disposition       text NOT NULL
                      CHECK (disposition IN ('staged','sent','blocked','skipped','failed')),
    blocked_reason    text CHECK (blocked_reason IS NULL OR char_length(blocked_reason) <= 500),
    command_id        uuid,

    -- The two timestamps the metric is computed from. lead_created_at is copied
    -- rather than joined: the lead row can be edited or deleted, and a latency
    -- ledger that silently changes when its subject changes is not a ledger.
    lead_created_at   timestamptz NOT NULL,
    responded_at      timestamptz NOT NULL DEFAULT now(),

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lead_response_anchor_chk CHECK (
        intake_event_id IS NOT NULL OR contact_id IS NOT NULL
        OR client_id IS NOT NULL OR lead_id IS NOT NULL
    ),
    -- A blocked row must say why; a non-blocked row must not carry a reason.
    CONSTRAINT lead_response_blocked_reason_chk CHECK (
        (disposition = 'blocked') = (blocked_reason IS NOT NULL)
    )
);

-- Latency is derived, never stored: storing it invites the two timestamps and
-- the duration to disagree after a backfill.
CREATE INDEX IF NOT EXISTS idx_lead_response_events_recent
    ON lead_response_events (tenant_id, responded_at DESC);
CREATE INDEX IF NOT EXISTS idx_lead_response_events_intake
    ON lead_response_events (tenant_id, intake_event_id)
    WHERE intake_event_id IS NOT NULL;

-- The metric is "time to FIRST response", so a second attempt on the same lead
-- must not create a competing row. Partial unique indexes per anchor: a lead can
-- have exactly one first-response record per anchor kind.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_response_first_intake
    ON lead_response_events (tenant_id, intake_event_id)
    WHERE intake_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_response_first_lead
    ON lead_response_events (tenant_id, lead_id)
    WHERE lead_id IS NOT NULL AND intake_event_id IS NULL;

DROP TRIGGER IF EXISTS trg_lead_response_events_updated ON lead_response_events;
CREATE TRIGGER trg_lead_response_events_updated BEFORE UPDATE ON lead_response_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ── 2. Usage metering ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS billing_usage_events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Deliberately an open text column with a CHECK rather than an enum: which
    -- unit Oracle actually bills on is an OPEN question (leads engaged vs AI
    -- voice minutes vs closed transactions). An enum would need a migration to
    -- answer it; this needs a one-line CHECK edit.
    metric           text NOT NULL CHECK (metric IN (
                        'lead_engaged','ai_voice_minute','transaction_closed','media_capture'
                     )),
    quantity         numeric(12,3) NOT NULL CHECK (quantity >= 0),
    occurred_at      timestamptz NOT NULL DEFAULT now(),

    -- Usage that is double-counted is a billing incident, so idempotency is a
    -- table constraint and not a convention.
    idempotency_key  text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 240),

    -- Null until pushed to Stripe. Nullable-forever is a valid steady state:
    -- when STRIPE_METERED_PRICE_ID is unset we still record locally.
    reported_at      timestamptz,
    stripe_event_id  text,
    report_error     text,

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_billing_usage_events_period
    ON billing_usage_events (tenant_id, metric, occurred_at DESC);
-- The drain query: everything not yet pushed to Stripe, oldest first.
CREATE INDEX IF NOT EXISTS idx_billing_usage_events_unreported
    ON billing_usage_events (tenant_id, occurred_at)
    WHERE reported_at IS NULL;

DROP TRIGGER IF EXISTS trg_billing_usage_events_updated ON billing_usage_events;
CREATE TRIGGER trg_billing_usage_events_updated BEFORE UPDATE ON billing_usage_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ── RLS ─────────────────────────────────────────────────────────────────────
-- FORCE, matching 0018's posture: a table owner bypassing its own policy is the
-- exact cross-tenant leak 0017/0018 were written to close.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['lead_response_events','billing_usage_events'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I',table_name || '_tenant_isolation',table_name);
        EXECUTE format(
            'CREATE POLICY %I ON %I USING (app_is_platform_admin() OR tenant_id=app_current_tenant()) WITH CHECK (app_is_platform_admin() OR tenant_id=app_current_tenant())',
            table_name || '_tenant_isolation',table_name
        );
    END LOOP;
END $$;

-- Both are ledgers. Neither is ever deleted by the app: a first-response record
-- that can be erased cannot be trusted as evidence of compliance behaviour, and
-- deletable usage rows are a billing-dispute liability.
GRANT SELECT,INSERT,UPDATE ON lead_response_events,billing_usage_events TO oracle_app;
REVOKE DELETE,TRUNCATE ON lead_response_events,billing_usage_events FROM oracle_app;

COMMIT;
