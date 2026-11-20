-- ---------------------------------------------------------------------------
-- 0181 — the environment operator's sessions can be ended by the server, and
-- a retired emailed code pauses new codes for 15 minutes.
--
-- 1. operator_sessions (external audit 28.3). The deployment-configured
--    operator (ORACLE_ADMIN_ID, and any environment identity whose role is
--    platform_admin) has no users row, so 0117's epoch check never covered it:
--    logout only cleared the cookie, and a copied operator token kept
--    platform-wide authority until it expired, through logout and through a
--    passphrase rotation. Now every operator sign-in inserts one row here,
--    keyed by the SHA-256 of the token's random `jti`. tenancy.apply_rls_context
--    runs app_begin_operator_session() at the start of every tenant transaction
--    of an operator session (as app_begin_session does for a users row), and
--    the HTTP middleware, the admin gates and the WebSocket admissions prove it
--    through that transaction. The row must exist, be unrevoked and unexpired,
--    and carry the fingerprint of the operator credential configured NOW: the
--    fingerprint is a keyed, stretched hash (operator_sessions.credential_fingerprint)
--    of the passphrase and second-factor material, never the material, so
--    rotating either ends every session issued before. /auth/logout sets
--    revoked_at. Refusal is SQLSTATE 28000 with 0117's message, which the API
--    already turns into 401 "Your session has ended".
--
--    Platform-only: RLS on and forced, and only a platform-admin context
--    (app_is_platform_admin(): the admin GUC AND a login in
--    platform_admin_role, 0120) reads or writes a row. The app may insert and
--    update (revoke); it may not delete. Rows past expires_at are dead weight
--    and may be purged by an operator.
--
-- 2. operator_otp_challenges.locked_at + trigger operator_otp_cooldown
--    (ledger L17-32 owner decision). Five wrong emailed codes retire a
--    challenge (28.1), but a fresh code was issued at once, so a passphrase
--    holder got ~50 guesses a minute across challenges. auth now stamps
--    locked_at when the fifth wrong code retires a challenge, and this
--    BEFORE INSERT trigger refuses any new challenge for that account for 15
--    minutes after it (SQLSTATE 'OTPLK', DETAIL retry_after_seconds=<n>), on
--    every replica. The trigger's query is its own statement, so it sees a
--    lock committed by a concurrent request after this transaction began.
--
-- Previous release on this schema: unaffected. It never reads
-- operator_sessions, never sets locked_at (so the trigger never refuses its
-- inserts), and calls neither function.
--
-- Rollback: DROP TRIGGER operator_otp_cooldown ON operator_otp_challenges;
-- DROP FUNCTION operator_otp_cooldown_guard(); DROP FUNCTION
-- app_begin_operator_session(text, text, text, text, text, text); DROP TABLE
-- operator_sessions; ALTER TABLE operator_otp_challenges DROP COLUMN locked_at.
-- Rolling the application back alone is safe (see above); rolling the schema
-- back while this release serves would refuse every operator request (fail
-- closed).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS operator_sessions (
    -- SHA-256 (hex) of the session token's `jti`; the jti itself is not stored.
    jti_hash               text PRIMARY KEY CHECK (jti_hash ~ '^[0-9a-f]{64}$'),
    -- The environment identity's sign-in id, lower-cased.
    account                text NOT NULL CHECK (account <> ''),
    -- Keyed, stretched hash of the credential that issued the session.
    credential_fingerprint text NOT NULL CHECK (credential_fingerprint ~ '^[0-9a-f]{64}$'),
    issued_at              timestamptz NOT NULL DEFAULT now(),
    expires_at             timestamptz NOT NULL,
    revoked_at             timestamptz,
    CHECK (expires_at > issued_at)
);

CREATE INDEX IF NOT EXISTS idx_operator_sessions_live
    ON operator_sessions (account) WHERE revoked_at IS NULL;

