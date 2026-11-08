-- ---------------------------------------------------------------------------
-- 0168 — a broker owner's decision that a public property record is, or is
-- not, a linked client's house; append-only (prompt 11, T11.03, F11.04).
--
-- Houses (mls_portal) names what its search matched: an exact parcel or
-- address key, or only a possible match (AC11.04). Nothing recorded a person
-- deciding whether that record IS the house a client's lead refers to, so
-- every row read `verified: false`, exact keys included.
-- POST /api/crm/houses/{lead_id}/record-match (property_match_api.py) now
-- records that decision, one row per decision.
--
-- The claim of a row: public record R (public_property_records.id) is the
-- house that this tenant's lead L (leads.id) refers to, L being linked to
-- client C (leads.seller_client_id) when decided. The pairing is the exact
-- parcel key Houses already joins on (L.parcel_id/state = R's): 'parcel_key'
-- is the only value.
--   decision  confirmed  (needs at least one evidence kind)
--             rejected   ("R is not C's house")
--             withdrawn  (back to undecided; needs a decision to supersede)
-- It does not claim that the text the user typed was this house (the search's
-- status and basis are never promoted), the relationship kind (owner, seller
-- or buyer), or that R's facts or owner of record are correct.
--
-- History is the chain itself. Rows are never updated; a reversal is a new row
-- naming the head it supersedes (supersedes_id, within the same pair: a
-- composite FK). One first decision per pair and one successor per decision
-- (the two unique indexes) keep the chain linear, so two people deciding
-- against the same head cannot both land. A decision counts as verified only
-- while it is the newest in its chain, is `confirmed`, and its client_id still
-- equals the lead's seller_client_id (computed on read in
-- mls_portal._lead_ids_for_records, never stored). If a re-harvest re-keys R's
-- parcel, Houses no longer reaches L from R and it reads not verified; R
-- purged from the catalog (no FK: 0050 is a global catalog) matches nothing.
--
-- client_id is read by the route from the lead under FOR UPDATE, never taken
-- from the request. decided_by, decided_role and decided_at are stamped by
-- trigger from the session (app.current_agent, app_current_role(), now()),
-- whatever the INSERT says; an INSERT with no session actor is refused.
-- record_refreshed_at keeps R's refresh stamp when judged, for history only:
-- it moves on every harvest, so it is no drift gate.
--
-- The reason is the decider's own words and often names an owner, so it is
-- stored only sealed (backend/sealed_text.py: pgp_sym_encrypt under the lead
-- tenant's HKDF key, 's2k-mode=1'), never under the caller's context, since a
-- platform admin may decide another tenant's house. There is no plaintext
-- column for it and no fingerprint of R's location or owner (an unkeyed hash
-- of a home location is effectively plaintext; drift detection under the same
-- record id is an owner decision). The brokerage export decrypts it
-- (privacy_export.DECRYPT); the audit ledger gets ids and the decision only.
--
-- Access (RESTRICTIVE policies, ANDed with the tenant wall; 0143's rule for
-- rows hanging off a lead):
--   read    the lead is visible, so a colleague's private client's house and
--           its decisions stay hidden;
--   insert  the lead is visible and its client editable, so a view-only share
--           is refused (42501): a database floor even while the route is
--           broker-owner only.
-- Platform admin, the broker owner and brokerage-scope pipelines short-circuit.
-- The application role is granted SELECT and INSERT only, and an UPDATE is
-- refused for everyone by trigger. Rows leave only with their lead, client or
-- tenant (FK cascade, with every later decision in their chain) or by tenant
-- erasure (0122, which runs as owner).
--
-- Creating the table takes SHARE ROW EXCLUSIVE on leads and clients for the
-- foreign keys: no rewrite or scan, bounded by the runner's lock_timeout.
--
-- Previous release on this schema: unaffected (it never reads the table).
--
-- Rollback: DROP TABLE property_match_decisions; DROP FUNCTION
-- property_match_decisions_stamp(), property_match_decisions_immutable();
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS property_match_decisions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id             uuid NOT NULL,
    -- No FK: the public catalog is global (0050); a purged id stops matching.
    public_record_id    uuid NOT NULL,
    -- leads.seller_client_id read under FOR UPDATE, never from the request.
    client_id           uuid NOT NULL,
    decision            text NOT NULL CHECK (decision IN ('confirmed', 'rejected', 'withdrawn')),
    supersedes_id       uuid,
    pairing             text NOT NULL DEFAULT 'parcel_key' CHECK (pairing = 'parcel_key'),
    evidence            text[] NOT NULL DEFAULT '{}' CHECK (evidence <@ ARRAY[
                            'parcel_number_checked', 'deed_or_title', 'tax_bill',
                            'client_confirmed', 'site_visit', 'other']::text[]),
    -- The record's refresh stamp when judged: history only.
    record_refreshed_at timestamptz,
    reason_ciphertext   bytea NOT NULL,
    -- Stamped by trigger from the session.
    decided_by          text NOT NULL,
    decided_role        text NOT NULL,
    decided_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pmd_lead_fk FOREIGN KEY (tenant_id, lead_id)
        REFERENCES leads (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT pmd_client_fk FOREIGN KEY (tenant_id, client_id)
        REFERENCES clients (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT pmd_chain_key UNIQUE (tenant_id, lead_id, public_record_id, id),
    CONSTRAINT pmd_supersedes_fk FOREIGN KEY (tenant_id, lead_id, public_record_id, supersedes_id)
        REFERENCES property_match_decisions (tenant_id, lead_id, public_record_id, id) ON DELETE CASCADE,
    CONSTRAINT pmd_withdraw_needs_prior CHECK (decision <> 'withdrawn' OR supersedes_id IS NOT NULL),
    CONSTRAINT pmd_confirm_needs_evidence CHECK (decision <> 'confirmed' OR cardinality(evidence) >= 1)
);

-- One first decision per pair, one successor per decision: a linear chain.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pmd_first
    ON property_match_decisions (tenant_id, lead_id, public_record_id) WHERE supersedes_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pmd_successor
    ON property_match_decisions (supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pmd_lead
    ON property_match_decisions (tenant_id, lead_id, public_record_id, decided_at);

ALTER TABLE property_match_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_match_decisions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS property_match_decisions_tenant_isolation ON property_match_decisions;
CREATE POLICY property_match_decisions_tenant_isolation ON property_match_decisions
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

DROP POLICY IF EXISTS property_match_decisions_lead_read ON property_match_decisions;
CREATE POLICY property_match_decisions_lead_read ON property_match_decisions AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = property_match_decisions.lead_id));

