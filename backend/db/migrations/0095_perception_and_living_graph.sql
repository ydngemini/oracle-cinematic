-- 0095 — Perception, and a memory that knows when it has gone stale
--
-- TWO problems, and they are the same problem seen from either end.
--
-- PERCEPTION. interaction_logs can record that a person was messaged, called or
-- sent a document. It cannot record that they looked at something. The CHECK
-- installed in 0012 permits sms, call_transcript, voice_note, portal_view,
-- document_signed, status_change, email and message — every one of them an
-- action the *brokerage* took, or a state the brokerage changed. Nothing in the
-- set describes what the client did on their own.
--
-- That is why the intent model scores a person off declared fields and staff
-- activity. It is also why 5,252 leads currently score >=80 on motivation with
-- no address attached: the only signals available are ones that say nothing
-- about a specific house. Behavioural intent is the thing every competing
-- product is built on, and this schema could not hold it.
--
-- The new types are all client-originated and all first-party — this is the
-- brokerage's own portal, its own listings, its own emails. No third-party
-- browsing data is implied by any of them, and none of them should ever be
-- populated from a source the tenant does not own.
--
-- MEMORY. A CRM field says "budget: 750000". It does not say who claimed that,
-- when, how sure we are, or whether the last three weeks of behaviour have
-- quietly contradicted it. So a field is either overwritten (and the history is
-- gone) or kept (and it silently rots). Both failures look identical from the
-- outside: confident, wrong, unattributable.
--
-- `beliefs` is the alternative. Each row is one claim with its provenance and
-- its temporal validity attached, so the system can hold "she said Ashburn in
-- June" and "she has searched Reston fourteen times this month" simultaneously,
-- notice that they disagree, and say so — instead of picking one and forgetting
-- that it ever picked.
--
-- The reason to store the disagreement rather than resolve it: resolving it
-- requires knowing which signal wins, and that depends on facts only the agent
-- has. The honest output is "these two disagree, here is each with its source,
-- ask her" — which is also the more useful one.

-- ---------------------------------------------------------------------------
-- 1. Perception — behavioural signal types
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_interaction_type') THEN
        ALTER TABLE interaction_logs DROP CONSTRAINT chk_interaction_type;
    END IF;
    ALTER TABLE interaction_logs ADD CONSTRAINT chk_interaction_type
        CHECK (interaction_type IN (
            -- Pre-existing. Unchanged, and every historical row still validates.
            'sms', 'call_transcript', 'voice_note', 'portal_view',
            'document_signed', 'status_change', 'email', 'message',
            -- Behavioural. What the client did, first-party surfaces only.
            'listing_view',        -- opened a specific property
            'listing_favorite',    -- saved it
            'listing_unfavorite',  -- removed it: a negative signal, and negatives
                                   -- are load-bearing. Silence is not rejection.
            'listing_share',       -- sent it to someone — often the co-decider
            'search',              -- ran a search; payload carries the criteria
            'saved_search',        -- asked to be told about new matches
            'calculator_use',      -- affordability / payment tooling
            'showing_request',     -- asked to see one in person
            'availability_view',   -- opened the scheduling surface without booking
            'email_open',
            'link_click',
            'map_view'
        ));
END $$;

