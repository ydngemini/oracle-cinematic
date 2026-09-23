-- rls_brokerage.sql — what Postgres enforces, proved against Postgres.
--
-- The Python suite runs on fakes (see tests/conftest.py), which can prove the
-- application's decisions but not the database's. These five properties are
-- the database's, and asserting them against a fake would only prove the fake
-- agrees with me:
--
--   1. a brokerage sees only its own invitations
--   2. a brokerage sees only its own tenant row          (RLS added in 0109)
--   3. one brokerage cannot revoke another's invitation
--   4. one brokerage cannot insert into another          (WITH CHECK)
--   5. a consumed invitation cannot be consumed again    (single-use UPDATE)
--
-- Run as superuser against a database with 0109 applied:
--     psql -U postgres -d oracle -v ON_ERROR_STOP=1 -f tests/rls_brokerage.sql
-- It seeds, asserts, and rolls everything back.

\set ON_ERROR_STOP on
BEGIN;

INSERT INTO tenants (id, slug, name) VALUES
  ('aaaaaaaa-0000-0000-0000-00000000000a','rls-proof-a','Brokerage A'),
  ('bbbbbbbb-0000-0000-0000-00000000000b','rls-proof-b','Brokerage B');

INSERT INTO users (id, tenant_id, agent_id, role, email) VALUES
  ('aaaaaaaa-1111-0000-0000-00000000000a','aaaaaaaa-0000-0000-0000-00000000000a','proof-owner-a@rls.test','broker_owner','proof-owner-a@rls.test'),
  ('bbbbbbbb-1111-0000-0000-00000000000b','bbbbbbbb-0000-0000-0000-00000000000b','proof-owner-b@rls.test','broker_owner','proof-owner-b@rls.test');

INSERT INTO brokerage_invitations
  (token_hash, tenant_id, email, invited_role, invited_by, invited_by_agent_id, expires_at) VALUES
  (repeat('a',64),'aaaaaaaa-0000-0000-0000-00000000000a','proof-invitee-a@rls.test','agent','aaaaaaaa-1111-0000-0000-00000000000a','proof-owner-a@rls.test', now()+interval '7 days'),
  (repeat('b',64),'bbbbbbbb-0000-0000-0000-00000000000b','proof-invitee-b@rls.test','agent','bbbbbbbb-1111-0000-0000-00000000000b','proof-owner-b@rls.test', now()+interval '7 days');

-- Become the unprivileged application role and adopt Brokerage A's identity.
SET LOCAL ROLE oracle_app;
SELECT set_config('app.current_tenant','aaaaaaaa-0000-0000-0000-00000000000a',true),
       set_config('app.current_role','broker_owner',true);

DO $$
DECLARE n integer; e text;
BEGIN
    -- 1. Only its own invitations.
    SELECT count(*) INTO n FROM brokerage_invitations;
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL 1: A sees % invitations, expected 1', n; END IF;
    SELECT email INTO e FROM brokerage_invitations;
    IF e <> 'proof-invitee-a@rls.test' THEN RAISE EXCEPTION 'FAIL 1: A sees %', e; END IF;

    -- 2. Only its own tenant row.
    SELECT count(*) INTO n FROM tenants;
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL 2: A sees % tenants, expected 1', n; END IF;

    -- 3. Cannot revoke B's invitation: the row is invisible, so 0 updated.
    UPDATE brokerage_invitations
       SET revoked_at = now(), revoked_by_agent_id = 'proof-owner-a@rls.test'
     WHERE token_hash = repeat('b',64);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL 3: A revoked % of B''s invitations', n; END IF;

    -- 4. Cannot insert into B: WITH CHECK must reject it.
    BEGIN
        INSERT INTO brokerage_invitations
          (token_hash, tenant_id, email, invited_role, invited_by, invited_by_agent_id, expires_at)
        VALUES (repeat('c',64),'bbbbbbbb-0000-0000-0000-00000000000b','sneak@rls.test','agent',
                'bbbbbbbb-1111-0000-0000-00000000000b','proof-owner-a@rls.test', now()+interval '1 day');
        RAISE EXCEPTION 'FAIL 4: A inserted a row into B';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;  -- expected
    END;

    RAISE NOTICE 'isolation proofs 1-4 passed';
END $$;

-- 5. Single use. The invitee has NO tenant context at all — exactly a signed
--    out person clicking the link — so this also proves the SECURITY DEFINER
--    path works where a direct read cannot.
SELECT set_config('app.current_tenant','',true), set_config('app.current_role','',true);

DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM brokerage_invitations;
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL 5a: a contextless session read % rows', n; END IF;

    SELECT count(*) INTO n FROM brokerage_invitation_preview(repeat('b',64));
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL 5b: preview returned % rows', n; END IF;

    SELECT count(*) INTO n FROM consume_brokerage_invitation(
        repeat('b',64), 'bbbbbbbb-1111-0000-0000-00000000000b');
    IF n <> 1 THEN RAISE EXCEPTION 'FAIL 5c: first consume returned % rows', n; END IF;

    SELECT count(*) INTO n FROM consume_brokerage_invitation(
        repeat('b',64), 'bbbbbbbb-1111-0000-0000-00000000000b');
    IF n <> 0 THEN RAISE EXCEPTION 'FAIL 5d: replay returned % rows — token is reusable', n; END IF;

    RAISE NOTICE 'single-use proof 5 passed';
END $$;

ROLLBACK;
