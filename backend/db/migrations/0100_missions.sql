-- 0100 — Missions: tell Neoh the outcome, Neoh figures out the work.
--
-- Everything before this file reports. A mission is the first thing that
-- *pursues*: an objective stated in the agent's own words, a deadline, a
-- budget, and a set of channels it may use. The engine then finds candidates,
-- plans a sequence, and — only under the conditions below — executes it.
--
-- The safety design is the point of this schema, so it lives in constraints
-- rather than in application code:
--
-- 1. **The standing autonomy dial is unchanged.** 0095 pins calls/texts/emails
--    to at most 'assist' at the database, and this migration does not touch
--    that. A mission does not raise the dial; it carries its OWN grant, which
--    is explicit, consented, per-mission and revocable, and which authorises
--    releasing that mission's own approvals and nothing else.
--
-- 2. **A grant without recorded consent is impossible.** `auto_channels`
--    non-empty requires `consent_at` and the verbatim `consent_text` the agent
--    agreed to. Not a boolean — the sentence itself, because "they ticked a
--    box" is not a record of what the box said.
--
-- 3. **Live requires a simulation.** `mode='live'` requires `simulated_at`.
--    Nobody may point this at their database and press go without first seeing
--    what it would do.
--
-- 4. **Every action is a row before it is an act.** `mission_actions` records
--    the plan, and `state` distinguishes 'would_have_done' (shadow, or blocked,
--    or no credentials) from 'staged'/'approved'/'executed'. Shadow mode is
--    therefore not a code path that skips work; it is the same work with the
--    last step withheld, which is the only kind of dry run worth trusting.
--
-- AI voice additionally requires express WRITTEN consent per contact under FCC
-- 24-17. That is enforced by guard_outreach at execution time regardless of
-- what a mission was granted, and the reason lands in blocked_reason.

BEGIN;