-- The aggregation this exists to serve is "everything one person did recently,
-- by type". client_id leads because behavioural rollups are per-person; the
-- 0008 index is (lead_id, created_at) and answers a different question.
CREATE INDEX IF NOT EXISTS idx_interaction_logs_behaviour
    ON interaction_logs (tenant_id, client_id, interaction_type, created_at DESC)
    WHERE client_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. beliefs — one claim, with where it came from and how long it holds
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS beliefs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Subject is (type, id) rather than a FK because the graph spans tables
    -- that do not share a key space: clients.id is a uuid, a parcel is a text
    -- key, a market is a ZIP. A FK per subject type would mean a table per
    -- subject type, and the join fan-out is the thing this design is avoiding.
    subject_type   text NOT NULL CHECK (subject_type IN (
                       'client', 'lead', 'property', 'household',
                       'transaction', 'market', 'agent')),
    subject_id     text NOT NULL,

    -- 'prefers_area', 'max_budget', 'financing_type', 'timeline',
    -- 'objection', 'must_have', 'deal_breaker', 'decision_role'.
    predicate      text NOT NULL,
    object_value   jsonb NOT NULL,

    -- EPISTEMIC STATUS. The distinction that keeps this from becoming a junk
    -- drawer: what somebody *said* and what the model *guessed* must never be
    -- stored at the same grade, because they decay differently and they carry
    -- different authority when they conflict.
    --   confirmed  — verified against a document or a system of record
    --   reported   — a person stated it; true that they said it, not that it holds
    --   inference  — derived from evidence, and the evidence is attached
    --   hypothesis — worth testing, not worth acting on unattended
    status         text NOT NULL CHECK (status IN
                       ('confirmed', 'reported', 'inference', 'hypothesis')),

    -- Never 1.0. A stored belief is a claim about a person's future behaviour
    -- and no such claim is certain; the CHECK makes that structural rather than
    -- a convention some later caller forgets.
    confidence     numeric(4,3) NOT NULL
                       CHECK (confidence > 0 AND confidence < 1.0),

    -- PROVENANCE. Non-null source_kind is the point of the table: a belief that
    -- cannot say where it came from cannot be shown to an agent as evidence,
    -- and cannot be corrected by one either.
    source_kind    text NOT NULL CHECK (source_kind IN (
                       'sms', 'call', 'email', 'form', 'behaviour',
                       'public_record', 'agent_entry', 'model', 'import')),
    source_ref     text,   -- interaction_logs.id, document id, message id
    source_quote   text,   -- the client's own words, when there are any

    -- TEMPORAL VALIDITY. learned_at is when the evidence *happened*, not when
    -- the row was written — the same distinction 0079 drew for market data,
    -- where conflating the two made an 81-day-old figure look current. A belief
    -- backfilled today from a June call is a June belief and must age like one.
    learned_at     timestamptz NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    -- Some claims have a natural expiry: a pre-approval letter, a lease end, a
    -- school deadline. NULL means "no known expiry", never "true forever".
    valid_until    timestamptz,

    -- REVISION. Nothing is deleted. A belief that stops being true becomes
    -- 'superseded' and keeps pointing at what replaced it, because "she used to
    -- want Ashburn" is itself worth knowing when she asks why you showed her
    -- Reston.
    revision_state text NOT NULL DEFAULT 'active' CHECK (revision_state IN
                       ('active', 'superseded', 'retracted', 'disputed')),
    superseded_by  uuid REFERENCES beliefs(id) ON DELETE SET NULL,

    -- HUMAN CONTROL. Provenance without correction is surveillance: showing an
    -- agent what the system thinks it knows, with no way to fix it, is worse
    -- than not showing them. A pinned belief is agent-asserted and outranks
    -- inference; a retracted one stays for the audit trail and stops being read.
    pinned_at      timestamptz,
    pinned_by      text,
    retracted_at   timestamptz,
    retracted_by   text,
    retraction_reason text,

    created_at     timestamptz NOT NULL DEFAULT now(),

    -- A retraction without an actor is unattributable, and this table's whole
    -- value is attribution.
    CONSTRAINT beliefs_retraction_attributed CHECK (
        (retracted_at IS NULL AND retracted_by IS NULL)
        OR (retracted_at IS NOT NULL AND retracted_by IS NOT NULL)),
    CONSTRAINT beliefs_pin_attributed CHECK (
        (pinned_at IS NULL AND pinned_by IS NULL)
        OR (pinned_at IS NOT NULL AND pinned_by IS NOT NULL)),
    -- A retracted row must not read as active, and an active one must not
    -- claim a successor.
    CONSTRAINT beliefs_state_coherent CHECK (
        (revision_state = 'retracted') = (retracted_at IS NOT NULL)
        AND (superseded_by IS NULL OR revision_state = 'superseded'))
);

-- The hot read: "everything currently believed about this person". Partial,
-- because superseded and retracted rows are the majority over time and are only
-- ever read one-by-one for history. Predicate is a plain equality on a stored
-- column so the planner can match it under a generic plan — 0089 is the
-- cautionary tale for putting an expression in here.
CREATE INDEX IF NOT EXISTS idx_beliefs_subject_active
    ON beliefs (tenant_id, subject_type, subject_id, predicate, learned_at DESC)
    WHERE revision_state IN ('active', 'disputed');

CREATE INDEX IF NOT EXISTS idx_beliefs_tenant_recent
    ON beliefs (tenant_id, recorded_at DESC);

ALTER TABLE beliefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE beliefs FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS beliefs_tenant_isolation ON beliefs;
CREATE POLICY beliefs_tenant_isolation ON beliefs
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- No DELETE. Retraction is a state, not a removal — see the table comment.
GRANT SELECT, INSERT, UPDATE ON beliefs TO oracle_app;

