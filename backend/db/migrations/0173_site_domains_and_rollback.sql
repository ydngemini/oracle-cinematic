-- ---------------------------------------------------------------------------
-- 0173 — a site's custom domain moves only on DNS evidence, revisions are
-- immutable, and every site and domain transition is an immutable event that
-- names who made it (prompt 14, T14.04, AC14.04, F14.07; T14.03 as a
-- constraint; ledger L14-02).
--
-- Before: hyperlocal_site_domains (0056) had verified / active / failed /
-- retired in its CHECK, but nothing ever set them; publish inserted 'pending'
-- with no challenge, and a republish reset any state back to 'pending'.
-- Revisions were immutable only by convention (oracle_app held UPDATE).
-- Nothing recorded when a site's live revision changed or who changed it, so
-- "the previous published revision" could not be named.
--
-- Now:
--   hyperlocal_site_domains
--     challenge_token      64 hex chars (256 bits) from gen_random_bytes, made
--                          by PostgreSQL per claim (the column DEFAULT, so an
--                          insert from the previous release gets one too, and
--                          every existing row gets its own). The owner of the
--                          name publishes TXT "_neoh-site-verify.<hostname>" =
--                          "neoh-site-verify=<token>" (backend/site_domains.py).
--                          Not a secret: it is public in DNS by design; it is
--                          unpredictable before the claim exists.
--     challenge_issued_at, last_checked_at, last_check (what the last DNS
--                          check saw: record name, answer count, verdicts,
--                          the matched value's hash; never another TXT value).
--   The claim's hostname, site, brokerage and token never change (a claim is
--   single-purpose), and a claim is born 'pending'. Legal moves:
--         pending  -> verified | failed | retired
--         failed   -> verified | retired
--         verified -> active | failed | retired
--         active   -> verified | failed | retired
--         retired  is terminal
--   There is no path from pending or failed to active. 'active' also needs
--   the site to be published at that moment. A CHECK requires verified_at and
--   verification_hash for verified and active. Anything else raises SQLSTATE
--   23514 with the pair named. The global UNIQUE (hostname) of 0056 already
--   means one brokerage's claim cannot be claimed or verified by another.
--
--   hyperlocal_site_events  append-only, written only by SECURITY DEFINER
--     triggers (the 0163 pattern), one row per:
--       site:   insert, and every change of status or published_revision_id
--               (publish, rollback, archive), with from/to revision;
--       domain: insert, and every change of status, with NEW.last_check as
--               the evidence.
--     actor = app.current_agent (the session's agent, set per transaction by
--     tenancy.apply_rls_context), else session_user; actor_role =
--     app.current_role. A site event's evidence is app.site_event_evidence
--     when the route set it (the approval and its reviewer for a publish, the
--     original approvals for a rollback). source='backfill': one row per site
--     published at migration time, stating its live revision then — not
--     history, and labelled so. The application holds SELECT only; an UPDATE
--     is refused for everyone by trigger. Rows leave only with their site or
--     brokerage (FK cascade, tenant erasure as owner).
--
--   hyperlocal_site_revisions  immutable: UPDATE revoked from oracle_app and
--     refused for everyone by trigger (nothing in the application updates a
--     revision; a rollback re-points hyperlocal_sites.published_revision_id).
--
--   gen_random_bytes(integer) is granted to oracle_app (0003 revoked every
--   function in public; 0091 granted pgp_sym_* the same way), because the
--   column DEFAULT runs with the inserting role's privileges.
--
-- Data: any verified/active claim without verified_at or verification_hash
-- (none expected — no code ever set those states) is demoted to 'pending'
-- before the CHECK is added; a NOTICE gives the count.
--
-- What 'active' means here: ownership is proven AND the hostname's CNAME is
-- the serving target the operator declared (ORACLE_SITES_DOMAIN_TARGET). It is
-- not proof that the edge terminates TLS for the name or routes it to the
-- site: no such serving exists in this repository yet (operator work).
--
-- Previous release on this schema: it never reads the new columns or the
-- trail; its site writes are recorded, not blocked. One exception: its publish
-- upsert resets an existing claim to 'pending', which is now an illegal move
-- once new code has verified or failed that claim, so a republish of that
-- hostname by the old release fails (23514) during the rollout window only.
--
-- Rollback: DROP TRIGGER hyperlocal_sites_event_write ON hyperlocal_sites;
-- DROP TRIGGER hyperlocal_site_domains_event_write ON hyperlocal_site_domains;
-- DROP TRIGGER hyperlocal_site_domains_transition ON hyperlocal_site_domains;
-- DROP TRIGGER hyperlocal_site_revisions_immutable ON hyperlocal_site_revisions;
-- DROP TABLE hyperlocal_site_events;
-- DROP FUNCTION hyperlocal_sites_write_event(), hyperlocal_site_domains_write_event(),
-- hyperlocal_site_domains_check_transition(), hyperlocal_site_events_immutable(),
-- hyperlocal_site_revisions_refuse_update();
-- ALTER TABLE hyperlocal_site_domains DROP CONSTRAINT hyperlocal_site_domains_evidence_chk,
-- DROP COLUMN challenge_token, DROP COLUMN challenge_issued_at,
-- DROP COLUMN last_checked_at, DROP COLUMN last_check;
-- GRANT UPDATE ON hyperlocal_site_revisions TO oracle_app;
-- REVOKE EXECUTE ON FUNCTION gen_random_bytes(integer) FROM oracle_app;
-- ---------------------------------------------------------------------------

