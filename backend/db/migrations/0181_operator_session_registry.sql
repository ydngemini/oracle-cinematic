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
--    rotating either ends every session issued before. A sign-in and every
--    process start also REVOKE the rows another credential issued, so rolling
--    the credential back does not bring them back. /auth/logout sets
--    revoked_at. Refusal is SQLSTATE 28000 with 0117's message, which the API
--    already turns into 401 "Your session has ended". The account is
--    lower()-cased here, in SQL, on every path (insert, revoke, check).
--
--    Platform-only: RLS on and forced, and only a platform-admin context
--    (app_is_platform_admin(): the admin GUC AND a login in
--    platform_admin_role, 0120) reads or writes a row. The app may insert a
--    row and set revoked_at (a column grant); nothing else. Trigger
--    operator_sessions_guard refuses, for every role including the owner, an
--    un-revoke, a later expiry and any change to the identity columns: a row
--    only ever moves towards ended. Rows past expires_at are dead weight and
--    may be purged (DELETE) by an operator on their own role.
--
-- 2. The emailed-code cool-down (ledger L17-32 owner decision). Five wrong
--    emailed codes retire a challenge (28.1), but a fresh code was issued at
--    once, so a passphrase holder got ~50 guesses a minute across challenges.
--    auth now stamps locked_at when the fifth wrong code retires a challenge
--    (and retires every other live challenge of the account with it);
--    operator_otp_cooldown_seconds() is the seconds left of the 15 minutes
--    after the account's latest lock; auth refuses both a new code and any
--    code typed while it is above 0, and BEFORE INSERT trigger
--    operator_otp_cooldown refuses a new challenge on any path (SQLSTATE
--    'OTPLK', DETAIL retry_after_seconds=<n>), on every replica.
--    uq_operator_otp_challenges_live makes two live challenges for one account
--    impossible: concurrent requests could each insert one (each saw no recent
--    code; neither saw the other's uncommitted row), and the fifth wrong code
--    then retired only one of them. auth also serialises issuance and checking
--    per account (pg_advisory_xact_lock), so the second request waits, sees the
--    first's code and sends no second email. Duplicates already present are
--    retired (the newest kept) before the index is built.
--
-- 3. operator_otp_challenges and operator_totp_uses (0121) get RLS on and
--    forced with a platform-admin policy: a brokerage (request) connection
--    could read the code HMACs or clear locked_at. Every path that touches them
--    (auth's operator second factor, at sign-in and at step-up) runs in the
--    platform context.
--
-- Previous release on this schema: it never reads operator_sessions, never
-- sets locked_at (so the cool-down never refuses its inserts) and calls no new
-- function; it reaches the 0121 tables in the platform context, which the new
-- policies admit. Two of its concurrent code requests for one account can now
-- fail one with a unique violation (500) instead of sending two codes.
--
-- Rollback: DROP TRIGGER operator_otp_cooldown ON operator_otp_challenges;
-- DROP TRIGGER operator_sessions_guard ON operator_sessions; DROP FUNCTION
-- operator_otp_cooldown_guard(), operator_otp_cooldown_seconds(text),
-- operator_sessions_guard(), app_begin_operator_session(text, text, text,
-- text, text, text); DROP INDEX uq_operator_otp_challenges_live; DROP POLICY
-- operator_otp_challenges_platform ON operator_otp_challenges; DROP POLICY
-- operator_totp_uses_platform ON operator_totp_uses; ALTER TABLE ... NO FORCE /
-- DISABLE ROW LEVEL SECURITY on both; DROP TABLE operator_sessions; ALTER
-- TABLE operator_otp_challenges DROP COLUMN locked_at. Rolling the
-- application back alone is safe (see above); rolling the schema back while
-- this release serves would refuse every operator request (fail closed).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS operator_sessions (
    -- SHA-256 (hex) of the session token's `jti`; the jti itself is not stored.
    jti_hash               text PRIMARY KEY CHECK (jti_hash ~ '^[0-9a-f]{64}$'),
    -- The environment identity's sign-in id, lower()-cased in SQL.
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
-- The only column the app may change is revoked_at.
REVOKE ALL ON operator_sessions FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON operator_sessions FROM oracle_app;
GRANT SELECT, INSERT ON operator_sessions TO oracle_app;
GRANT UPDATE (revoked_at) ON operator_sessions TO oracle_app;

-- A row only moves towards ended, whoever writes it.
CREATE OR REPLACE FUNCTION operator_sessions_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    SET search_path = pg_catalog, public
AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
        RAISE EXCEPTION 'an ended operator session cannot be revived or re-dated' USING ERRCODE = '42501';
    END IF;
    IF NEW.expires_at > OLD.expires_at THEN
        RAISE EXCEPTION 'an operator session cannot be extended' USING ERRCODE = '42501';
    END IF;
    IF NEW.jti_hash IS DISTINCT FROM OLD.jti_hash
       OR NEW.account IS DISTINCT FROM OLD.account
       OR NEW.credential_fingerprint IS DISTINCT FROM OLD.credential_fingerprint
       OR NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
        RAISE EXCEPTION 'an operator session''s identity cannot change' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION operator_sessions_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS operator_sessions_guard ON operator_sessions;
CREATE TRIGGER operator_sessions_guard
    BEFORE UPDATE ON operator_sessions
    FOR EACH ROW EXECUTE FUNCTION operator_sessions_guard();

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
    '0181 (L17-32): set when the fifth wrong code retired this challenge; no code is issued to or accepted '
    'from the account for 15 minutes after it (operator_otp_cooldown_seconds, trigger operator_otp_cooldown).';

CREATE INDEX IF NOT EXISTS idx_operator_otp_challenges_locked
    ON operator_otp_challenges (account, locked_at DESC) WHERE locked_at IS NOT NULL;

-- At most one live challenge per account. Retire older duplicates first (a
-- concurrent pair could exist), keeping each account's newest.
UPDATE operator_otp_challenges AS older
   SET consumed_at = now()
 WHERE older.consumed_at IS NULL
   AND EXISTS (SELECT 1 FROM operator_otp_challenges AS newer
                WHERE newer.account = older.account
                  AND newer.consumed_at IS NULL
                  AND (newer.created_at, newer.id) > (older.created_at, older.id));

CREATE UNIQUE INDEX IF NOT EXISTS uq_operator_otp_challenges_live
    ON operator_otp_challenges (account) WHERE consumed_at IS NULL;

-- Seconds left of the account's cool-down (0 when none). The one place the
-- 15 minutes is written; auth calls it before issuing and before checking a code.
CREATE OR REPLACE FUNCTION operator_otp_cooldown_seconds(p_account text)
    RETURNS integer
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, public
AS $$
    SELECT COALESCE(greatest(0, ceil(extract(epoch FROM (max(c.locked_at) + interval '900 seconds' - now())))::integer), 0)
      FROM public.operator_otp_challenges c
     WHERE c.account = p_account
       AND c.locked_at IS NOT NULL
$$;
REVOKE ALL ON FUNCTION operator_otp_cooldown_seconds(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION operator_otp_cooldown_seconds(text) TO oracle_app;

CREATE OR REPLACE FUNCTION operator_otp_cooldown_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    SET search_path = pg_catalog, public
AS $$
DECLARE
    wait_seconds integer;
BEGIN
    wait_seconds := public.operator_otp_cooldown_seconds(NEW.account);
    IF wait_seconds > 0 THEN
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

-- ── the operator's second-factor tables are platform-only ──────────────────

ALTER TABLE operator_otp_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE operator_otp_challenges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS operator_otp_challenges_platform ON operator_otp_challenges;
CREATE POLICY operator_otp_challenges_platform ON operator_otp_challenges
    FOR ALL USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin());

ALTER TABLE operator_totp_uses ENABLE ROW LEVEL SECURITY;
ALTER TABLE operator_totp_uses FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS operator_totp_uses_platform ON operator_totp_uses;
CREATE POLICY operator_totp_uses_platform ON operator_totp_uses
    FOR ALL USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin());
