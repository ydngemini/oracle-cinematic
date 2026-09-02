-- 0098 — an outcome that can find the decision it rewards
--
-- Everything the intelligence layer built this week reports preferences and
-- predictions. beliefs says what is claimed, intent_states says what is likely,
-- expected_value says what an hour is worth, agent_twin says which cards the
-- agent takes. None of it knows whether anything WORKED, and every one of them
-- says so in its own caveat. This is the missing half.
--
-- HALF OF IT ALREADY EXISTS. ai_decision_traces (0074) carries outcome_kind,
-- outcome_value, outcome_at and outcome_source, an immutability trigger that
-- permits only those columns to change, and an index built for the reward join.
-- decision_traces.attach_outcome() is idempotent and has had zero production
-- callers since the day it was written. Its source_id is a uuid that only ever
-- holds action_approvals.id, so it can reward an approved command and nothing
-- else — the Agent Twin's decisions key on an untyped text subject_id.
--
-- WHY A TABLE AND NOT JUST attach_outcome. Attaching outcomes only to traces
-- gives a numerator with no denominator. "Of 40 showings, 9 followed something
-- Neoh suggested" is unanswerable from traces alone: the 31 organic showings
-- leave no row anywhere. A Wilson interval without a denominator is precisely
-- the false precision agent_twin exists to refuse. So every outcome is recorded
-- as a FACT here first, then attributed — and an outcome that followed nothing
-- we did is written with attributed_at set and both attribution ids NULL. That
-- row is the base rate, and it is the most important row in the table.
--
-- CLOSED VOCABULARY, deliberately. beliefs keeps an open predicate and a closed
-- subject_type; outcomes are the reverse case. Every consumer of this table —
-- calibration, the twin's "and did it work", mission evaluation — needs a fixed
-- denominator per kind. An open vocabulary is a junk drawer with no denominator.
--
-- occurred_at IS NOT created_at. A deal that closed in March and is entered in
-- April closed in March. 0095 made the same distinction for beliefs and 0079
-- for market data; conflating the two once made an 81-day-old figure look
-- current. Every consumer reads occurred_at.
--
-- outcome_value is RAW. purchase_price, not the commission on it. 0074 set this
-- rule for traces and it holds here: a derived figure bakes in today's rate,
-- and the rate is expected_value's business, not this table's.

-- ---------------------------------------------------------------------------
-- 1. outcome_events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    outcome_kind    text NOT NULL CHECK (outcome_kind IN (
                        'reply_received',      -- a client answered a thread we opened
                        'appointment_booked',  -- a CALENDAR command succeeded
                        'showing_held',        -- a showing resolved to interested/offer_made/passed
                        'no_show',
                        'offer_made',
                        'transaction_closed',
                        'transaction_lost',
                        'contact_suppressed')),-- a channel earned an opt-out: negative, not neutral

    -- Derived, never supplied. A scorer must not infer sign from a kind string,
    -- and a caller must not be able to file a loss as a win.
    outcome_valence smallint NOT NULL GENERATED ALWAYS AS (
                        CASE outcome_kind
                            WHEN 'no_show'            THEN -1
                            WHEN 'transaction_lost'   THEN -1
                            WHEN 'contact_suppressed' THEN -1
                            ELSE 1
                        END) STORED,

    subject_type    text NOT NULL CHECK (subject_type IN
                        ('client', 'lead', 'transaction', 'contact')),
    subject_id      text NOT NULL,
    -- The resolved person, when the subject can be traced to one. NULL means
    -- "could not resolve", never "no client" — consumers that group by person
    -- must treat NULL as a coverage gap, not as a bucket.
    client_id       uuid,

    outcome_value   numeric,
    occurred_at     timestamptz NOT NULL,
    source_table    text NOT NULL,
    source_id       uuid NOT NULL,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(detail) = 'object'),

    -- LATE-BINDING ATTRIBUTION. Written by outcome_memory.attribute_pending,
    -- enriched in place, never as a second row. attribution_model names the
    -- rule that produced it so a later model can re-run over the same facts.
    attributed_at          timestamptz,
    attribution_model      text,
    attributed_trace_id    uuid REFERENCES ai_decision_traces(id) ON DELETE SET NULL,
    attributed_decision_id uuid REFERENCES agent_decisions(id)    ON DELETE SET NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    -- One source row yields one row PER KIND: a showing that resolved to
    -- offer_made is legitimately both showing_held and offer_made. It is never
    -- the same kind twice.
    CONSTRAINT outcome_events_identity
        UNIQUE (tenant_id, source_table, source_id, outcome_kind),

    -- "We looked" and "what rule we used" travel together or not at all.
    CONSTRAINT chk_outcome_attribution_shape CHECK (
        (attributed_at IS NULL AND attribution_model IS NULL)
        OR (attributed_at IS NOT NULL AND attribution_model IS NOT NULL))
);

-- The attribution sweep's work queue. IS NULL is a literal the planner can
-- match under a generic plan (0089 is the cautionary tale).
CREATE INDEX IF NOT EXISTS idx_outcome_events_unattributed
    ON outcome_events (tenant_id, occurred_at)
    WHERE attributed_at IS NULL;

-- Per-person, per-kind history — the read that calibration and the twin make.
CREATE INDEX IF NOT EXISTS idx_outcome_events_client_kind
    ON outcome_events (tenant_id, client_id, outcome_kind, occurred_at DESC)
    WHERE client_id IS NOT NULL;

