-- ===========================================================================
-- 0015_outreach_consent.sql — TCPA / mini-TCPA / BIPA outreach compliance spine
--
-- Backs backend/outreach_compliance.py. Installs the consent, suppression, and
-- attempt-frequency surface that every outbound voice/SMS/message path must
-- consult BEFORE contacting a homeowner — the gate the legal audit (Jun 2026)
-- found entirely absent for telephony/SMS.
--
--   1. outreach_consent       — prior-express-(written)-consent records per
--                               contact + channel (TCPA / FCC 24-17 for AI voice;
--                               BIPA voiceprint consent for IL). Retention-aware.
--   2. outreach_suppression   — do-not-contact list: STOP keyword opt-outs,
--                               manual DNC, and known-litigator scrubs. The
--                               authoritative block list checked on every send.
--   3. outreach_attempt_log   — per-contact attempt ledger for frequency caps
--                               (e.g. OK OTSA: max 3 calls / number / 24h) and
--                               quiet-hours auditing.
--
-- All three are tenant-private and use the standard 0001 RLS isolation posture:
-- USING (app_is_platform_admin() OR tenant_id = app_current_tenant()) — platform
-- admins see every tenant's rows; every other role is row-filtered to its tenant.
--
-- Idempotent: IF NOT EXISTS / DO $$ guards throughout; safe to re-apply.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. outreach_consent  (tenant-private)
-- ---------------------------------------------------------------------------
-- consent_type:
--   express_written        — TCPA prior express WRITTEN consent (required for
--                            AI/artificial-voice marketing calls per FCC 24-17,
--                            and for marketing SMS). The strongest basis.
--   express_oral           — prior express consent (sufficient for some
--                            informational, non-marketing contact).
--   prior_business         — established business relationship (narrow; not a
--                            substitute for written consent on AI-voice marketing).
--   biometric_voiceprint   — BIPA written release to collect/store a voiceprint
--                            (required before recording IL recipients).
CREATE TABLE IF NOT EXISTS outreach_consent (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    -- One of lead_id / client_id may be set to tie consent to a CRM record;
    -- contact (normalised E.164 phone or lowercased email) is always set so the
    -- send path can match consent without first resolving a CRM id.
    lead_id         uuid,
    client_id       uuid,
    contact         text        NOT NULL,
    channel         text        NOT NULL,          -- 'voice' | 'sms' | 'email'
    consent_type    text        NOT NULL,
    state_code      char(2),                       -- recipient state at capture
    -- Proof of consent — the audit trail a TCPA/BIPA defence rests on.
    proof_source    text,                          -- e.g. 'web_form', 'ivr_keypress', 'signed_doc'
    proof_text      text,                          -- exact disclosure text shown
    proof_ip        inet,
    captured_at     timestamptz NOT NULL DEFAULT now(),
    -- Retention floor. CA ARL: keep >= 3y, or 1y past contract end (later wins).
    expires_at      timestamptz,
    revoked_at      timestamptz,                   -- consent withdrawn (still retained)
    created_at      timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_consent_channel') THEN
        ALTER TABLE outreach_consent
            ADD CONSTRAINT chk_consent_channel CHECK (channel IN ('voice', 'sms', 'email'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_consent_type') THEN
        ALTER TABLE outreach_consent
            ADD CONSTRAINT chk_consent_type CHECK (consent_type IN
                ('express_written', 'express_oral', 'prior_business', 'biometric_voiceprint'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_oc_tenant_contact
    ON outreach_consent(tenant_id, contact, channel);
CREATE INDEX IF NOT EXISTS idx_oc_active
    ON outreach_consent(tenant_id, contact)
    WHERE revoked_at IS NULL;

ALTER TABLE outreach_consent ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'outreach_consent' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON outreach_consent
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. outreach_suppression  (tenant-private)   — the do-not-contact list
-- ---------------------------------------------------------------------------
-- reason:
--   stop_keyword   — recipient replied STOP/UNSUBSCRIBE/etc (TCPA: honour within
--                    10 business days, eff. Apr 11 2025; FL FTSA: 15-day cure).
--   manual_dnc     — agent/admin manual do-not-contact flag.
--   litigator      — known serial-TCPA-plaintiff scrub.
--   regulatory     — national/state DNC registry hit.
-- One active (lifted_at IS NULL) row per (tenant, contact, channel) blocks sends.
-- channel = '*' suppresses every channel for that contact.
CREATE TABLE IF NOT EXISTS outreach_suppression (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,
    contact       text        NOT NULL,            -- E.164 phone or lowercased email
    channel       text        NOT NULL DEFAULT '*',-- 'voice' | 'sms' | 'email' | '*'
    reason        text        NOT NULL,
    source_text   text,                            -- e.g. the inbound 'STOP' body
    created_at    timestamptz NOT NULL DEFAULT now(),
    lifted_at     timestamptz                      -- set when consumer opts back in
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_suppression_channel') THEN
        ALTER TABLE outreach_suppression
            ADD CONSTRAINT chk_suppression_channel CHECK (channel IN ('voice', 'sms', 'email', '*'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_suppression_reason') THEN
        ALTER TABLE outreach_suppression
            ADD CONSTRAINT chk_suppression_reason CHECK (reason IN
                ('stop_keyword', 'manual_dnc', 'litigator', 'regulatory'));
    END IF;
END $$;

-- Partial unique index: at most one *active* suppression per (tenant, contact,
-- channel). Re-suppressing an already-blocked contact is a no-op upsert target.
CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_active
    ON outreach_suppression(tenant_id, contact, channel)
    WHERE lifted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_supp_tenant_contact
    ON outreach_suppression(tenant_id, contact);

ALTER TABLE outreach_suppression ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'outreach_suppression' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON outreach_suppression
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. outreach_attempt_log  (tenant-private)   — frequency-cap + quiet-hours audit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_attempt_log (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,
    contact       text        NOT NULL,
    channel       text        NOT NULL,            -- 'voice' | 'sms' | 'email'
    state_code    char(2),
    allowed       boolean     NOT NULL,            -- did the gate permit this attempt?
    block_reason  text,                            -- first blocker when allowed = false
    attempted_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_oal_freq
    ON outreach_attempt_log(tenant_id, contact, channel, attempted_at);

ALTER TABLE outreach_attempt_log ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'outreach_attempt_log' AND policyname = 'tenant_isolation'
    ) THEN
        CREATE POLICY tenant_isolation ON outreach_attempt_log
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;
