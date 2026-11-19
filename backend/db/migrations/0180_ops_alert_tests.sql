-- ── ops_alert_tests (audit 30.3) ───────────────────────────────────────────
-- Go-live needs proof that an alert reaches a person, not only that
-- ORACLE_ALERT_EMAIL is set: a mistyped address or a broken relay passes that
-- check until the first real incident. An operator sends a test alert carrying
-- a one-time code and types the code back; confirmed_at is that proof.
-- Platform-level, no tenant data, no RLS (like ops_alerts). The address itself
-- is never stored: recipient_fingerprint is a SHA-256 prefix of it, so the
-- launch check can tell whether the proof is for the recipient set today.
CREATE TABLE IF NOT EXISTS ops_alert_tests (
    id                    bigserial PRIMARY KEY,
    requested_by          text NOT NULL,
    recipient_fingerprint text NOT NULL,
    -- HMAC (keyed by the server secret) of the one-time code; NULL when
    -- nothing was sent or once it is confirmed.
    code_hash             text,
    -- 'sending' until the relay answers, then 'sent', 'recipient_missing' or
    -- the delivery error's name.
    outcome               text NOT NULL,
    attempts              integer NOT NULL DEFAULT 0,
    requested_at          timestamptz NOT NULL DEFAULT now(),
    confirmed_at          timestamptz,
    confirmed_by          text
);
CREATE INDEX IF NOT EXISTS ix_ops_alert_tests_requested ON ops_alert_tests (requested_at DESC);
REVOKE ALL ON ops_alert_tests FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON ops_alert_tests FROM oracle_app;
GRANT SELECT, INSERT, UPDATE ON ops_alert_tests TO oracle_app;
GRANT USAGE ON SEQUENCE ops_alert_tests_id_seq TO oracle_app;
