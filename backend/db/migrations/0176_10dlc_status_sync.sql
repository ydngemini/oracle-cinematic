-- ---------------------------------------------------------------------------
-- 0176 — 10DLC brand and campaign status is read back from the provider
-- (external audit 25.2).
--
-- The brand and campaign rows were written once, at submission, with whatever
-- status the submission answered (always pending), and nothing read the
-- provider again: an approved campaign stayed 'pending' here forever, so the
-- number could never be assigned and (since 25.1) texts were refused. A
-- suspended or expired campaign stayed 'approved'.
--
-- Each row now records the provider's own status value, when it was last
-- read, and the last read's error. A campaign can also be 'suspended' or
-- 'expired' (TCR_SUSPENDED / TCR_EXPIRED, or gone at the provider).
-- Status metadata only: no personal data.
-- ---------------------------------------------------------------------------

ALTER TABLE tenant_messaging_brands
    ADD COLUMN IF NOT EXISTS provider_status    text,
    ADD COLUMN IF NOT EXISTS status_checked_at  timestamptz,
    ADD COLUMN IF NOT EXISTS status_check_error text;

ALTER TABLE tenant_messaging_campaigns
    ADD COLUMN IF NOT EXISTS provider_status    text,
    ADD COLUMN IF NOT EXISTS status_checked_at  timestamptz,
    ADD COLUMN IF NOT EXISTS status_check_error text;

ALTER TABLE tenant_messaging_campaigns
    DROP CONSTRAINT IF EXISTS tenant_messaging_campaigns_status_chk;
ALTER TABLE tenant_messaging_campaigns
    ADD CONSTRAINT tenant_messaging_campaigns_status_chk CHECK (status IN (
        'not_started','pending','approved','rejected','failed','suspended','expired'
    ));

-- The sweep reads registrations that can still change.
CREATE INDEX IF NOT EXISTS idx_tenant_messaging_campaigns_sync
    ON tenant_messaging_campaigns (status_checked_at NULLS FIRST)
    WHERE provider_campaign_id IS NOT NULL AND status IN ('pending','approved','suspended');