COMMENT ON TABLE operator_sessions IS
    '0181 (audit 28.3): one row per environment-operator sign-in; a session token is accepted only while '
    'its row is unrevoked, unexpired and carries the current operator credential fingerprint.';

ALTER TABLE operator_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE operator_sessions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS operator_sessions_platform ON operator_sessions;
CREATE POLICY operator_sessions_platform ON operator_sessions
    FOR ALL USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin());

-- Default privileges hand the app arwd on every new table; narrow explicitly.
REVOKE ALL ON operator_sessions FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON operator_sessions FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON operator_sessions TO oracle_app;

-- Same three GUCs as app_begin_session (0117), then the registry check.
-- SECURITY INVOKER on purpose: the GUCs are set first, so the policy above
-- admits the caller only when it is a platform-admin context on the platform
-- login — which tenant_tx guarantees for a platform_admin session and nothing
-- else can reach. No extra round trip: it rides the statement apply_rls_context
-- already sends.
CREATE OR REPLACE FUNCTION app_begin_operator_session(
    p_tenant      text,
    p_role        text,
    p_agent       text,
    p_jti_hash    text,
    p_fingerprint text,
    p_scope       text
) RETURNS void
    LANGUAGE plpgsql
    VOLATILE
    SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM set_config('app.current_tenant', p_tenant, true),
            set_config('app.current_role',   p_role,   true),
            set_config('app.current_agent',  p_agent,  true),
            set_config('app.record_scope',   p_scope,  true);

    PERFORM 1 FROM public.operator_sessions s
     WHERE s.jti_hash = p_jti_hash
       AND s.account = lower(p_agent)
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND s.credential_fingerprint = p_fingerprint;

    IF NOT FOUND THEN
        -- 28000 invalid_authorization_specification, 0117's message: the API
        -- answers 401 "Your session has ended" and clears the cookie.
        RAISE EXCEPTION 'session is no longer valid' USING ERRCODE = '28000';
    END IF;
END
$$;

-- 0003 revoked EXECUTE from PUBLIC on every function; grant explicitly.
REVOKE ALL ON FUNCTION app_begin_operator_session(text, text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_begin_operator_session(text, text, text, text, text, text) TO oracle_app;

-- ── the emailed-code cool-down ─────────────────────────────────────────────

ALTER TABLE operator_otp_challenges ADD COLUMN IF NOT EXISTS locked_at timestamptz;

COMMENT ON COLUMN operator_otp_challenges.locked_at IS
    '0181 (L17-32): set when the fifth wrong code retired this challenge; no new challenge is issued to the '
    'account for 15 minutes after it (trigger operator_otp_cooldown).';

CREATE INDEX IF NOT EXISTS idx_operator_otp_challenges_locked
    ON operator_otp_challenges (account, locked_at DESC) WHERE locked_at IS NOT NULL;

CREATE OR REPLACE FUNCTION operator_otp_cooldown_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    SET search_path = pg_catalog, public
AS $$
DECLARE
    last_lock timestamptz;
    wait_seconds integer;
BEGIN
    SELECT max(c.locked_at) INTO last_lock
      FROM public.operator_otp_challenges c
     WHERE c.account = NEW.account
       AND c.locked_at IS NOT NULL
       AND c.locked_at > now() - interval '900 seconds';
    IF last_lock IS NOT NULL THEN
        wait_seconds := greatest(1, ceil(extract(epoch FROM (last_lock + interval '900 seconds' - now())))::integer);
        RAISE EXCEPTION 'operator sign-in codes are paused after a lockout'
            USING ERRCODE = 'OTPLK',
                  DETAIL = format('retry_after_seconds=%s', wait_seconds);
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION operator_otp_cooldown_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS operator_otp_cooldown ON operator_otp_challenges;
CREATE TRIGGER operator_otp_cooldown
    BEFORE INSERT ON operator_otp_challenges
    FOR EACH ROW EXECUTE FUNCTION operator_otp_cooldown_guard();