-- ---------------------------------------------------------------------------
-- missions — one stated outcome
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS missions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    objective_kind text NOT NULL CHECK (objective_kind IN (
                       'listings_won', 'buyers_converted', 'appointments_set',
                       'database_reactivated', 'sphere_touched', 'deals_saved')),
    -- The agent's own words, kept verbatim. The planner is shown this rather
    -- than a normalised summary, and a person reading the mission later needs
    -- to see what was actually asked for, not what we decided it meant.
    objective_text text NOT NULL CHECK (length(btrim(objective_text)) > 0),

    target_count   integer CHECK (target_count IS NULL OR target_count > 0),
    deadline       timestamptz,
    budget_cents   integer NOT NULL DEFAULT 0 CHECK (budget_cents >= 0),

    -- What the mission may use at all.
    allowed_channels text[] NOT NULL DEFAULT '{}'::text[]
        CHECK (allowed_channels <@ ARRAY['email','sms','voice','task']::text[]),

    -- The grant: channels this mission may release its OWN approvals on,
    -- without a person clicking each one. A subset of what it may use at all.
    auto_channels  text[] NOT NULL DEFAULT '{}'::text[]
        CHECK (auto_channels <@ allowed_channels),
    consent_at     timestamptz,
    consent_by     text,
    -- The sentence the agent agreed to, verbatim. See note 2 above.
    consent_text   text,

    constraints        jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- What the dial said when this mission was created. Recorded so a later
    -- audit can tell a mission that was granted autopilot from one that merely
    -- ran while the dial happened to be permissive.
    autonomy_snapshot  jsonb NOT NULL DEFAULT '{}'::jsonb,

    status text NOT NULL DEFAULT 'draft' CHECK (status IN (
               'draft', 'simulated', 'shadow', 'active',
               'paused', 'completed', 'failed', 'cancelled')),
    mode   text NOT NULL DEFAULT 'shadow' CHECK (mode IN ('shadow', 'live')),

    simulated_at   timestamptz,
    launched_at    timestamptz,
    completed_at   timestamptz,
    paused_reason  text,

    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text,
    updated_at timestamptz NOT NULL DEFAULT now(),

    -- An autopilot grant cannot exist without the consent that authorised it,
    -- and the consent is not a flag — it is the sentence.
    CONSTRAINT missions_grant_requires_consent CHECK (
        auto_channels = '{}'::text[]
        OR (consent_at IS NOT NULL AND consent_text IS NOT NULL
            AND length(btrim(consent_text)) > 0)),

    -- Nobody goes live on their real database without seeing a simulation.
    CONSTRAINT missions_live_requires_simulation CHECK (
        mode = 'shadow' OR simulated_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_missions_tenant_status
    ON missions (tenant_id, status, updated_at DESC);

-- The tick sweep asks only for missions that could act. Literal-matchable so
-- the planner can use it.
CREATE INDEX IF NOT EXISTS idx_missions_runnable
    ON missions (tenant_id, updated_at)
    WHERE status IN ('shadow', 'active');

ALTER TABLE missions ENABLE ROW LEVEL SECURITY;
ALTER TABLE missions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS missions_tenant_isolation ON missions;
CREATE POLICY missions_tenant_isolation ON missions
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON missions TO oracle_app;

-- ---------------------------------------------------------------------------
-- mission_candidates — who the mission is considering, and who it ruled out
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mission_candidates (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mission_id   uuid NOT NULL REFERENCES missions(id) ON DELETE CASCADE,

    subject_type text NOT NULL CHECK (subject_type IN ('client', 'lead', 'contact')),
    subject_id   text NOT NULL,
    client_id    uuid,

    state        text NOT NULL DEFAULT 'proposed' CHECK (state IN (
                     'proposed', 'selected', 'excluded', 'exhausted')),
    -- Why this person is not being worked. An exclusion with no reason is
    -- indistinguishable from a bug, and this is the column a person reads when
    -- they ask why their best lead was skipped.
    excluded_reason text,
    score        numeric,
    evidence     jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT mission_candidates_unique_subject
        UNIQUE (mission_id, subject_type, subject_id),
    CONSTRAINT mission_candidates_exclusion_has_a_reason CHECK (
        state <> 'excluded' OR excluded_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_mission_candidates_selected
    ON mission_candidates (mission_id, state);

ALTER TABLE mission_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE mission_candidates FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mission_candidates_tenant_isolation ON mission_candidates;
CREATE POLICY mission_candidates_tenant_isolation ON mission_candidates
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON mission_candidates TO oracle_app;

-- ---------------------------------------------------------------------------
-- mission_actions — every act, as a row, before it is an act
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mission_actions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mission_id    uuid NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    candidate_id  uuid REFERENCES mission_candidates(id) ON DELETE SET NULL,

    step_index    integer NOT NULL DEFAULT 0 CHECK (step_index >= 0),
    channel       text NOT NULL CHECK (channel IN ('email', 'sms', 'voice', 'task')),
    due_at        timestamptz,

    -- 'would_have_done' is the shadow/blocked/not-ready terminal: the action
    -- was fully evaluated and deliberately not performed. It is enriched in
    -- place rather than superseded by a second row, so the count of actions is
    -- the count of intentions either way.
    state         text NOT NULL DEFAULT 'planned' CHECK (state IN (
                      'planned', 'would_have_done', 'staged', 'approved',
                      'executed', 'blocked', 'skipped', 'failed')),
    blocked_reason text,

    cost_cents    integer NOT NULL DEFAULT 0 CHECK (cost_cents >= 0),
    predicted_probability numeric CHECK (
        predicted_probability IS NULL
        OR (predicted_probability >= 0 AND predicted_probability <= 1)),
    predicted_value numeric,

    -- Composite FK: a command belongs to a tenant, and a mission may only ever
    -- point at a command in its own.
    command_id      uuid,
    decision_trace_id uuid REFERENCES ai_decision_traces(id) ON DELETE SET NULL,
    outcome_event_id  uuid REFERENCES outcome_events(id) ON DELETE SET NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT mission_actions_command_fk
        FOREIGN KEY (tenant_id, command_id)
        REFERENCES command_executions (tenant_id, id) ON DELETE SET NULL,

    -- A withheld or blocked action must say why. This is the column the UI
    -- shows when a mission reports it did nothing.
    CONSTRAINT mission_actions_withheld_has_a_reason CHECK (
        state NOT IN ('would_have_done', 'blocked') OR blocked_reason IS NOT NULL)
);

-- The executor asks for exactly this, so the predicate is literal-matchable.
CREATE INDEX IF NOT EXISTS idx_mission_actions_planned
    ON mission_actions (mission_id, due_at)
    WHERE state = 'planned';

CREATE INDEX IF NOT EXISTS idx_mission_actions_mission
    ON mission_actions (mission_id, created_at DESC);

-- The evaluator joins attributed outcomes to actions still awaiting one.
CREATE INDEX IF NOT EXISTS idx_mission_actions_awaiting_outcome
    ON mission_actions (tenant_id, candidate_id)
    WHERE outcome_event_id IS NULL;

ALTER TABLE mission_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE mission_actions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mission_actions_tenant_isolation ON mission_actions;
CREATE POLICY mission_actions_tenant_isolation ON mission_actions
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON mission_actions TO oracle_app;
-- No DELETE: an action that was planned is a thing the system intended, and
-- deleting it would make a mission's own history disagree with its receipts.
REVOKE DELETE ON mission_actions FROM oracle_app;

-- ---------------------------------------------------------------------------
-- mission_events — the journal
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mission_events (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mission_id uuid NOT NULL REFERENCES missions(id) ON DELETE CASCADE,

    kind       text NOT NULL CHECK (kind IN (
                   'created', 'simulated', 'launched', 'paused', 'resumed',
                   'planned', 'plan_failed', 'candidate_added', 'candidate_excluded',
                   'action_withheld', 'action_staged', 'action_released',
                   'action_blocked', 'budget_exhausted', 'strategy_changed',
                   'completed', 'failed', 'cancelled')),
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mission_events_mission
    ON mission_events (mission_id, occurred_at DESC);

ALTER TABLE mission_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE mission_events FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS mission_events_tenant_isolation ON mission_events;
CREATE POLICY mission_events_tenant_isolation ON mission_events
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT ON mission_events TO oracle_app;
REVOKE UPDATE, DELETE ON mission_events FROM oracle_app;

-- ---------------------------------------------------------------------------
-- tenant_action_budgets — a ceiling above every mission
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_action_budgets (
    tenant_id         uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    monthly_cap_cents integer NOT NULL DEFAULT 0 CHECK (monthly_cap_cents >= 0),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text
);

ALTER TABLE tenant_action_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_action_budgets FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_action_budgets_isolation ON tenant_action_budgets;
CREATE POLICY tenant_action_budgets_isolation ON tenant_action_budgets
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON tenant_action_budgets TO oracle_app;

COMMIT;
