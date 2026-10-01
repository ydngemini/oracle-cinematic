-- ---------------------------------------------------------------------------
-- 0121 — the operator's second factor: one-time codes are used once.
--
-- The operator login now requires a TOTP code (totp.py, review AUTH-12). A code
-- stays valid for up to ~90 s (its step ± one), so without a record of which
-- steps were spent, a code read over a shoulder or out of a proxy log could be
-- replayed inside that window. One row per (account, step), inserted in the
-- same request that accepts the code; the primary key makes a second use — on
-- any replica — fail.
--
-- Platform-level, no tenant column, so no RLS. The app may record and read;
-- nothing may rewrite. Rows older than a day are dead weight and may be purged
-- by an operator; they are never needed after their step has passed.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS operator_totp_uses (
    account  text   NOT NULL,
    step     bigint NOT NULL,
    used_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, step)
);

-- Default privileges hand the app arwd on every new table; narrow explicitly.
REVOKE ALL ON operator_totp_uses FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON operator_totp_uses FROM oracle_app;
GRANT SELECT, INSERT ON operator_totp_uses TO oracle_app;

-- Emailed codes (the alternative to an authenticator app): one row per code
-- sent. Only an HMAC of the code is stored, it expires in minutes, it is
-- consumed on first success, and five wrong guesses retire it.
CREATE TABLE IF NOT EXISTS operator_otp_challenges (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account     text NOT NULL,
    code_hmac   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    attempts    integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_operator_otp_challenges_live
    ON operator_otp_challenges (account, created_at DESC) WHERE consumed_at IS NULL;

REVOKE ALL ON operator_otp_challenges FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON operator_otp_challenges FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON operator_otp_challenges TO oracle_app;
