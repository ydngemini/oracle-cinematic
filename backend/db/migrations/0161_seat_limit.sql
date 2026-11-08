-- ---------------------------------------------------------------------------
-- 0161 — a brokerage cannot hold more active seats than its plan (prompt 18,
-- T18.04, AC18.02; ledger L18-05).
--
-- Before: invitations and reinstatement checked membership, never a seat
-- count (brokerage_onboarding.py has no seat logic), so a plan sold as 20
-- named users had no ceiling, and two acceptances at once could not be
-- serialised against one even if a check were added in Python.
--
-- A seat is an ACTIVE user of the brokerage who is not a platform admin
-- (users.is_active AND role <> 'platform_admin'). The broker-owner counts;
-- whether the plan's number includes the owner is the owner's decision
-- (T18.02, D-2026-10-12-02), so the number itself lives in data:
--
--   tenants.seat_limit  NULL  = no ceiling enforced (every brokerage today;
--                               nothing changes until the plan is decided)
--                       n > 0 = at most n active seats
--
-- Enforced in the database, so every path that makes a user active is
-- covered — invitation acceptance (auth.accept_invite), reinstatement
-- (brokerage_onboarding), owner signup and scripts — not just one route:
--
--   users_seat_limit  BEFORE INSERT OR UPDATE OF is_active, role, tenant_id
--   on users. When the resulting row holds a seat, it locks the tenant row
--   (SELECT ... FOR UPDATE) and counts the other active seats. Concurrent
--   activations in one brokerage therefore serialise on that lock, and the
--   (n+1)th raises SQLSTATE 'NSEAT' instead of committing. Rows that do not
--   gain a seat (a suspension, a profile edit, an already-active user) are
--   not checked, so suspension frees a seat and never charges one.
--
--   tenants_seat_limit_guard  BEFORE UPDATE OF seat_limit on tenants: on an
--   application connection (a login that is a member of oracle_app and not a
--   superuser; current_user inside a SECURITY DEFINER function is its owner,
--   so session_user is what is checked) only a platform admin may change
--   it (the column is billing, not a brokerage setting; AC18.05). Migrations
--   and operators on their own role are not the application. Lowering it below the active count is allowed — no
--   one is removed — and blocks further activations until seats are freed.
--
-- Both functions are SECURITY DEFINER with a pinned search_path so the count
-- sees every user of the brokerage whatever the caller's RLS context; they
-- read only the brokerage of the row being written.
--
-- Previous release on this schema: unaffected (seat_limit is NULL for every
-- brokerage; the trigger returns at once when it is).
--
-- Rollback: DROP TRIGGER users_seat_limit ON users; DROP TRIGGER
-- tenants_seat_limit_guard ON tenants; DROP FUNCTION users_enforce_seat_limit(),
-- tenants_guard_seat_limit(); ALTER TABLE tenants DROP COLUMN seat_limit.
-- ---------------------------------------------------------------------------

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS seat_limit integer;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_seat_limit_positive') THEN
        ALTER TABLE tenants ADD CONSTRAINT tenants_seat_limit_positive CHECK (seat_limit IS NULL OR seat_limit > 0);
    END IF;
END $$;

COMMENT ON COLUMN tenants.seat_limit IS
    '0161 (L18-05): at most this many active non-platform-admin users; NULL = not enforced. Set by a platform admin only.';

CREATE OR REPLACE FUNCTION users_enforce_seat_limit()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
DECLARE
    lim integer;
    used integer;
BEGIN
    IF NOT (NEW.is_active AND NEW.role <> 'platform_admin') THEN
        RETURN NEW;   -- the row holds no seat
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.is_active AND OLD.role <> 'platform_admin'
       AND OLD.tenant_id = NEW.tenant_id THEN
        RETURN NEW;   -- already held one here; nothing gained
    END IF;
    -- Lock first, then read the limit and count: concurrent activations in
    -- this brokerage wait here and each sees the others' committed rows.
    SELECT seat_limit INTO lim FROM tenants WHERE id = NEW.tenant_id FOR UPDATE;
    IF lim IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT count(*) INTO used
      FROM users
     WHERE tenant_id = NEW.tenant_id AND is_active AND role <> 'platform_admin'
       AND id IS DISTINCT FROM NEW.id;
    IF used + 1 > lim THEN
        RAISE EXCEPTION 'seat limit reached: % of % seats in use', used, lim
            USING ERRCODE = 'NSEAT',
                  HINT = 'Suspend a member to free a seat, or change the plan.';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION users_enforce_seat_limit() FROM PUBLIC;

DROP TRIGGER IF EXISTS users_seat_limit ON users;
CREATE TRIGGER users_seat_limit
    BEFORE INSERT OR UPDATE OF is_active, role, tenant_id ON users
    FOR EACH ROW EXECUTE FUNCTION users_enforce_seat_limit();

CREATE OR REPLACE FUNCTION tenants_guard_seat_limit()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $$
BEGIN
    IF NEW.seat_limit IS DISTINCT FROM OLD.seat_limit AND NOT app_is_platform_admin()
       AND pg_has_role(session_user, 'oracle_app', 'MEMBER')
       AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = session_user) THEN
        RAISE EXCEPTION 'only a platform admin may change a brokerage''s seat limit'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION tenants_guard_seat_limit() FROM PUBLIC;

DROP TRIGGER IF EXISTS tenants_seat_limit_guard ON tenants;
CREATE TRIGGER tenants_seat_limit_guard
    BEFORE UPDATE OF seat_limit ON tenants
    FOR EACH ROW EXECUTE FUNCTION tenants_guard_seat_limit();
