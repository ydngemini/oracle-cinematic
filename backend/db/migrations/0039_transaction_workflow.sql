-- Canonical tenant-safe deal workflow: explicit property provenance,
-- optimistic versions, and offer lifecycle state. Additive for legacy rows.

BEGIN;

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS client_id uuid,
    ADD COLUMN IF NOT EXISTS client_party_role text,
    ADD COLUMN IF NOT EXISTS property_source text,
    ADD COLUMN IF NOT EXISTS property_id uuid,
    ADD COLUMN IF NOT EXISTS mls_listing_id uuid,
    ADD COLUMN IF NOT EXISTS property_address text,
    ADD COLUMN IF NOT EXISTS property_city text,
    ADD COLUMN IF NOT EXISTS property_postal_code text,
    ADD COLUMN IF NOT EXISTS source_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS purchase_price numeric(14,2),
    ADD COLUMN IF NOT EXISTS earnest_money numeric(14,2),
    ADD COLUMN IF NOT EXISTS financing_amount numeric(14,2),
    ADD COLUMN IF NOT EXISTS offer_deadline date,
    ADD COLUMN IF NOT EXISTS inspection_deadline date,
    ADD COLUMN IF NOT EXISTS financing_deadline date,
    ADD COLUMN IF NOT EXISTS closing_deadline date,
    ADD COLUMN IF NOT EXISTS notes text,
    ADD COLUMN IF NOT EXISTS accepted_offer_id uuid,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz,
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS created_by text,
    ADD COLUMN IF NOT EXISTS updated_by text;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_client_party_pair_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_client_party_pair_chk
            CHECK ((client_id IS NULL) = (client_party_role IS NULL)) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_client_party_role_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_client_party_role_chk
            CHECK (client_party_role IS NULL OR client_party_role IN (
                'seller','buyer','assignor','assignee','agent','broker',
                'attorney','title','lender','joint_venture'
            )) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_property_source_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_property_source_chk
            CHECK (
                (property_source IS NULL AND property_id IS NULL AND mls_listing_id IS NULL)
                OR (property_source='pipeline' AND property_id=lead_id
                    AND lead_id IS NOT NULL AND mls_listing_id IS NULL)
                OR (property_source='mls' AND property_id=mls_listing_id
                    AND mls_listing_id IS NOT NULL AND lead_id IS NULL)
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_source_provenance_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_source_provenance_chk
            CHECK (jsonb_typeof(source_provenance)='object') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_financials_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_financials_chk
            CHECK (
                (purchase_price IS NULL OR purchase_price >= 0)
                AND (earnest_money IS NULL OR earnest_money >= 0)
                AND (financing_amount IS NULL OR financing_amount >= 0)
                AND (purchase_price IS NULL OR earnest_money IS NULL
                     OR earnest_money <= purchase_price)
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_deadline_order_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_deadline_order_chk
            CHECK (
                closing_deadline IS NULL
                OR ((offer_deadline IS NULL OR offer_deadline <= closing_deadline)
                    AND (inspection_deadline IS NULL OR inspection_deadline <= closing_deadline)
                    AND (financing_deadline IS NULL OR financing_deadline <= closing_deadline))
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_status_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_status_chk
            CHECK (status IN ('active','under_contract','closed','cancelled')) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_version_chk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_version_chk CHECK (version > 0) NOT VALID;
    END IF;
END $$;

-- Composite keys let child FKs prove tenant agreement, not merely row identity.
CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_tenant_id
    ON clients(tenant_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_tenant_id
    ON leads(tenant_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_listings_tenant_id
    ON listings(tenant_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_tenant_id
    ON transactions(tenant_id,id);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transactions_tenant_fk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_tenant_fk
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transactions_client_tenant_fk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_client_tenant_fk
            FOREIGN KEY (tenant_id,client_id) REFERENCES clients(tenant_id,id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transactions_lead_tenant_fk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_lead_tenant_fk
            FOREIGN KEY (tenant_id,lead_id) REFERENCES leads(tenant_id,id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transactions_mls_listing_fk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_mls_listing_fk
            FOREIGN KEY (mls_listing_id) REFERENCES oracle_mls_listings(id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transaction_parties_transaction_tenant_fk'
    ) THEN
        ALTER TABLE transaction_parties
            ADD CONSTRAINT transaction_parties_transaction_tenant_fk
            FOREIGN KEY (tenant_id,transaction_id)
            REFERENCES transactions(tenant_id,id) ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transaction_parties_client_tenant_fk'
    ) THEN
        ALTER TABLE transaction_parties
            ADD CONSTRAINT transaction_parties_client_tenant_fk
            FOREIGN KEY (tenant_id,client_id) REFERENCES clients(tenant_id,id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='transaction_milestones_transaction_tenant_fk'
    ) THEN
        ALTER TABLE transaction_milestones
            ADD CONSTRAINT transaction_milestones_transaction_tenant_fk
            FOREIGN KEY (tenant_id,transaction_id)
            REFERENCES transactions(tenant_id,id) ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='compliance_items_transaction_tenant_fk'
    ) THEN
        ALTER TABLE compliance_checklist_items
            ADD CONSTRAINT compliance_items_transaction_tenant_fk
            FOREIGN KEY (tenant_id,transaction_id)
            REFERENCES transactions(tenant_id,id) ON DELETE CASCADE NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS transaction_offers (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    transaction_id         uuid NOT NULL,
    status                 text NOT NULL DEFAULT 'submitted',
    amount                 numeric(14,2) NOT NULL,
    earnest_money          numeric(14,2) NOT NULL DEFAULT 0,
    financing_type         text NOT NULL DEFAULT 'cash',
    proposed_closing_date  date,
    expires_at             timestamptz,
    contingencies          jsonb NOT NULL DEFAULT '{}'::jsonb,
    notes                  text,
    version                integer NOT NULL DEFAULT 1,
    submitted_at           timestamptz NOT NULL DEFAULT now(),
    accepted_at            timestamptz,
    rejected_at            timestamptz,
    withdrawn_at           timestamptz,
    created_by             text NOT NULL,
    updated_by             text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT transaction_offers_transaction_tenant_fk
        FOREIGN KEY (tenant_id,transaction_id)
        REFERENCES transactions(tenant_id,id) ON DELETE CASCADE,
    CONSTRAINT transaction_offers_status_chk
        CHECK (status IN ('submitted','accepted','rejected','withdrawn','expired')),
    CONSTRAINT transaction_offers_amounts_chk
        CHECK (amount > 0 AND earnest_money >= 0 AND earnest_money <= amount),
    CONSTRAINT transaction_offers_financing_chk
        CHECK (financing_type IN ('cash','conventional','fha','va','usda','other')),
    CONSTRAINT transaction_offers_contingencies_chk
        CHECK (jsonb_typeof(contingencies)='object'),
    CONSTRAINT transaction_offers_version_chk CHECK (version > 0),
    CONSTRAINT transaction_offers_accepted_at_chk
        CHECK (status <> 'accepted' OR accepted_at IS NOT NULL),
    CONSTRAINT transaction_offers_tenant_transaction_id_key
        UNIQUE (tenant_id,transaction_id,id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_transaction_offers_tenant_id
    ON transaction_offers(tenant_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_transaction_offers_one_accepted
    ON transaction_offers(tenant_id,transaction_id)
    WHERE status='accepted';
CREATE INDEX IF NOT EXISTS idx_transaction_offers_transaction
    ON transaction_offers(tenant_id,transaction_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_workflow
    ON transactions(tenant_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_property_source
    ON transactions(tenant_id,property_source,property_id);
CREATE INDEX IF NOT EXISTS idx_transactions_client
    ON transactions(tenant_id,client_id,updated_at DESC)
    WHERE client_id IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='transactions_accepted_offer_fk'
    ) THEN
        ALTER TABLE transactions
            ADD CONSTRAINT transactions_accepted_offer_fk
            FOREIGN KEY (tenant_id,id,accepted_offer_id)
            REFERENCES transaction_offers(tenant_id,transaction_id,id)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname='trg_transaction_offers_updated'
    ) THEN
        CREATE TRIGGER trg_transaction_offers_updated
            BEFORE UPDATE ON transaction_offers
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- RLS is defense-in-depth; API queries also carry explicit tenant predicates.
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON transactions;
DROP POLICY IF EXISTS transactions_tenant_isolation ON transactions;
CREATE POLICY transactions_tenant_isolation ON transactions
    USING (app_is_platform_admin() OR tenant_id=app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id=app_current_tenant());

ALTER TABLE transaction_offers ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction_offers FORCE ROW LEVEL SECURITY;
CREATE POLICY transaction_offers_tenant_isolation ON transaction_offers
    USING (app_is_platform_admin() OR tenant_id=app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id=app_current_tenant());

ALTER TABLE transaction_parties FORCE ROW LEVEL SECURITY;
ALTER TABLE transaction_milestones FORCE ROW LEVEL SECURITY;
ALTER TABLE compliance_checklist_items FORCE ROW LEVEL SECURITY;

GRANT SELECT,INSERT,UPDATE,DELETE ON transaction_offers TO oracle_app;

COMMENT ON COLUMN transactions.source_provenance IS
    'Immutable-at-API snapshot of the explicitly selected pipeline or normalized MLS source.';
COMMENT ON COLUMN transactions.version IS
    'Optimistic concurrency token; every canonical transaction mutation increments it.';
COMMENT ON COLUMN transaction_offers.version IS
    'Optimistic concurrency token; acceptance and future offer mutations increment it.';

COMMIT;
