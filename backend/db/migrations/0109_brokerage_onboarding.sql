-- 0109 — Brokerage onboarding: profile, agent invitations, resumable setup state.
--
-- Goal: a real brokerage owner can sign up, describe their business, invite
-- their agents, and have those agents land in the right tenant — with no
-- developer, no manual SQL, and no tenant id ever copied by hand.
--
-- Three things this file deliberately does NOT do:
--
--   * It does not create a second organization/membership/user system. A
--     tenant IS a brokerage (0001 calls them "Domains (brokerages)"), and
--     team_memberships (0027) is already the roster. Both are extended.
--
--   * It does not add a public business number column. That number lives in
--     telephony_routes.voice_caller_id_e164 and is shared with messaging via
--     0108's join. A second copy would immediately disagree with the first.
--
--   * It does not invent multi-brokerage membership. users.tenant_id is NOT
--     NULL and lower(agent_id) is globally UNIQUE (0001 + 0082), so one email
--     is one account in one tenant. Inviting an address that already belongs
--     to another brokerage is a conflict the API reports honestly rather than
--     a move this migration pretends to support.

BEGIN;

-- ── Guard: refuse to run on top of an incompatible earlier attempt ───────
-- A dev database was found carrying a brokerage_invitations table from an
-- abandoned "0106_brokerage_setup.sql" whose source file exists nowhere in
-- the repository or git history. Its shape differs (surrogate id PK, status
-- enum, a separate brokerage_profiles table). CREATE TABLE IF NOT EXISTS
-- silently skips such a table and then every later statement fails on a
-- missing column — an error that says nothing about the real cause.
--
-- Fail here instead, naming the problem.
DO $$
BEGIN
    IF to_regclass('public.brokerage_invitations') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = 'brokerage_invitations'
              AND column_name  = 'consumed_at'
       )
    THEN
        RAISE EXCEPTION
            'brokerage_invitations already exists in an incompatible shape '
            '(no consumed_at column). This database carries an abandoned '
            'pre-0109 brokerage attempt. Drop brokerage_invitations and '
            'brokerage_profiles, delete the stale schema_migrations row, '
            'then re-run.'
            USING ERRCODE = '42710';
    END IF;
END $$;

-- ── Brokerage profile ────────────────────────────────────────────────────
-- tenants has been id/slug/name since 0001 and has never been altered. These
-- are the minimum facts needed to run a brokerage account; EIN, licences and
-- tax forms are deliberately absent because nothing in the product requires
-- them to get started.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS org_type text NOT NULL DEFAULT 'brokerage'
        CHECK (org_type IN ('brokerage', 'team', 'independent_agent')),
    ADD COLUMN IF NOT EXISTS primary_state text
        CHECK (primary_state IS NULL OR primary_state ~ '^[A-Z]{2}$'),
    ADD COLUMN IF NOT EXISTS website text,
    ADD COLUMN IF NOT EXISTS profile_completed_at timestamptz;

-- tenants carried NO row-level security at all: any authenticated session
-- could read or write every brokerage row, and the only reason that was not
-- already a leak is that the handful of cross-tenant readers (admin_ops, the
-- contact-search backfill, /auth/register) all run as platform_admin.
--
-- The columns above turn this table into business data worth reading, so the
-- hole gets closed in the same migration that creates the incentive. Every
-- known cross-tenant caller satisfies app_is_platform_admin(); a normal
-- session now sees exactly its own brokerage.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenants_self_or_admin ON tenants;
CREATE POLICY tenants_self_or_admin ON tenants
    USING (app_is_platform_admin() OR id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR id = app_current_tenant());

