-- ---------------------------------------------------------------------------
-- 0174 — a carrier's call outcome is kept until its call session exists
-- (external audit 24.4).
--
-- The command worker writes live_call_sessions only after the provider
-- acknowledges the call. An authenticated Plivo status callback that arrives
-- first (a short call, a slow acknowledgement) updated zero rows, wrote no
-- timeline entry and metered nothing, then answered 204 and cleaned up the
-- call state: the outcome — including the only lead_engaged meter of a
-- speed-to-lead call — was gone, and nothing could replay it.
--
-- provider_call_outcomes: every verified outcome is written here BEFORE the
-- carrier is acknowledged; it is applied once the session exists (at once,
-- when the worker creates the session, or by the reconciliation sweep).
-- Platform-level: the call id, a status and a duration — no tenant, no
-- person, no content — so no RLS (as stripe_webhook_events, 0118).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_call_outcomes (
    id                bigserial PRIMARY KEY,
    provider          text NOT NULL CHECK (provider IN ('plivo', 'twilio')),
    provider_call_id  text NOT NULL CHECK (length(provider_call_id) BETWEEN 1 AND 128),
    call_status       text NOT NULL CHECK (length(call_status) BETWEEN 1 AND 40),
    duration_seconds  integer CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    received_at       timestamptz NOT NULL DEFAULT now(),
    applied_at        timestamptz,
    -- one row per (call, status): a carrier retry of the same callback is a no-op
    CONSTRAINT provider_call_outcomes_event UNIQUE (provider, provider_call_id, call_status)
);

-- The apply step looks up pending outcomes by call id, and the sweep scans
-- only pending rows.
CREATE INDEX IF NOT EXISTS idx_provider_call_outcomes_pending
    ON provider_call_outcomes (provider_call_id) WHERE applied_at IS NULL;

COMMENT ON TABLE provider_call_outcomes IS
    'Verified carrier call outcomes kept until their live_call_sessions row exists (0174, audit 24.4).';

-- The app records, applies (applied_at) and expires outcomes; nothing else.
REVOKE ALL ON provider_call_outcomes FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON provider_call_outcomes TO oracle_app;
GRANT USAGE, SELECT ON SEQUENCE provider_call_outcomes_id_seq TO oracle_app;
