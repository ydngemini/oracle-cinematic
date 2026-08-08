-- Live agent hand-off for inbound AI calls.
-- Depends on 0055_inbound_voice_routes.sql for telephony_routes.
--
-- agent_forward_e164 is the agent's own reachable phone (cell/desk). It is the
-- transfer TARGET when the AI hands a live call over — the inverse of
-- forwarding_source_e164, which is the agent's legacy number forwarding INTO
-- the Neoh DID. The two are deliberately separate columns.

BEGIN;

ALTER TABLE telephony_routes
    ADD COLUMN IF NOT EXISTS agent_forward_e164        text,
    ADD COLUMN IF NOT EXISTS forward_on_request        boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS forward_when_ai_unavailable boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS forward_timeout_seconds   integer NOT NULL DEFAULT 25;

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_agent_forward_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_agent_forward_chk CHECK (
        (agent_forward_e164 IS NULL OR agent_forward_e164 ~ '^\+[1-9][0-9]{7,14}$')
        AND forward_timeout_seconds BETWEEN 5 AND 120
        -- A hand-off cannot be enabled without a destination to hand off to.
        AND (
            agent_forward_e164 IS NOT NULL
            OR NOT (forward_on_request OR forward_when_ai_unavailable)
        )
    );

-- Never let a route dial its own DID: Twilio would loop the call back into this
-- same webhook until the carrier or the concurrency limit kills it.
ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_forward_not_self_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_forward_not_self_chk CHECK (
        agent_forward_e164 IS NULL OR agent_forward_e164 <> inbound_did
    );

-- Existing rows predate the feature and have no destination on file, so the
-- defaults above must not silently arm a hand-off they cannot complete.
UPDATE telephony_routes
   SET forward_on_request = false,
       forward_when_ai_unavailable = false
 WHERE agent_forward_e164 IS NULL;

-- Outcome of a hand-off attempt, for the call record and the agent's timeline.
ALTER TABLE inbound_voice_calls
    ADD COLUMN IF NOT EXISTS forwarded_at     timestamptz,
    ADD COLUMN IF NOT EXISTS forward_reason   text,
    ADD COLUMN IF NOT EXISTS forward_outcome  text;

ALTER TABLE inbound_voice_calls
    DROP CONSTRAINT IF EXISTS inbound_voice_calls_forward_chk;
ALTER TABLE inbound_voice_calls
    ADD CONSTRAINT inbound_voice_calls_forward_chk CHECK (
        (forward_reason IS NULL OR forward_reason IN (
            'caller_request','ai_unavailable','turn_limit'
        ))
        AND (forward_outcome IS NULL OR forward_outcome IN (
            'requested','connected','no_answer','busy','failed'
        ))
        AND (forward_reason IS NOT NULL OR forwarded_at IS NULL)
    );

CREATE INDEX IF NOT EXISTS idx_inbound_voice_calls_forwarded
    ON inbound_voice_calls (tenant_id, forwarded_at DESC)
    WHERE forwarded_at IS NOT NULL;

COMMIT;
