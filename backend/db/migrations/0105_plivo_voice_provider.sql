-- 0105 — Plivo as a second voice carrier, generic call/account identity.
--
-- Neoh is adding Plivo as the PRIMARY telephony rail while keeping Twilio as
-- a legacy/fallback provider (see backend/voice_provider.py). This migration
-- only widens what 0055/0064/0104 already built — it does not introduce a
-- second telephony schema:
--
--   * telephony_routes.provider (0104) already exists but its CHECK only
--     allowed 'twilio'. Widened to 'plivo'.
--   * telephony_routes.twilio_account_sid was NOT NULL with a Twilio-only
--     format CHECK — every non-Twilio route would violate it. A new generic
--     provider_account_id column takes over as the column every route,
--     Twilio or Plivo, always has populated; twilio_account_sid becomes
--     conditionally required (only when provider='twilio') and is kept
--     for backward compatibility with existing code paths and historical
--     rows rather than dropped.
--   * outbound_verification_status/sid/requested_at/last_tested_at/
--     failure_reason and inbound_forwarding_status/provider_sid/
--     last_tested_at/failure_reason (0104) were already provider-neutral
--     names — no rename needed. Plivo-specific additions are
--     outbound_verification_channel (Plivo verification is channel-choice
--     SMS/call; Twilio's Outgoing Caller ID flow has no channel to choose)
--     and a small rate-limit counter pair, since Plivo's flow is
--     agent-submitted-OTP (a guessable-secret surface) where Twilio's is not.
--   * inbound_voice_calls.provider_call_sid was char(34) CHECK'd to Twilio's
--     CAxxxxxxxx...32-hex format. Plivo call UUIDs are standard UUIDs and do
--     not fit that shape at all (different length, contains hyphens). Widened
--     to text with a per-provider CHECK. The column keeps its name — despite
--     "sid" being Twilio terminology, renaming it would touch every read/
--     write site in inbound_voice.py/telephony_api.py for a cosmetic gain;
--     the functional requirement (a Plivo call UUID must be valid without
--     pretending to be a Twilio CallSid) is satisfied by the CHECK change
--     alone.
--
-- agent_call_intents (browser/Twilio-Voice-SDK dialer, 0058) is intentionally
-- untouched: that feature is Twilio Voice SDK client-side dialing, a separate
-- product surface this migration does not move to Plivo.

BEGIN;

-- ── telephony_routes: generic provider + account identity ──────────────────

ALTER TABLE telephony_routes
    ADD COLUMN IF NOT EXISTS provider_account_id text,
    -- Plivo Application ID bound to the hidden forwarding number's answer_url
    -- (Plivo requires an Application resource, not a bare URL, per-number).
    ADD COLUMN IF NOT EXISTS provider_app_id text,
    ADD COLUMN IF NOT EXISTS outbound_verification_channel text,
    ADD COLUMN IF NOT EXISTS outbound_verification_attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS outbound_verification_locked_until timestamptz;

-- Backfill: every existing row is a Twilio row (provider defaults to
-- 'twilio' since 0104), so its account identity already lives in
-- twilio_account_sid.
UPDATE telephony_routes
   SET provider_account_id = twilio_account_sid
 WHERE provider_account_id IS NULL;

ALTER TABLE telephony_routes
    ALTER COLUMN twilio_account_sid DROP NOT NULL;

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_account_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_account_chk CHECK (
        twilio_account_sid IS NULL OR twilio_account_sid ~ '^AC[0-9A-Fa-f]{32}$'
    );

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_provider_account_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_provider_account_chk CHECK (
        provider_account_id IS NOT NULL
        AND length(provider_account_id) BETWEEN 5 AND 64
        AND (provider <> 'twilio' OR provider_account_id = twilio_account_sid)
    );

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_provider_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_provider_chk CHECK (provider IN ('twilio','plivo'));

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_verification_channel_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_verification_channel_chk CHECK (
        outbound_verification_channel IS NULL
        OR outbound_verification_channel IN ('sms','call')
    );

-- ── inbound_voice_calls: generic provider + call identity ───────────────────

ALTER TABLE inbound_voice_calls
    ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'twilio';

ALTER TABLE inbound_voice_calls
    DROP CONSTRAINT IF EXISTS inbound_voice_calls_provider_chk;
ALTER TABLE inbound_voice_calls
    ADD CONSTRAINT inbound_voice_calls_provider_chk CHECK (provider IN ('twilio','plivo'));

ALTER TABLE inbound_voice_calls
    ALTER COLUMN provider_call_sid TYPE text;

ALTER TABLE inbound_voice_calls
    DROP CONSTRAINT IF EXISTS inbound_voice_calls_sid_chk;
ALTER TABLE inbound_voice_calls
    ADD CONSTRAINT inbound_voice_calls_sid_chk CHECK (
        (provider = 'twilio' AND provider_call_sid ~ '^CA[0-9A-Fa-f]{32}$')
        OR (
            provider = 'plivo'
            AND provider_call_sid ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        )
    );

COMMIT;
