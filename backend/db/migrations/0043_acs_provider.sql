-- Migration 0043: Add Azure Communication Services without removing the
-- still-supported Twilio provider or its tenant credentials.
ALTER TABLE provider_credentials
  DROP CONSTRAINT IF EXISTS provider_credentials_provider_check;
ALTER TABLE provider_credentials
  ADD CONSTRAINT provider_credentials_provider_check
  CHECK (provider IN ('google', 'twilio', 'acs', 'ses', 'runpod', 'mls'));
