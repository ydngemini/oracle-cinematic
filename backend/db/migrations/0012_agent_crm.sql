-- ===========================================================================
-- 0012_agent_crm.sql — Agent CRM foundation (5-tab mobile redesign)
--
-- The CRM thesis (design brief 2026-06-11): a house profile is the JOIN POINT
-- between a property and a person, so the AI emailer always knows "this house
-- belongs to this person, and here is how to reach them". Buyers get the same
-- treatment via showings (buyer ↔ house exposure edges).
--
--   1. clients gains a type discriminator (seller/buyer/both) + preferences —
--      one person table, two-sided book. No separate buyers table.
--   2. leads gains seller_client_id — the house → owner relational link that
--      previously existed only inside the encrypted_payload blob.
--   3. leads gains first-class house fields (address, asking_price, beds,
--      baths, sqft) so the mobile listing card needs no jsonb spelunking.
--   4. listings ties back to its source dossier (lead_id) and its seller.
--   5. showings — append-friendly buyer↔property exposure log w/ outcome.
--   6. property_media — photos/docs/splats per house, sortable.
--   7. interaction_logs widens into the unified comms spine: email/message
--      types, client anchoring (buyer comms without a lead), thread identity,
--      direction, subject, external message-id for provider threading.
--   8. email_outbox — durable outbound queue for the AI emailer with a
--      sent/bounced audit posture (no UPDATE-free rows deleted, ever).
--   9. user_profiles gains the public-facing agent identity for My Profile.
--
-- Idempotent: IF NOT EXISTS / DO $$ guards throughout; safe to re-apply.
-- RLS: every new table copies the 0001 tenant_isolation posture verbatim.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. clients — two-sided client book.
-- ---------------------------------------------------------------------------
ALTER TABLE clients ADD COLUMN IF NOT EXISTS client_type text NOT NULL DEFAULT 'seller';
ALTER TABLE clients ADD COLUMN IF NOT EXISTS preferences jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS source      text;          -- referral / sign_call / portal / import
ALTER TABLE clients ADD COLUMN IF NOT EXISTS notes       text;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS archived_at timestamptz;   -- soft archive, never hard-delete a person

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_client_type') THEN
        ALTER TABLE clients ADD CONSTRAINT chk_client_type
            CHECK (client_type IN ('seller', 'buyer', 'both'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_clients_type
    ON clients(tenant_id, client_type) WHERE archived_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2 + 3. leads — house ↔ seller link and first-class house fields.
-- ---------------------------------------------------------------------------
ALTER TABLE leads ADD COLUMN IF NOT EXISTS seller_client_id uuid REFERENCES clients(id) ON DELETE SET NULL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS address      text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS asking_price numeric(12,2);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS beds         smallint;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS baths        numeric(3,1);
ALTER TABLE leads ADD COLUMN IF NOT EXISTS sqft         integer;

CREATE INDEX IF NOT EXISTS idx_leads_seller_client ON leads(seller_client_id);

-- ---------------------------------------------------------------------------
-- 4. listings — provenance (source dossier) and seller identity.
-- ---------------------------------------------------------------------------
ALTER TABLE listings ADD COLUMN IF NOT EXISTS lead_id          uuid UNIQUE REFERENCES leads(id)   ON DELETE SET NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS seller_client_id uuid        REFERENCES clients(id) ON DELETE SET NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS status           text NOT NULL DEFAULT 'active';

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_listing_status') THEN
        ALTER TABLE listings ADD CONSTRAINT chk_listing_status
            CHECK (status IN ('draft', 'active', 'pending', 'sold', 'withdrawn'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_listings_seller_client ON listings(seller_client_id);
CREATE INDEX IF NOT EXISTS idx_listings_status        ON listings(tenant_id, status);

-- ---------------------------------------------------------------------------
-- 5. showings — buyer ↔ property exposure. The AI emailer's buyer context:
--    "you saw 14 Elm St on Tuesday" comes from here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS showings (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id)  ON DELETE CASCADE,
    client_id   uuid NOT NULL REFERENCES clients(id)  ON DELETE CASCADE,   -- the buyer
    lead_id     uuid REFERENCES leads(id)             ON DELETE CASCADE,
    listing_id  uuid REFERENCES listings(id)          ON DELETE CASCADE,
    shown_at    timestamptz NOT NULL DEFAULT now(),
    feedback    text,
    outcome     text NOT NULL DEFAULT 'pending',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_showing_outcome CHECK (outcome IN
        ('pending', 'interested', 'offer_made', 'passed', 'no_show')),
    CONSTRAINT chk_showing_property CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL)
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_showings_updated') THEN
        CREATE TRIGGER trg_showings_updated
            BEFORE UPDATE ON showings
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_showings_tenant  ON showings(tenant_id);
CREATE INDEX IF NOT EXISTS idx_showings_client  ON showings(client_id, shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_showings_lead    ON showings(lead_id);
CREATE INDEX IF NOT EXISTS idx_showings_listing ON showings(listing_id);

-- ---------------------------------------------------------------------------
-- 6. property_media — photos, documents, splats, floorplans per house.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_media (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id     uuid REFERENCES leads(id)            ON DELETE CASCADE,
    listing_id  uuid REFERENCES listings(id)         ON DELETE CASCADE,
    kind        text NOT NULL DEFAULT 'photo',
    url         text NOT NULL,                       -- public or signed URL
    s3_key      text,                                -- canonical object key when ours
    caption     text,
    sort_order  int  NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_media_kind CHECK (kind IN
        ('photo', 'document', 'splat', 'floorplan', 'tour')),
    CONSTRAINT chk_media_property CHECK (lead_id IS NOT NULL OR listing_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_property_media_tenant  ON property_media(tenant_id);
CREATE INDEX IF NOT EXISTS idx_property_media_lead    ON property_media(lead_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_property_media_listing ON property_media(listing_id, sort_order);

-- ---------------------------------------------------------------------------
-- 7. interaction_logs — widen into the unified comms spine.
--    (a) buyer comms need no lead: lead_id nullable + client_id anchor.
--    (b) email/message join the type set.
--    (c) thread identity, direction, subject, provider message-id.
-- ---------------------------------------------------------------------------
ALTER TABLE interaction_logs ALTER COLUMN lead_id DROP NOT NULL;
ALTER TABLE interaction_logs ADD COLUMN IF NOT EXISTS client_id           uuid REFERENCES clients(id) ON DELETE CASCADE;
ALTER TABLE interaction_logs ADD COLUMN IF NOT EXISTS thread_id           uuid;
ALTER TABLE interaction_logs ADD COLUMN IF NOT EXISTS direction           text;
ALTER TABLE interaction_logs ADD COLUMN IF NOT EXISTS subject             text;
ALTER TABLE interaction_logs ADD COLUMN IF NOT EXISTS external_message_id text;

DO $$ BEGIN
    -- Extend the inline 0008 CHECK: drop the auto-named original, add named.
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interaction_logs_interaction_type_check') THEN
        ALTER TABLE interaction_logs DROP CONSTRAINT interaction_logs_interaction_type_check;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_interaction_type') THEN
        ALTER TABLE interaction_logs ADD CONSTRAINT chk_interaction_type
            CHECK (interaction_type IN
                ('sms', 'call_transcript', 'voice_note', 'portal_view',
                 'document_signed', 'status_change', 'email', 'message'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_interaction_direction') THEN
        ALTER TABLE interaction_logs ADD CONSTRAINT chk_interaction_direction
            CHECK (direction IS NULL OR direction IN ('inbound', 'outbound'));
    END IF;
    -- Every row must anchor to a house or a person (existing rows: all lead-anchored).
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_interaction_anchor') THEN
        ALTER TABLE interaction_logs ADD CONSTRAINT chk_interaction_anchor
            CHECK (lead_id IS NOT NULL OR client_id IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_interaction_logs_client
    ON interaction_logs(client_id, created_at DESC) WHERE client_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_interaction_logs_thread
    ON interaction_logs(thread_id, created_at) WHERE thread_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 8. email_outbox — durable outbound queue for the AI emailer.
--    Rows are never deleted; status walks queued → sending → sent|bounced|
--    failed|cancelled. The send worker mirrors terminal states into
--    interaction_logs (type 'email', actor_role 'ai_system' or 'agent').
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_outbox (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id)  ON DELETE CASCADE,
    client_id           uuid NOT NULL REFERENCES clients(id)  ON DELETE CASCADE,
    lead_id             uuid REFERENCES leads(id)             ON DELETE SET NULL,
    thread_id           uuid,
    to_email            text NOT NULL,
    subject             text NOT NULL,
    body_text           text NOT NULL,
    body_html           text,
    template_key        text,                       -- e.g. 'seller_intro', 'buyer_followup'
    status              text NOT NULL DEFAULT 'queued',
    provider_message_id text,
    error               text,
    scheduled_at        timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    created_by          text NOT NULL DEFAULT 'ai_system',   -- agent_id or 'ai_system'
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_email_status CHECK (status IN
        ('queued', 'sending', 'sent', 'bounced', 'failed', 'cancelled'))
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_email_outbox_updated') THEN
        CREATE TRIGGER trg_email_outbox_updated
            BEFORE UPDATE ON email_outbox
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_email_outbox_tenant ON email_outbox(tenant_id);
CREATE INDEX IF NOT EXISTS idx_email_outbox_client ON email_outbox(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_outbox_queue
    ON email_outbox(status, scheduled_at) WHERE status = 'queued';

-- No DELETE for the app role: the outbox is an auditable send ledger.
REVOKE DELETE ON email_outbox FROM oracle_app;

-- ---------------------------------------------------------------------------
-- 9. user_profiles — public-facing agent identity for the My Profile tab.
--    (TEXT tenant_id type debt acknowledged and deliberately untouched —
--    Memory Core reads/writes this table; converting is its own migration.)
-- ---------------------------------------------------------------------------
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS display_name    text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS public_email    text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS phone           text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS headshot_url    text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS license_number  text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS brokerage       text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS bio             text;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS email_signature text;

-- ---------------------------------------------------------------------------
-- RLS — ENABLE + FORCE, tenant isolation per 0001 house style.
-- ---------------------------------------------------------------------------
ALTER TABLE showings       ENABLE ROW LEVEL SECURITY;  ALTER TABLE showings       FORCE ROW LEVEL SECURITY;
ALTER TABLE property_media ENABLE ROW LEVEL SECURITY;  ALTER TABLE property_media FORCE ROW LEVEL SECURITY;
ALTER TABLE email_outbox   ENABLE ROW LEVEL SECURITY;  ALTER TABLE email_outbox   FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'showings_tenant_isolation') THEN
        CREATE POLICY showings_tenant_isolation ON showings
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'property_media_tenant_isolation') THEN
        CREATE POLICY property_media_tenant_isolation ON property_media
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'email_outbox_tenant_isolation') THEN
        CREATE POLICY email_outbox_tenant_isolation ON email_outbox
            USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
            WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());
    END IF;
END $$;
