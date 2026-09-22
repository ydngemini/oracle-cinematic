-- 0106 — "use my existing business number" (V1 phone connect)
--
-- Reuses telephony_routes (0055) and its carrier-forwarding/caller-ID columns
-- (0055) and live-handoff columns (0064) as-is. No new table: the product's
-- two concepts already map onto existing columns —
--
--   public_business_number (inbound half) == forwarding_source_e164
--     the number the client already dials; the agent's carrier forwards it
--     into inbound_did, which stays a hidden, never-advertised number.
--   public_business_number (outbound half) == voice_caller_id_e164
--     what Neoh asks Twilio to present as caller ID on an AI-placed call.
--
-- In this flow both columns are set to the SAME real-world number at once
-- (inbound_voice.connect_business_number does that), which is exactly why no
-- third "public_business_number" column is added here — it would just be a
-- copy of the two that already exist, and 0055/0090/0102's history in this
-- codebase is full of the cost of fields the planner or the reader can no
-- longer trust to agree with each other.
--
-- What genuinely does not exist yet: proof. voice_caller_id_verified today is
-- a bare boolean the HTTP layer (telephony_api.configure_route, fixed in the
-- same change as this migration) let a client set directly to true with zero
-- Twilio verification behind it — a caller-ID spoofing hole. The columns
-- below give the server somewhere to record the actual state of a real
-- Twilio Outgoing Caller ID verification and a real Twilio number purchase,
-- so voice_caller_id_verified can become something only the server-side
-- verification flow (command_providers.check_twilio_caller_id_verified) is
-- ever allowed to flip.

BEGIN;

ALTER TABLE telephony_routes
    -- Forward-compatible label only, not yet a behaviour switch — Twilio is
    -- the only implementation; ACS/SIP can add rows to this CHECK later
    -- without a schema change to every other column.
    ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'twilio',

    -- Outbound: has Twilio actually confirmed the agent may use this number
    -- as caller ID? 'verified' is the only state place_twilio_call
    -- (commands_api.py, via inbound_voice.get_verified_caller_id) will ever
    -- honour as the client-facing caller ID.
    ADD COLUMN IF NOT EXISTS outbound_verification_status text NOT NULL DEFAULT 'unverified',
    -- Twilio ValidationRequest SID — the idempotency key: a pending request
    -- already on file is polled, never re-requested.
    ADD COLUMN IF NOT EXISTS outbound_verification_sid text,
    ADD COLUMN IF NOT EXISTS outbound_verification_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS outbound_verification_last_tested_at timestamptz,
    ADD COLUMN IF NOT EXISTS outbound_verification_failure_reason text,

    -- Inbound: has the hidden Neoh number actually been purchased and wired
    -- to this route's webhook?
    ADD COLUMN IF NOT EXISTS inbound_forwarding_status text NOT NULL DEFAULT 'not_configured',
    -- Twilio IncomingPhoneNumber SID for inbound_did — the idempotency key
    -- that stops connect_business_number from ever buying a second number
    -- for the same route.
    ADD COLUMN IF NOT EXISTS inbound_forwarding_provider_sid text,
    ADD COLUMN IF NOT EXISTS inbound_forwarding_last_tested_at timestamptz,
    ADD COLUMN IF NOT EXISTS inbound_forwarding_failure_reason text;

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_provider_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_provider_chk CHECK (provider IN ('twilio'));

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_outbound_verification_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_outbound_verification_chk CHECK (
        outbound_verification_status IN ('unverified','pending','verified','failed')
        -- Mirrors telephony_routes_voice_caller_id_chk (0055): the two flags
        -- must never disagree about whether a caller ID is trustworthy.
        AND (outbound_verification_status <> 'verified' OR voice_caller_id_verified)
        AND (NOT voice_caller_id_verified OR outbound_verification_status = 'verified')
    );

ALTER TABLE telephony_routes
    DROP CONSTRAINT IF EXISTS telephony_routes_inbound_forwarding_chk;
ALTER TABLE telephony_routes
    ADD CONSTRAINT telephony_routes_inbound_forwarding_chk CHECK (
        inbound_forwarding_status IN ('not_configured','pending','active','failed')
    );

-- Backfill rows that predate this migration. An operator-typed route with
-- voice_caller_id_verified already true predates real verification and is
-- left exactly as honest as it always was — 'verified' here, not a downgrade,
-- since the CHECK above requires the two columns to agree either way.
UPDATE telephony_routes
   SET outbound_verification_status = 'verified'
 WHERE voice_caller_id_verified
   AND outbound_verification_status = 'unverified';

-- A route with an inbound_did already on file predates this feature and its
-- number, by definition, already works — it just has no purchase SID because
-- nothing here ever bought it.
UPDATE telephony_routes
   SET inbound_forwarding_status = 'active'
 WHERE active
   AND inbound_did IS NOT NULL
   AND inbound_forwarding_status = 'not_configured';

COMMIT;