-- ---------------------------------------------------------------------------
-- 3. agent_decisions — what the agent did about what Neoh suggested
-- ---------------------------------------------------------------------------
-- Writing style is the easy half of an Agent Twin and the worthless half. The
-- valuable half is decision policy: which recommendations this agent takes,
-- which they skip, and why. That signal only exists if the skip is recorded as
-- deliberately as the acceptance — an ignored recommendation currently leaves
-- no trace at all, so the system cannot distinguish "wrong suggestion" from
-- "right suggestion, busy afternoon".
--
-- The rationale is the prize. "She's wasting time until she gets pre-approved"
-- is a rule this agent runs and the model does not have. It is captured only
-- when volunteered: an interface that demanded a reason for every dismissal
-- would be abandoned in a week, and the resulting data would be worse than
-- none because it would be uniformly "other".
CREATE TABLE IF NOT EXISTS agent_decisions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         text NOT NULL,

    opportunity_kind text NOT NULL,
    subject_type    text NOT NULL,
    subject_id      text NOT NULL,
    recommended_action text NOT NULL,
    -- What the engine believed at the moment it recommended. Stored, not
    -- re-derived: comparing today's confidence against a decision made under
    -- last month's confidence would score the wrong model.
    recommended_confidence numeric(4,3),
    recommended_rank int,

    outcome         text NOT NULL CHECK (outcome IN
                        ('accepted', 'overridden', 'deferred', 'dismissed')),
    chosen_action   text,
    rationale       text,
    rationale_source text CHECK (rationale_source IN
                        ('agent_typed', 'agent_selected', 'inferred')),

    decided_at      timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- An inferred rationale must not be read back as something the agent said.
    CONSTRAINT agent_decisions_rationale_attributed CHECK (
        rationale IS NULL OR rationale_source IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_user_time
    ON agent_decisions (tenant_id, user_id, decided_at DESC);

ALTER TABLE agent_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_decisions FORCE  ROW LEVEL SECURITY;

-- Per-broker, mirroring ai_tool_operations: how one agent works is not
-- tenant-wide reading material.
DROP POLICY IF EXISTS agent_decisions_isolation ON agent_decisions;
CREATE POLICY agent_decisions_isolation ON agent_decisions
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()));

GRANT SELECT, INSERT, UPDATE ON agent_decisions TO oracle_app;

-- ---------------------------------------------------------------------------
-- 4. autonomy_preferences — the dial, with a ceiling it cannot be turned past
-- ---------------------------------------------------------------------------
-- One global "AI on/off" is the wrong control, because the answer differs by
-- category: an agent may be happy for Neoh to file notes unattended and never
-- want it near a counter-offer. Per-category levels make that expressible.
--
-- The CHECK is the important line in this file. Categories that carry legal,
-- financial or fiduciary consequence are pinned to 'observe' at the DATABASE,
-- so no settings screen, no API caller, no future migration author and no
-- agent-in-a-hurry can raise them. A ceiling enforced in application code is a
-- ceiling until somebody adds a second write path.
CREATE TABLE IF NOT EXISTS autonomy_preferences (
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    category   text NOT NULL CHECK (category IN (
                   'crm_hygiene', 'research', 'drafting', 'texts', 'emails',
                   'calls', 'scheduling', 'offers', 'pricing',
                   'contract_changes', 'legal_financial')),
    level      text NOT NULL CHECK (level IN ('observe', 'assist', 'autopilot')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    PRIMARY KEY (tenant_id, user_id, category),

    CONSTRAINT autonomy_consequential_ceiling CHECK (
        category NOT IN ('offers', 'pricing', 'contract_changes', 'legal_financial')
        OR level = 'observe'),
    -- Outbound communication reaches a real person under the agent's licence
    -- and cannot be un-sent. Assist (draft, human sends) is the ceiling; the
    -- existing outreach compliance gate still applies on top of this.
    CONSTRAINT autonomy_outbound_ceiling CHECK (
        category NOT IN ('calls', 'texts', 'emails')
        OR level IN ('observe', 'assist'))
);

ALTER TABLE autonomy_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_preferences FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS autonomy_preferences_isolation ON autonomy_preferences;
CREATE POLICY autonomy_preferences_isolation ON autonomy_preferences
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()));

GRANT SELECT, INSERT, UPDATE, DELETE ON autonomy_preferences TO oracle_app;
