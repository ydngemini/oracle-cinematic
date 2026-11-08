-- ---------------------------------------------------------------------------
-- 0163 — every deal status change is an immutable event, and an illegal one
-- is refused by the database (prompt 13, T13.02 first slice, AC13.01; ledger
-- L13-03).
--
-- Before: transactions.status moved by plain UPDATEs in portfolio_api
-- (accept offer, close, lose). Each route checked its own "from" state, but
-- nothing kept WHEN a deal moved, WHO moved it or FROM what — the row only
-- held the latest state — and any other writer could set any status.
--
-- Now:
--   transaction_status_events   append-only: one row per insert of a deal and
--     per status change, written by trigger (the application cannot forget
--     it and cannot write one itself). source = 'trigger' for real
--     transitions; 'backfill' for the one row this migration gives each
--     existing deal, stating its status as of now — not a reconstructed
--     history, and labelled so.
--   transactions_status_transition  BEFORE UPDATE OF status: only these moves
--     are legal, matching what the routes already allow:
--         active          -> under_contract | lost | cancelled
--         under_contract  -> closed | lost | cancelled
--     closed, lost and cancelled are terminal. Anything else raises SQLSTATE
--     23514 (check_violation) with the pair named. A deal falling out of
--     contract back to active is NOT legal yet: whether a fallen-through
--     contract reopens the same deal is the owner's call (L13-03).
--
-- Reading follows the deal (an EXISTS on transactions under the caller's own
-- RLS: 0131 / 0155 decide), behind the tenant wall. The application role is
-- granted SELECT only (no INSERT, UPDATE or DELETE); an UPDATE is refused for
-- everyone by trigger. Rows leave only with their deal (FK cascade) or by
-- tenant erasure (privacy_erase_tenant_batch, 0122, which runs as owner).
--
-- Previous release on this schema: unaffected — it never reads the table,
-- and every transition it performs is in the legal set (the routes check
-- the same "from" states).
--
-- Rollback: DROP TRIGGER transactions_status_event_write,
-- transactions_status_transition ON transactions; DROP TABLE
-- transaction_status_events; DROP FUNCTION transactions_write_status_event(),
-- transactions_check_status_transition(), transaction_status_events_immutable().
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transaction_status_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    transaction_id  uuid NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    from_status     text,
    to_status       text NOT NULL,
    deal_version    integer NOT NULL,
    actor           text NOT NULL,
    source          text NOT NULL DEFAULT 'trigger' CHECK (source IN ('trigger', 'backfill')),
    occurred_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transaction_status_events_deal
    ON transaction_status_events (tenant_id, transaction_id, occurred_at);

ALTER TABLE transaction_status_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction_status_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS transaction_status_events_tenant_isolation ON transaction_status_events;
CREATE POLICY transaction_status_events_tenant_isolation ON transaction_status_events
    USING (tenant_id >= app_tenant_floor() AND tenant_id <= app_tenant_ceiling())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

DROP POLICY IF EXISTS transaction_status_events_deal_read ON transaction_status_events;
CREATE POLICY transaction_status_events_deal_read ON transaction_status_events AS RESTRICTIVE FOR SELECT USING (
    (SELECT app_is_platform_admin())
    OR (SELECT app_current_role()) = 'broker_owner'
    OR (SELECT COALESCE(current_setting('app.record_scope', true), '')) = 'brokerage'
    OR EXISTS (SELECT 1 FROM public.transactions t WHERE t.id = transaction_status_events.transaction_id));

-- Default privileges (0003) give oracle_app INSERT/UPDATE/DELETE on every new
-- table; this one is read-only to the application, so take them back.
GRANT SELECT ON transaction_status_events TO oracle_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON transaction_status_events FROM oracle_app;

COMMENT ON TABLE transaction_status_events IS
    '0163 (L13-03): append-only deal status trail, written by trigger; source=backfill rows state the status at migration time, not history.';

-- Immutable: no UPDATE by anyone. (DELETE is not granted to the application;
-- erasure and the deal's cascade run as owner.)
CREATE OR REPLACE FUNCTION transaction_status_events_immutable()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'transaction_status_events is append-only (UPDATE refused)'
        USING ERRCODE = '42501';
END $$;
REVOKE ALL ON FUNCTION transaction_status_events_immutable() FROM PUBLIC;

DROP TRIGGER IF EXISTS transaction_status_events_no_change ON transaction_status_events;
CREATE TRIGGER transaction_status_events_no_change
    BEFORE UPDATE ON transaction_status_events
    FOR EACH ROW EXECUTE FUNCTION transaction_status_events_immutable();

-- The legal moves.
CREATE OR REPLACE FUNCTION transactions_check_status_transition()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) IN (
        ('active', 'under_contract'), ('active', 'lost'), ('active', 'cancelled'),
        ('under_contract', 'closed'), ('under_contract', 'lost'), ('under_contract', 'cancelled')
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal deal transition: % -> %', OLD.status, NEW.status
        USING ERRCODE = '23514';
END $$;
REVOKE ALL ON FUNCTION transactions_check_status_transition() FROM PUBLIC;

DROP TRIGGER IF EXISTS transactions_status_transition ON transactions;
CREATE TRIGGER transactions_status_transition
    BEFORE UPDATE OF status ON transactions
    FOR EACH ROW EXECUTE FUNCTION transactions_check_status_transition();

-- The trail. SECURITY DEFINER: the application cannot INSERT into the table
-- itself; only this trigger writes, for the row that just changed.
CREATE OR REPLACE FUNCTION transactions_write_status_event()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    INSERT INTO transaction_status_events
        (tenant_id, transaction_id, from_status, to_status, deal_version, actor)
    VALUES (
        NEW.tenant_id, NEW.id,
        CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
        NEW.status, NEW.version,
        COALESCE(NULLIF(current_setting('app.current_agent', true), ''), NEW.updated_by, NEW.created_by, session_user)
    );
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION transactions_write_status_event() FROM PUBLIC;

DROP TRIGGER IF EXISTS transactions_status_event_write ON transactions;
CREATE TRIGGER transactions_status_event_write
    AFTER INSERT OR UPDATE OF status ON transactions
    FOR EACH ROW EXECUTE FUNCTION transactions_write_status_event();

-- One stated row per existing deal, once.
INSERT INTO transaction_status_events
    (tenant_id, transaction_id, from_status, to_status, deal_version, actor, source, occurred_at)
SELECT t.tenant_id, t.id, NULL, t.status, t.version, 'migration-0163', 'backfill', now()
  FROM transactions t
 WHERE NOT EXISTS (SELECT 1 FROM transaction_status_events e WHERE e.transaction_id = t.id);