-- Append-only in everything but attribution. Same argument as 0074: a record of
-- what happened whose facts can be edited afterwards is not evidence. Enforced
-- here because the guarantee has to hold against any writer, including one
-- that has not read this file.
CREATE OR REPLACE FUNCTION outcome_events_immutable()
RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id     IS DISTINCT FROM OLD.tenant_id
       OR NEW.outcome_kind  IS DISTINCT FROM OLD.outcome_kind
       OR NEW.subject_type  IS DISTINCT FROM OLD.subject_type
       OR NEW.subject_id    IS DISTINCT FROM OLD.subject_id
       OR NEW.client_id     IS DISTINCT FROM OLD.client_id
       OR NEW.outcome_value IS DISTINCT FROM OLD.outcome_value
       OR NEW.occurred_at   IS DISTINCT FROM OLD.occurred_at
       OR NEW.source_table  IS DISTINCT FROM OLD.source_table
       OR NEW.source_id     IS DISTINCT FROM OLD.source_id
       OR NEW.detail        IS DISTINCT FROM OLD.detail
       OR NEW.created_at    IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'outcome_events is append-only; only attribution columns may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

REVOKE ALL ON FUNCTION outcome_events_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_outcome_events_immutable ON outcome_events;
CREATE TRIGGER trg_outcome_events_immutable
    BEFORE UPDATE ON outcome_events
    FOR EACH ROW EXECUTE FUNCTION outcome_events_immutable();

ALTER TABLE outcome_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outcome_events FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS outcome_events_tenant_isolation ON outcome_events;
CREATE POLICY outcome_events_tenant_isolation ON outcome_events
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- 0003 revoked PUBLIC. No DELETE: an outcome is history.
GRANT SELECT, INSERT, UPDATE ON outcome_events TO oracle_app;
REVOKE DELETE ON outcome_events FROM oracle_app;

-- ---------------------------------------------------------------------------
-- 2. agent_decisions — the result slot
-- ---------------------------------------------------------------------------
-- agent_decisions.outcome means "did the agent take the suggestion". It is a
-- judgement, not a result, and the column name has already confused one
-- reader. result_* is what happened afterwards, written by attribution, and
-- kept as a separate all-or-nothing group so an accepted card that led nowhere
-- is distinguishable from one nobody has checked yet.
ALTER TABLE agent_decisions
    ADD COLUMN IF NOT EXISTS result_kind    text,
    ADD COLUMN IF NOT EXISTS result_valence smallint,
    ADD COLUMN IF NOT EXISTS result_value   numeric,
    ADD COLUMN IF NOT EXISTS result_at      timestamptz,
    ADD COLUMN IF NOT EXISTS result_source  text;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_decisions_result_shape') THEN
        ALTER TABLE agent_decisions ADD CONSTRAINT agent_decisions_result_shape CHECK (
            (result_kind IS NULL AND result_at IS NULL AND result_source IS NULL
                AND result_valence IS NULL)
            OR (result_kind IS NOT NULL AND result_at IS NOT NULL AND result_source IS NOT NULL
                AND result_valence IS NOT NULL));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. transactions — a deal that dies is a fact, not a cancelled row
-- ---------------------------------------------------------------------------
-- 'cancelled' carries no timestamp, no reason and no value, so a lost deal is
-- indistinguishable from an abandoned draft. The evaluator needs to COUNT loss
-- causes, which is why lost_reason_code has a vocabulary and lost_reason is the
-- agent's own words — the same (code, verbatim) pair agent_decisions uses for
-- rationale.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS lost_at          timestamptz,
    ADD COLUMN IF NOT EXISTS lost_reason      text,
    ADD COLUMN IF NOT EXISTS lost_reason_code text;

-- 0039 installed this NOT VALID and it has never been validated
-- (pg_constraint.convalidated = false, checked before writing this), so
-- dropping and re-adding it NOT VALID loosens nothing.
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_status_chk;
ALTER TABLE transactions ADD CONSTRAINT transactions_status_chk
    CHECK (status IN ('active', 'under_contract', 'closed', 'cancelled', 'lost')) NOT VALID;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_lost_shape_chk') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_lost_shape_chk
            CHECK ((status = 'lost') = (lost_at IS NOT NULL)) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_lost_reason_chk') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_lost_reason_chk
            CHECK (status <> 'lost' OR lost_reason_code IN (
                'price', 'financing', 'inspection', 'competing_offer',
                'client_withdrew', 'listing_expired', 'other')) NOT VALID;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. command_executions — a tenant-scoped key for FKs to bind to
-- ---------------------------------------------------------------------------
-- id is the PK, so (tenant_id, id) is trivially unique. It exists only so a
-- child row can declare its FK tenant-scoped and the database, not the app,
-- refuses a cross-tenant reference. Used by lead_response_events below and by
-- mission_actions in 0099.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'command_executions_tenant_id_key') THEN
        ALTER TABLE command_executions
            ADD CONSTRAINT command_executions_tenant_id_key UNIQUE (tenant_id, id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. lead_response_events — the send it staged can now be found
-- ---------------------------------------------------------------------------
-- command_id has been a bare, unindexed, un-keyed uuid since 0067. The
-- disposition 'sent' has been allowed just as long and never written, because
-- nothing that sends could find the row to update. Both fixed here; the write
-- lands in commands_api with the rest of the send-path bookkeeping.
CREATE INDEX IF NOT EXISTS idx_lead_response_events_command
    ON lead_response_events (tenant_id, command_id)
    WHERE command_id IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lead_response_events_command_fk') THEN
        ALTER TABLE lead_response_events
            ADD CONSTRAINT lead_response_events_command_fk
            FOREIGN KEY (tenant_id, command_id)
            REFERENCES command_executions (tenant_id, id)
            ON DELETE SET NULL NOT VALID;
    END IF;
END $$;