-- ── Invitations ──────────────────────────────────────────────────────────
-- Modelled on password_reset_tokens (0041): only the SHA-256 digest of the
-- token is stored, so a database read cannot reconstruct a working invite
-- link. The plaintext exists only long enough to build the email.
--
-- This cannot be folded into team_memberships: that table's user_id is
-- NOT NULL REFERENCES users(id), and the whole point of an invitation is that
-- the person does not have an account yet.
CREATE TABLE IF NOT EXISTS brokerage_invitations (
    token_hash      char(64) PRIMARY KEY
                        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    -- The public handle. The API addresses invitations by this and never by
    -- token_hash: a digest in a URL is a secret in a log file, a referrer
    -- header and a browser history.
    id              uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email           text NOT NULL CHECK (email = lower(email) AND email <> ''),
    -- Never platform_admin: an invitation must not be able to mint god-mode.
    invited_role    text NOT NULL DEFAULT 'agent'
                        CHECK (invited_role IN ('agent', 'broker_owner')),
    invited_by      uuid NOT NULL,
    invited_by_agent_id text NOT NULL,
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz,
    consumed_by     uuid,
    revoked_at      timestamptz,
    revoked_by_agent_id text,
    -- Resend cannot re-send the original link: only its digest was kept, by
    -- design. A resend therefore revokes this row and issues a new one,
    -- carrying send_count forward so the roster can still say "sent 3 times".
    last_sent_at    timestamptz NOT NULL DEFAULT now(),
    send_count      integer NOT NULL DEFAULT 1 CHECK (send_count > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT brokerage_invitations_inviter_fkey
        FOREIGN KEY (invited_by, tenant_id)
        REFERENCES users (id, tenant_id)
        ON DELETE CASCADE,
    CONSTRAINT brokerage_invitations_expiry_after_creation
        CHECK (expires_at > created_at),
    CONSTRAINT brokerage_invitations_consumed_after_creation
        CHECK (consumed_at IS NULL OR consumed_at >= created_at),
    -- A consumed invitation must record who consumed it, and an unconsumed
    -- one must not name a consumer. Keeps replay forensics unambiguous.
    CONSTRAINT brokerage_invitations_consumer_agrees_with_state
        CHECK ((consumed_at IS NULL) = (consumed_by IS NULL))
);

-- At most one LIVE invitation per (brokerage, email). Expiry is not in the
-- predicate because now() is not immutable — the API revokes the previous
-- row before issuing a replacement, which keeps this index simple and makes
-- "invite again after it lapsed" an explicit, audited transition.
CREATE UNIQUE INDEX IF NOT EXISTS idx_brokerage_invitations_live
    ON brokerage_invitations (tenant_id, email)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_brokerage_invitations_tenant_created
    ON brokerage_invitations (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_brokerage_invitations_pending_expiry
    ON brokerage_invitations (expires_at)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

DROP TRIGGER IF EXISTS trg_brokerage_invitations_updated ON brokerage_invitations;
CREATE TRIGGER trg_brokerage_invitations_updated
    BEFORE UPDATE ON brokerage_invitations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE brokerage_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE brokerage_invitations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS brokerage_invitations_tenant_isolation ON brokerage_invitations;
CREATE POLICY brokerage_invitations_tenant_isolation ON brokerage_invitations
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- 0001 grants blanket DML on every table. Narrow this one to issuance,
-- listing, and the two state transitions it is allowed to make.
REVOKE ALL ON brokerage_invitations FROM PUBLIC;
REVOKE ALL ON brokerage_invitations FROM oracle_app;
GRANT SELECT, INSERT ON brokerage_invitations TO oracle_app;
GRANT UPDATE (consumed_at, consumed_by, revoked_at, revoked_by_agent_id,
              last_sent_at, send_count, updated_at)
    ON brokerage_invitations TO oracle_app;

-- ── Setup progress ───────────────────────────────────────────────────────
-- Only the capabilities with no live source of truth are stored. phone,
-- messaging, billing and the roster are DERIVED from their real tables at
-- read time, because a cached copy of "phone is ready" is a copy that can be
-- wrong. What cannot be derived is intent: "we imported contacts already",
-- "skip this for now".
CREATE TABLE IF NOT EXISTS brokerage_setup_progress (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    capability  text NOT NULL CHECK (capability IN (
                    'brokerage_profile', 'agent_invites', 'contact_import',
                    'email_calendar', 'phone', 'mls', 'billing', 'readiness')),
    status      text NOT NULL CHECK (status IN (
                    'NOT_STARTED', 'NEEDS_ACTION', 'IN_PROGRESS',
                    'READY', 'BLOCKED', 'ERROR', 'OPTIONAL')),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_by_agent_id text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, capability)
);

DROP TRIGGER IF EXISTS trg_brokerage_setup_progress_updated ON brokerage_setup_progress;
CREATE TRIGGER trg_brokerage_setup_progress_updated
    BEFORE UPDATE ON brokerage_setup_progress
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE brokerage_setup_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE brokerage_setup_progress FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS brokerage_setup_progress_tenant_isolation ON brokerage_setup_progress;
CREATE POLICY brokerage_setup_progress_tenant_isolation ON brokerage_setup_progress
    USING (app_is_platform_admin() OR tenant_id = app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id = app_current_tenant());

-- ── Accepting an invitation ──────────────────────────────────────────────
-- The invitee has no tenant context: they are either signed out entirely or
-- signed into a different brokerage. RLS therefore hides the invitation row
-- from exactly the person who needs to read it, so the two operations below
-- are SECURITY DEFINER and keyed on the token digest.
--
-- That is safe because the digest is the capability: recovering it needs the
-- 32-byte token from the emailed link. Neither function accepts a tenant id,
-- so neither can be aimed at a brokerage by a caller.

-- Read-only preview, for rendering the accept screen before signup.
-- Returns no secret material and nothing that identifies a tenant beyond the
-- brokerage's display name — which the invitee is being invited to join.
CREATE OR REPLACE FUNCTION brokerage_invitation_preview(p_token_hash char(64))
RETURNS TABLE (
    tenant_id      uuid,
    tenant_name    text,
    email          text,
    invited_role   text,
    invited_by_agent_id text,
    expires_at     timestamptz,
    state          text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT i.tenant_id,
           t.name,
           i.email,
           i.invited_role,
           i.invited_by_agent_id,
           i.expires_at,
           CASE
               WHEN i.revoked_at  IS NOT NULL THEN 'revoked'
               WHEN i.consumed_at IS NOT NULL THEN 'accepted'
               WHEN i.expires_at <= now()     THEN 'expired'
               ELSE 'pending'
           END
      FROM brokerage_invitations i
      JOIN tenants t ON t.id = i.tenant_id
     WHERE i.token_hash = p_token_hash;
$$;

-- Single-use claim. The UPDATE's own WHERE clause is the concurrency guard:
-- two simultaneous accepts both try to move consumed_at from NULL, exactly
-- one row is returned, and the loser gets no row rather than a second
-- membership. No advisory lock, no read-then-write race.
CREATE OR REPLACE FUNCTION consume_brokerage_invitation(
    p_token_hash char(64),
    p_user_id    uuid
)
RETURNS TABLE (tenant_id uuid, email text, invited_role text)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE brokerage_invitations
       SET consumed_at = now(),
           consumed_by = p_user_id
     WHERE token_hash  = p_token_hash
       AND consumed_at IS NULL
       AND revoked_at  IS NULL
       AND expires_at  > now()
    RETURNING brokerage_invitations.tenant_id,
              brokerage_invitations.email,
              brokerage_invitations.invited_role;
$$;

-- 0003 revoked PUBLIC execute on every function; these need it back for the
-- application role and nobody else.
REVOKE ALL ON FUNCTION brokerage_invitation_preview(char) FROM PUBLIC;
REVOKE ALL ON FUNCTION consume_brokerage_invitation(char, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION brokerage_invitation_preview(char) TO oracle_app;
GRANT EXECUTE ON FUNCTION consume_brokerage_invitation(char, uuid) TO oracle_app;

COMMIT;
