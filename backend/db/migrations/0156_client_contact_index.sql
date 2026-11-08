-- ---------------------------------------------------------------------------
-- 0156 — keyed lookup hashes and name-search tokens for clients, beside the
-- plaintext (audit A01 / ISO R01, prompt 17, ledger L17-23; L17-21 design §2,
-- slice (a): expand + fill, no reader change).
--
-- clients.full_name / email / phone are plaintext, searched by substring and
-- matched exactly (privacy requests, dedupe, inbound numbers). Before they can
-- be sealed, every search must move onto values that are not the plaintext.
-- agent_contacts already has that scheme (contact_truth): HMAC-SHA-256 under
-- the tenant key over the normalised email / phone (exact match) and keyed
-- word-prefix tokens over the name. This adds the same three values for every
-- client, and keeps them current. Nothing reads them yet: slice (b) moves the
-- searches, after the owner approves word-prefix name search and exact-only
-- email search (L17-21 next_task).
--
-- Why a side table and not columns on clients:
--   * trg_clients_updated stamps clients.updated_at on EVERY update. Filling
--     a column would bump it for every client, which reorders the CRM lists,
--     and client_ai_automation treats a moved updated_at as a human edit and
--     abandons its pass (stale) — on every client, once per fill.
--   * the hashes are computed in the app (the key is derived from the master
--     key, which the database never sees), so the fill is a second write per
--     client; on a side table it never rewrites the client's row or indexes.
--
-- Freshness: the index may be MISSING, never WRONG.
--   * clients_contact_index_new (AFTER INSERT, once per statement) creates
--     each new client's row stale; clients_contact_index_stale (AFTER UPDATE
--     OF full_name, email, phone, when a value changes) marks it stale. SECURITY DEFINER so an agent's edit can mark it without
--     oracle_app being able to write index values.
--   * The filler (backend/client_contact_index.py; periodic task
--     client_contact_index, internal-only, and an operator script) reads a
--     batch of stale or missing clients FOR SHARE, computes in the app and
--     writes stale = false in the same transaction. FOR SHARE makes a
--     concurrent edit wait until the fill commits, and the edit's trigger then
--     marks the row stale again, so a fill can never record old values as
--     current.
--
-- Access: read = the client is visible (an EXISTS on clients under the
-- caller's RLS, so 0131 decides). Write = platform admin, broker owner or a
-- brokerage-scope pipeline only (the filler runs as platform admin of the
-- row's own tenant); an agent never writes it directly.
--
-- Erasure: rows go with their client (FK ON DELETE CASCADE) and with their
-- brokerage (tenant_id; privacy_data_map: erase).
--
-- Previous release on this schema: unaffected (it never reads the table; its
-- client writes fire the trigger, which only marks rows stale).
--
-- Rollback: DROP TRIGGER clients_contact_index_new ON clients;
-- DROP TRIGGER clients_contact_index_stale ON clients; DROP FUNCTION
-- clients_contact_index_mark_new(), clients_contact_index_mark_stale();
-- DROP TABLE client_contact_index.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS client_contact_index (
    client_id           uuid PRIMARY KEY,
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email_lookup_hash   character(64),
    phone_lookup_hash   character(64),
    name_search_tokens  text[] NOT NULL DEFAULT ARRAY[]::text[],
    -- A value was present but could not be normalised (an email that is not
    -- an address, a phone without a country code). Counted, never guessed.
    email_unusable      boolean NOT NULL DEFAULT false,
    phone_unusable      boolean NOT NULL DEFAULT false,
    stale               boolean NOT NULL DEFAULT true,
    indexed_at          timestamptz,
    CONSTRAINT client_contact_index_client_fk
        FOREIGN KEY (tenant_id, client_id) REFERENCES clients(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT client_contact_index_email_hash_chk
        CHECK (email_lookup_hash IS NULL OR email_lookup_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT client_contact_index_phone_hash_chk
        CHECK (phone_lookup_hash IS NULL OR phone_lookup_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT client_contact_index_name_tokens_chk
        CHECK (array_position(name_search_tokens, NULL::text) IS NULL
               AND array_to_string(name_search_tokens, '') ~ '^([0-9a-f]{64})*$'),
    -- A fresh row says when it was computed.
    CONSTRAINT client_contact_index_fresh_chk CHECK (stale OR indexed_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_client_contact_index_email
    ON client_contact_index (tenant_id, email_lookup_hash) WHERE email_lookup_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_client_contact_index_phone
    ON client_contact_index (tenant_id, phone_lookup_hash) WHERE phone_lookup_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_client_contact_index_name
    ON client_contact_index USING gin (name_search_tokens);
CREATE INDEX IF NOT EXISTS idx_client_contact_index_stale
    ON client_contact_index (tenant_id, client_id) WHERE stale;

ALTER TABLE client_contact_index ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_contact_index FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS client_contact_index_tenant_isolation ON client_contact_index;
CREATE POLICY client_contact_index_tenant_isolation ON client_contact_index
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

DROP POLICY IF EXISTS client_contact_index_client_read ON client_contact_index;
DROP POLICY IF EXISTS client_contact_index_write_insert ON client_contact_index;
DROP POLICY IF EXISTS client_contact_index_write_update ON client_contact_index;
DROP POLICY IF EXISTS client_contact_index_write_delete ON client_contact_index;
CREATE POLICY client_contact_index_client_read ON client_contact_index AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR EXISTS (SELECT 1 FROM public.clients c WHERE c.id = client_contact_index.client_id));
CREATE POLICY client_contact_index_write_insert ON client_contact_index AS RESTRICTIVE FOR INSERT WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage');
CREATE POLICY client_contact_index_write_update ON client_contact_index AS RESTRICTIVE FOR UPDATE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage')
  WITH CHECK (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage');
CREATE POLICY client_contact_index_write_delete ON client_contact_index AS RESTRICTIVE FOR DELETE USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage');

GRANT SELECT, INSERT, UPDATE, DELETE ON client_contact_index TO oracle_app;

-- The staleness marks. Run as the migration owner (BYPASSRLS), touch only
-- the inserted / edited clients' own index rows, and write no value but
-- `stale`. INSERT is one statement-level insert from the transition table, so
-- a bulk import pays once per statement (measured in L17-23); an UPDATE of
-- name / email / phone marks per row, and only when a value changed.
CREATE OR REPLACE FUNCTION clients_contact_index_mark_new()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    INSERT INTO public.client_contact_index (client_id, tenant_id, stale)
    SELECT n.id, n.tenant_id, true FROM new_clients n
    ON CONFLICT (client_id) DO UPDATE SET stale = true;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION clients_contact_index_mark_new() FROM PUBLIC;

CREATE OR REPLACE FUNCTION clients_contact_index_mark_stale()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.full_name IS NOT DISTINCT FROM OLD.full_name
       AND NEW.email IS NOT DISTINCT FROM OLD.email
       AND NEW.phone IS NOT DISTINCT FROM OLD.phone THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.client_contact_index (client_id, tenant_id, stale)
    VALUES (NEW.id, NEW.tenant_id, true)
    ON CONFLICT (client_id) DO UPDATE SET stale = true;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION clients_contact_index_mark_stale() FROM PUBLIC;

DROP TRIGGER IF EXISTS clients_contact_index_new ON clients;
CREATE TRIGGER clients_contact_index_new
    AFTER INSERT ON clients
    REFERENCING NEW TABLE AS new_clients
    FOR EACH STATEMENT EXECUTE FUNCTION clients_contact_index_mark_new();
DROP TRIGGER IF EXISTS clients_contact_index_stale ON clients;
CREATE TRIGGER clients_contact_index_stale
    AFTER UPDATE OF full_name, email, phone ON clients
    FOR EACH ROW EXECUTE FUNCTION clients_contact_index_mark_stale();

COMMENT ON TABLE client_contact_index IS
    '0156 (L17-23): keyed exact-match hashes and word-prefix name tokens for clients (contact_truth scheme). May be missing (stale), never wrong; filled by client_contact_index.py. Not read by any search yet (slice b).';