GRANT EXECUTE ON FUNCTION gen_random_bytes(integer) TO oracle_app;

-- ── the claim's challenge and evidence ──────────────────────────────────────

ALTER TABLE hyperlocal_site_domains
    ADD COLUMN IF NOT EXISTS challenge_token text NOT NULL
        DEFAULT encode(gen_random_bytes(32), 'hex'),
    ADD COLUMN IF NOT EXISTS challenge_issued_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_checked_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_check jsonb;

DO $$
DECLARE n int;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'hyperlocal_site_domains_token_chk') THEN
        ALTER TABLE hyperlocal_site_domains ADD CONSTRAINT hyperlocal_site_domains_token_chk
            CHECK (challenge_token ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'hyperlocal_site_domains_last_check_chk') THEN
        ALTER TABLE hyperlocal_site_domains ADD CONSTRAINT hyperlocal_site_domains_last_check_chk
            CHECK (last_check IS NULL OR jsonb_typeof(last_check) = 'object');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'hyperlocal_site_domains_evidence_chk') THEN
        UPDATE hyperlocal_site_domains SET status = 'pending'
         WHERE status IN ('verified', 'active') AND (verified_at IS NULL OR verification_hash IS NULL);
        GET DIAGNOSTICS n = ROW_COUNT;
        RAISE NOTICE '0173: % verified/active domain claim(s) without evidence demoted to pending', n;
        ALTER TABLE hyperlocal_site_domains ADD CONSTRAINT hyperlocal_site_domains_evidence_chk
            CHECK (status NOT IN ('verified', 'active')
                   OR (verified_at IS NOT NULL AND verification_hash IS NOT NULL));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_hyperlocal_site_domains_token
    ON hyperlocal_site_domains (challenge_token);

COMMENT ON COLUMN hyperlocal_site_domains.challenge_token IS
    '0173 (T14.04): 256-bit token, made by PostgreSQL, for TXT _neoh-site-verify.<hostname> = '
    'neoh-site-verify=<token>. Fixed for the life of the claim; public in DNS by design.';
COMMENT ON COLUMN hyperlocal_site_domains.last_check IS
    '0173: what the last DNS check saw (record, answer count, verdicts, matched value hash); never other TXT values.';

