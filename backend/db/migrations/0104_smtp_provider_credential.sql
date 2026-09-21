-- Migration 0104: allow 'smtp' in provider_credentials.provider.
--
-- ACS/SES email support was removed from the app in favour of a single SMTP
-- transport (per-agent or tenant-default), but the CHECK constraint from
-- migration 0043 was never updated — every INSERT into provider_credentials
-- for provider='smtp' currently fails with a CheckViolationError. acs/ses
-- are left in the allow-list rather than dropped: no code path inserts them
-- any more, but existing rows (if any) must remain valid under the check.
ALTER TABLE provider_credentials
  DROP CONSTRAINT IF EXISTS provider_credentials_provider_check;
ALTER TABLE provider_credentials
  ADD CONSTRAINT provider_credentials_provider_check
  CHECK (provider IN ('google', 'twilio', 'acs', 'ses', 'smtp', 'runpod', 'mls'));
