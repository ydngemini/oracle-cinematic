-- ---------------------------------------------------------------------------
-- 0170 — a brokerage's monthly budget for hosted model tokens, a platform
-- kill switch, and only a broker owner or platform admin may set a budget
-- (prompt 18, T18.07, AC18.05, AC18.06; prompt 07, T07.07).
--
-- Before: a Neoh chat turn could make up to seven hosted model calls, each
-- carrying up to ~40k characters, with no per-brokerage ceiling of any kind:
-- admission limits were per agent and counted messages, not spend, and the
-- deployed gateway path did not even meter its tokens. tenant_action_budgets
-- (0100) held only the missions cents cap, and its policy let ANY member of
-- the brokerage write it, an agent included.
--
-- Unit is tokens as the provider reports them (prompt + completion), not
-- money: the repository has no per-token price table and prices are an owner
-- decision (D-2026-10-12-02). Period is the calendar month in UTC.
--
--   tenant_action_budgets.ai_tokens_monthly_cap
--       NULL  = no cap (every brokerage today; nothing is limited until an
--               owner or platform admin sets one)
--       n > 0 = hosted model calls are refused once used + held + the next
--               call's estimate would pass n
--   tenant_action_budgets.ai_tokens_warn_pct   when the soft warning shows
--       (a threshold the owner chooses, not a measurement)
--   tenant_action_budgets.ai_inference_paused  the brokerage's own switch
--
-- "Used" is the existing ledger: billing_usage_events rows of
-- ai_prompt_tokens / ai_completion_tokens (0084), unchanged here.
--
-- ai_token_reservations holds a call's estimate from just before it is sent
-- until its reported usage is recorded (ai_budget.reserve/settle). Concurrent
-- reservations of one brokerage serialise on a per-brokerage advisory lock,
-- so held + used never passes the cap when a call is admitted. A hold that is
-- never settled (a worker that died mid-call) lapses at expires_at, so the
-- budget can never block a brokerage permanently.
--
-- platform_spend_controls is the platform kill switch, one row per channel
-- ('ai_tokens' only): readable by every context, changed only by a platform
-- admin. No tenant column and no customer data.
--
-- tenant_action_budgets_guard: on an application login (a member of
-- oracle_app that is not a superuser; current_user inside a SECURITY DEFINER
-- function is its owner, so session_user is what is checked, as 0161 does),
-- an INSERT or UPDATE is refused with 42501 unless the session is a platform
-- admin or a broker owner. That covers the missions cap (monthly_cap_cents)
-- too, which an agent's connection could write until now (AC18.05, F18.05).
-- Migrations, erasure and operators on their own role are not the application.
--
-- Previous release on this schema: unaffected (it never writes
-- tenant_action_budgets and never reads the new columns or tables; missions
-- read monthly_cap_cents exactly as before).
--
-- Rollback: DROP TRIGGER tenant_action_budgets_guard ON tenant_action_budgets;
-- DROP FUNCTION tenant_action_budgets_guard(); DROP TABLE ai_token_reservations;
-- DROP TABLE platform_spend_controls; ALTER TABLE tenant_action_budgets
-- DROP COLUMN ai_tokens_monthly_cap, DROP COLUMN ai_tokens_warn_pct,
-- DROP COLUMN ai_inference_paused;
-- ---------------------------------------------------------------------------