DROP POLICY IF EXISTS property_match_decisions_lead_insert ON property_match_decisions;
CREATE POLICY property_match_decisions_lead_insert ON property_match_decisions AS RESTRICTIVE FOR INSERT WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR EXISTS (SELECT 1 FROM public.leads l WHERE l.id = property_match_decisions.lead_id
                  AND (l.seller_client_id IS NULL
                       OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = l.seller_client_id
                                     AND app_client_access(c.assignee_user_id, c.id, true)))));

-- Default privileges (0001) give oracle_app UPDATE/DELETE on every new table;
-- this one is append-only to the application, so take them back. (They never
-- include TRUNCATE.)
GRANT SELECT, INSERT ON property_match_decisions TO oracle_app;
REVOKE UPDATE, DELETE ON property_match_decisions FROM oracle_app;

COMMENT ON TABLE property_match_decisions IS
    '0168 (T11.03): append-only decisions that a public property record is (or is not) the house a '
    'client-linked lead refers to. Verified = newest decision in the chain is confirmed for the lead''s '
    'current client. The reason is stored only sealed.';
COMMENT ON COLUMN property_match_decisions.reason_ciphertext IS
    '0168: pgp_sym_encrypt(the decider''s reason, the lead tenant''s HKDF key; crypto.py). '
    'Free text that may name an owner: never copied into audit metadata.';

-- Immutable: no UPDATE by anyone. (DELETE is not granted to the application;
-- erasure and the cascades run as owner.)
CREATE OR REPLACE FUNCTION property_match_decisions_immutable()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'property_match_decisions is append-only (UPDATE refused)'
        USING ERRCODE = '42501';
END $$;
REVOKE ALL ON FUNCTION property_match_decisions_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS property_match_decisions_no_change ON property_match_decisions;
CREATE TRIGGER property_match_decisions_no_change
    BEFORE UPDATE ON property_match_decisions
    FOR EACH ROW EXECUTE FUNCTION property_match_decisions_immutable();

-- Who decided, in what role and when: from the session, never the INSERT.
CREATE OR REPLACE FUNCTION property_match_decisions_stamp()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
DECLARE
    actor text := NULLIF(current_setting('app.current_agent', true), '');
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'property_match_decisions: no session actor (app.current_agent is not set)'
            USING ERRCODE = '42501';
    END IF;
    NEW.decided_by := actor;
    NEW.decided_role := app_current_role();
    NEW.decided_at := now();
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION property_match_decisions_stamp() FROM PUBLIC;

DROP TRIGGER IF EXISTS property_match_decisions_stamp ON property_match_decisions;
CREATE TRIGGER property_match_decisions_stamp
    BEFORE INSERT ON property_match_decisions
    FOR EACH ROW EXECUTE FUNCTION property_match_decisions_stamp();
