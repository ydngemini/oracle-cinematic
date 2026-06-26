-- ===========================================================================
-- 0018_harvest_idempotency_and_force_rls.sql
--
-- Two forward-only, idempotent hardening fixes surfaced by the 2026-06-24
-- multi-agent branch review:
--
--   (A) FORCE ROW LEVEL SECURITY on the seven tenant-private tables created in
--       0013/0015. They ENABLE'd RLS but never FORCE'd it, breaking the 0001
--       house invariant ("FORCE so even a table owner is bound — defense in
--       depth", 0001:124). 0017 canonicalized the *policies* on these tables but
--       did not add FORCE, so a process connecting as the table owner would
--       bypass tenant isolation. The prod app role (oracle_app_login) is a
--       non-owner so ENABLE already binds it; this closes the owner-path gap.
--
--   (B) UNIQUE (tenant_id, parcel_id) on leads so re-harvests upsert in place
--       (see harvesters/base.py ON CONFLICT) instead of inserting a brand-new
--       row per parcel on every scheduled cycle, which multiplied the leads
--       table N× and corrupted pipeline/motivation counts.
--
-- Idempotent and non-destructive: re-applying is a no-op, and (B) never deletes
-- rows — if legacy duplicates exist the constraint is skipped with a warning so
-- FK-referenced data is never destroyed by a migration.
-- ===========================================================================

-- (A) FORCE RLS — same table list / idiom as 0017's canonicalize loop.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        -- 0013_state_compliance.sql
        'agent_licenses',
        'agent_ce_log',
        'transactions',
        'compliance_checklist_items',
        -- 0015_outreach_consent.sql
        'outreach_consent',
        'outreach_suppression',
        'outreach_attempt_log'
    ]
    LOOP
        CONTINUE WHEN to_regclass(t) IS NULL;   -- upstream table not applied yet
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;

-- (B) Idempotent harvest key. Non-destructive: catch the "already exists" and
-- the "duplicates present" cases instead of failing the whole migration.
DO $$
BEGIN
    IF to_regclass('leads') IS NULL THEN
        RETURN;
    END IF;
    BEGIN
        ALTER TABLE leads
            ADD CONSTRAINT leads_tenant_parcel_key UNIQUE (tenant_id, parcel_id);
    EXCEPTION
        WHEN duplicate_object THEN
            NULL;  -- constraint already present; safe to re-apply
        WHEN unique_violation THEN
            RAISE WARNING
                'leads has duplicate (tenant_id, parcel_id) rows; UNIQUE not added. '
                'Dedupe (keep newest per parcel), then re-run 0018 — the harvest '
                'upsert in harvesters/base.py depends on this constraint.';
    END;
END $$;