CREATE OR REPLACE FUNCTION hyperlocal_site_domains_check_transition()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.verified_at IS NOT NULL OR NEW.verification_hash IS NOT NULL THEN
            RAISE EXCEPTION 'a domain claim starts pending, without verification evidence'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.hostname IS DISTINCT FROM OLD.hostname
       OR NEW.challenge_token IS DISTINCT FROM OLD.challenge_token THEN
        RAISE EXCEPTION 'a domain claim''s brokerage, site, hostname and challenge are fixed'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) NOT IN (
        ('pending', 'verified'), ('pending', 'failed'), ('pending', 'retired'),
        ('failed', 'verified'), ('failed', 'retired'),
        ('verified', 'active'), ('verified', 'failed'), ('verified', 'retired'),
        ('active', 'verified'), ('active', 'failed'), ('active', 'retired')
    ) THEN
        RAISE EXCEPTION 'illegal domain transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'active' AND NOT EXISTS (
        SELECT 1 FROM hyperlocal_sites s
         WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.site_id AND s.status = 'published'
    ) THEN
        RAISE EXCEPTION 'a domain can be active only for a published site'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION hyperlocal_site_domains_check_transition() FROM PUBLIC;

DROP TRIGGER IF EXISTS hyperlocal_site_domains_transition ON hyperlocal_site_domains;
CREATE TRIGGER hyperlocal_site_domains_transition
    BEFORE INSERT OR UPDATE ON hyperlocal_site_domains
    FOR EACH ROW EXECUTE FUNCTION hyperlocal_site_domains_check_transition();

