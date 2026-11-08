-- ---------------------------------------------------------------------------
-- 0165 — the platform Twilio account reports its last real outcome for texts
-- and calls (prompt 10, T10.09, AC10.05; ledger L10-12).
--
-- sales_api._provider_snapshot counted platform SMS and Neoh calls as ready
-- whenever TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN were set and an operator set
-- ORACLE_TWILIO_CREDENTIALS_VALIDATED=true — a flag nothing in the platform
-- ever checked. command_providers.send_twilio_sms and place_twilio_call now
-- write their outcome to provider_health (0160) when they used the platform
-- account (no tenant account of its own), and the snapshot counts a channel
-- only after a success with no failure since. Texts and calls are separate
-- rows: an account that can text may not be able to call.
--
-- Recorded: success, rejected credentials (HTTP 401/403, code 20003), rate
-- limit, timeout. A refusal about one recipient and an answer Twilio may have
-- acted on record nothing. No phone number, body or error text is stored.
--
-- Previous release on this schema: unaffected (it never writes the new values).
--
-- Rollback: DELETE FROM provider_health WHERE provider IN
-- ('platform_twilio_sms', 'platform_twilio_voice'); restore the 0164 CHECK
-- (provider IN ('rentcast', 'attom', 'platform_smtp')).
-- ---------------------------------------------------------------------------

ALTER TABLE provider_health DROP CONSTRAINT IF EXISTS provider_health_provider_check;
ALTER TABLE provider_health ADD CONSTRAINT provider_health_provider_check
    CHECK (provider IN ('rentcast', 'attom', 'platform_smtp', 'platform_twilio_sms', 'platform_twilio_voice'));

COMMENT ON TABLE provider_health IS
    '0160/0164/0165: last observed outcome per provider (RentCast, ATTOM, the platform SMTP relay, the platform Twilio account for texts and calls), written after each call; read by component_health and the delivery snapshot. No tenant or customer data.';