ALTER TABLE tenant_action_budgets
    ADD COLUMN IF NOT EXISTS ai_tokens_monthly_cap bigint,
    ADD COLUMN IF NOT EXISTS ai_tokens_warn_pct smallint NOT NULL DEFAULT 80,
    ADD COLUMN IF NOT EXISTS ai_inference_paused boolean NOT NULL DEFAULT false;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenant_action_budgets_ai_cap_positive') THEN
        ALTER TABLE tenant_action_budgets ADD CONSTRAINT tenant_action_budgets_ai_cap_positive
            CHECK (ai_tokens_monthly_cap IS NULL OR ai_tokens_monthly_cap > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenant_action_budgets_ai_warn_pct') THEN
        ALTER TABLE tenant_action_budgets ADD CONSTRAINT tenant_action_budgets_ai_warn_pct
            CHECK (ai_tokens_warn_pct BETWEEN 1 AND 100);
    END IF;
END $$;

COMMENT ON COLUMN tenant_action_budgets.ai_tokens_monthly_cap IS
    '0170 (T18.07): hosted model tokens (prompt + completion, as reported) per calendar month, UTC; '
    'NULL = no cap. Set by a broker owner or platform admin only.';
COMMENT ON COLUMN tenant_action_budgets.ai_tokens_warn_pct IS
    '0170: the soft warning shows at this percent of the cap (the owner''s threshold, not a measurement).';
COMMENT ON COLUMN tenant_action_budgets.ai_inference_paused IS
    '0170: the brokerage''s own switch; true refuses every hosted model call of a Neoh chat turn.';

CREATE OR REPLACE FUNCTION tenant_action_budgets_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT app_is_platform_admin() AND app_current_role() <> 'broker_owner'
       AND pg_has_role(session_user, 'oracle_app', 'MEMBER')
       AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = session_user) THEN
        RAISE EXCEPTION 'only a broker owner or platform admin may change a brokerage''s budgets'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION tenant_action_budgets_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS tenant_action_budgets_guard ON tenant_action_budgets;
CREATE TRIGGER tenant_action_budgets_guard
    BEFORE INSERT OR UPDATE ON tenant_action_budgets
    FOR EACH ROW EXECUTE FUNCTION tenant_action_budgets_guard();

-- ── the platform kill switch ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS platform_spend_controls (
    channel     text PRIMARY KEY CHECK (channel IN ('ai_tokens')),
    halted      boolean NOT NULL DEFAULT false,
    reason      text CHECK (reason IS NULL OR char_length(reason) <= 300),
    -- The platform admin who last changed it (their sign-in id).
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO platform_spend_controls (channel, halted) VALUES ('ai_tokens', false)
    ON CONFLICT (channel) DO NOTHING;

ALTER TABLE platform_spend_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_spend_controls FORCE ROW LEVEL SECURITY;

-- Every context reads the switch (a chat turn must see it); only a platform
-- admin changes it. No tenant column: it is the platform's, not a brokerage's.
DROP POLICY IF EXISTS platform_spend_controls_read ON platform_spend_controls;
CREATE POLICY platform_spend_controls_read ON platform_spend_controls
    FOR SELECT USING (true);
DROP POLICY IF EXISTS platform_spend_controls_admin_write ON platform_spend_controls;
CREATE POLICY platform_spend_controls_admin_write ON platform_spend_controls
    FOR UPDATE USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin());

-- Channels are added by migration, never by the application.
GRANT SELECT, UPDATE ON platform_spend_controls TO oracle_app;
REVOKE INSERT, DELETE ON platform_spend_controls FROM oracle_app;

COMMENT ON TABLE platform_spend_controls IS
    '0170 (T18.07): platform kill switch per spend channel; halted refuses every hosted model call of a '
    'Neoh chat turn on every brokerage. Changed by a platform admin only.';

-- ── holds for calls in flight ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ai_token_reservations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- chat:{assistant message id}:{provider}:{n}; also the usage idempotency key.
    call_key    text NOT NULL CHECK (char_length(call_key) BETWEEN 1 AND 240),
    -- The call's estimate (prompt characters / 3 + its completion ceiling).
    tokens      bigint NOT NULL CHECK (tokens > 0),
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, call_key)
);

CREATE INDEX IF NOT EXISTS idx_ai_token_reservations_live
    ON ai_token_reservations (tenant_id, expires_at);

ALTER TABLE ai_token_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_token_reservations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ai_token_reservations_tenant_isolation ON ai_token_reservations;
CREATE POLICY ai_token_reservations_tenant_isolation ON ai_token_reservations
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- A hold is inserted, counted and deleted; never edited.
GRANT SELECT, INSERT, DELETE ON ai_token_reservations TO oracle_app;
REVOKE UPDATE ON ai_token_reservations FROM oracle_app;

COMMENT ON TABLE ai_token_reservations IS
    '0170 (T18.07): a hosted model call''s token estimate, held from just before it is sent until its '
    'reported usage is recorded; lapses at expires_at. Holds no prompt or answer text.';