-- ── the trail ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hyperlocal_site_events (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id           uuid NOT NULL,
    subject           text NOT NULL CHECK (subject IN ('site', 'domain')),
    -- Domain events only. No FK: the trail outlives nothing, and a claim
    -- leaves only with its site, which takes these rows with it.
    domain_id         uuid,
    hostname          text,
    from_status       text,
    to_status         text NOT NULL,
    from_revision_id  uuid,
    to_revision_id    uuid,
    evidence          jsonb CHECK (evidence IS NULL OR jsonb_typeof(evidence) = 'object'),
    actor             text NOT NULL,
    actor_role        text,
    source            text NOT NULL DEFAULT 'trigger' CHECK (source IN ('trigger', 'backfill')),
    occurred_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT hyperlocal_site_events_site_fk
        FOREIGN KEY (tenant_id, site_id)
        REFERENCES hyperlocal_sites(tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT hyperlocal_site_events_subject_chk CHECK (
        (subject = 'site' AND domain_id IS NULL AND hostname IS NULL)
        OR (subject = 'domain' AND domain_id IS NOT NULL AND hostname IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_hyperlocal_site_events_site
    ON hyperlocal_site_events (tenant_id, site_id, occurred_at DESC);

ALTER TABLE hyperlocal_site_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS hyperlocal_site_events_tenant_isolation ON hyperlocal_site_events;
CREATE POLICY hyperlocal_site_events_tenant_isolation ON hyperlocal_site_events
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- Default privileges give oracle_app INSERT/UPDATE/DELETE on every new table;
-- this one is read-only to the application, so take them back.
GRANT SELECT ON hyperlocal_site_events TO oracle_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON hyperlocal_site_events FROM oracle_app;

COMMENT ON TABLE hyperlocal_site_events IS
    '0173 (T14.04): append-only site and domain transitions, written by trigger with the acting agent; '
    'source=backfill rows state the live revision at migration time, not history.';

CREATE OR REPLACE FUNCTION hyperlocal_site_events_immutable()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'hyperlocal_site_events is append-only (UPDATE refused)'
        USING ERRCODE = '42501';
END $$;
REVOKE ALL ON FUNCTION hyperlocal_site_events_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS hyperlocal_site_events_no_change ON hyperlocal_site_events;
CREATE TRIGGER hyperlocal_site_events_no_change
    BEFORE UPDATE ON hyperlocal_site_events
    FOR EACH ROW EXECUTE FUNCTION hyperlocal_site_events_immutable();

-- SECURITY DEFINER: the application cannot INSERT into the trail itself; only
-- these triggers write, for the row that just changed.
CREATE OR REPLACE FUNCTION hyperlocal_sites_write_event()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
DECLARE evidence_text text := NULLIF(current_setting('app.site_event_evidence', true), '');
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status
       AND NEW.published_revision_id IS NOT DISTINCT FROM OLD.published_revision_id THEN
        RETURN NULL;
    END IF;
    INSERT INTO hyperlocal_site_events
        (tenant_id, site_id, subject, from_status, to_status, from_revision_id, to_revision_id,
         evidence, actor, actor_role)
    VALUES (
        NEW.tenant_id, NEW.id, 'site',
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.published_revision_id END, NEW.published_revision_id,
        evidence_text::jsonb,
        COALESCE(NULLIF(current_setting('app.current_agent', true), ''), session_user),
        NULLIF(current_setting('app.current_role', true), '')
    );
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION hyperlocal_sites_write_event() FROM PUBLIC;

DROP TRIGGER IF EXISTS hyperlocal_sites_event_write ON hyperlocal_sites;
CREATE TRIGGER hyperlocal_sites_event_write
    AFTER INSERT OR UPDATE OF status, published_revision_id ON hyperlocal_sites
    FOR EACH ROW EXECUTE FUNCTION hyperlocal_sites_write_event();

CREATE OR REPLACE FUNCTION hyperlocal_site_domains_write_event()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    INSERT INTO hyperlocal_site_events
        (tenant_id, site_id, subject, domain_id, hostname, from_status, to_status, evidence, actor, actor_role)
    VALUES (
        NEW.tenant_id, NEW.site_id, 'domain', NEW.id, NEW.hostname,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status,
        NEW.last_check,
        COALESCE(NULLIF(current_setting('app.current_agent', true), ''), session_user),
        NULLIF(current_setting('app.current_role', true), '')
    );
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION hyperlocal_site_domains_write_event() FROM PUBLIC;

DROP TRIGGER IF EXISTS hyperlocal_site_domains_event_write ON hyperlocal_site_domains;
CREATE TRIGGER hyperlocal_site_domains_event_write
    AFTER INSERT OR UPDATE OF status ON hyperlocal_site_domains
    FOR EACH ROW EXECUTE FUNCTION hyperlocal_site_domains_write_event();

-- One stated row per site published now, once.
INSERT INTO hyperlocal_site_events
    (tenant_id, site_id, subject, from_status, to_status, to_revision_id, evidence, actor, source, occurred_at)
SELECT s.tenant_id, s.id, 'site', NULL, s.status, s.published_revision_id,
       jsonb_build_object('note', 'live revision at migration time, not history'),
       'migration-0173', 'backfill', now()
  FROM hyperlocal_sites s
 WHERE s.status = 'published' AND s.published_revision_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM hyperlocal_site_events e WHERE e.site_id = s.id AND e.source = 'backfill');

-- ── revisions are immutable ─────────────────────────────────────────────────

REVOKE UPDATE ON hyperlocal_site_revisions FROM oracle_app;

CREATE OR REPLACE FUNCTION hyperlocal_site_revisions_refuse_update()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'hyperlocal_site_revisions are immutable (UPDATE refused); save a new revision'
        USING ERRCODE = '42501';
END $$;
REVOKE ALL ON FUNCTION hyperlocal_site_revisions_refuse_update() FROM PUBLIC;

DROP TRIGGER IF EXISTS hyperlocal_site_revisions_immutable ON hyperlocal_site_revisions;
CREATE TRIGGER hyperlocal_site_revisions_immutable
    BEFORE UPDATE ON hyperlocal_site_revisions
    FOR EACH ROW EXECUTE FUNCTION hyperlocal_site_revisions_refuse_update();
