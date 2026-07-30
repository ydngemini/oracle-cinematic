-- Runtime assertions for the 0027 intelligence platform RLS and append-only data.
-- Run as a superuser; every assertion executes under oracle_app and rolls back.
\set ON_ERROR_STOP on

BEGIN;

-- Make the audit self-contained on both a pristine database and an existing
-- development database. Seed as the migration owner before adopting the
-- non-owner application role; the outer rollback leaves no fixtures behind.
INSERT INTO tenants (id, slug, name) VALUES
    ('11111111-1111-1111-1111-111111111111', 'platform-rls-apex', 'Platform RLS Apex')
ON CONFLICT (id) DO NOTHING;

SET LOCAL ROLE oracle_app;

SELECT set_config('app.current_tenant', '11111111-1111-1111-1111-111111111111', true);
SELECT set_config('app.current_role', 'agent', true);

INSERT INTO source_licenses (
    tenant_id,source_key,source_name,license_name,property_level_allowed
) VALUES (
    '11111111-1111-1111-1111-111111111111','platform-rls-test','RLS Test Source',
    'public-record',true
);

INSERT INTO source_records (
    tenant_id,source_license_id,source_key,record_key,property_key,
    observed_at,request_hash,payload_hash,raw_payload,expires_at
) SELECT
    '11111111-1111-1111-1111-111111111111',id,'platform-rls-test','record-1','parcel-1',
    now(),repeat('a',64),repeat('b',64),'{}'::jsonb,now()-interval '1 minute'
FROM source_licenses WHERE source_key='platform-rls-test';

INSERT INTO oauth_authorization_states (
    tenant_id,state_hash,account_label,code_verifier_ciphertext,
    redirect_uri,return_path,scopes,created_by,created_at,expires_at
) VALUES (
    '11111111-1111-1111-1111-111111111111',repeat('c',64),'agent-a',decode('00','hex'),
    'https://api.example.test/api/commands/providers/google/oauth/callback','/profile',
    ARRAY['openid'],'agent-a',now()-interval '8 days',now()-interval '8 days'+interval '10 minutes'
);

DO $$
BEGIN
    IF (SELECT count(*) FROM source_records WHERE source_key='platform-rls-test') <> 1 THEN
        RAISE EXCEPTION 'tenant A cannot read its source record';
    END IF;
    IF (SELECT count(*) FROM oauth_authorization_states WHERE account_label='agent-a') <> 1 THEN
        RAISE EXCEPTION 'tenant A cannot read its OAuth state';
    END IF;
END $$;

SELECT set_config('app.current_tenant', '00000000-0000-0000-0000-000000000000', true);
SELECT set_config('app.current_role', 'agent', true);

DO $$
BEGIN
    IF (SELECT count(*) FROM source_records WHERE source_key='platform-rls-test') <> 0 THEN
        RAISE EXCEPTION 'tenant B read tenant A source record';
    END IF;
    IF (SELECT count(*) FROM oauth_authorization_states WHERE account_label='agent-a') <> 0 THEN
        RAISE EXCEPTION 'tenant B read tenant A OAuth state';
    END IF;
    BEGIN
        INSERT INTO source_licenses (
            tenant_id,source_key,source_name,license_name,property_level_allowed
        ) VALUES (
            '11111111-1111-1111-1111-111111111111','cross-tenant-write','Blocked',
            'public-record',true
        );
        RAISE EXCEPTION 'cross-tenant INSERT unexpectedly succeeded';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        PERFORM purge_expired_platform_data(730, 365);
        RAISE EXCEPTION 'non-admin retention cleanup unexpectedly succeeded';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        PERFORM purge_expired_oauth_states();
        RAISE EXCEPTION 'non-admin OAuth retention cleanup unexpectedly succeeded';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END $$;

SELECT set_config('app.current_tenant', '11111111-1111-1111-1111-111111111111', true);
SELECT set_config('app.current_role', 'agent', true);

DO $$
BEGIN
    BEGIN
        UPDATE source_records SET raw_payload='{"tampered":true}'::jsonb
         WHERE source_key='platform-rls-test';
        RAISE EXCEPTION 'append-only source record UPDATE unexpectedly succeeded';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    UPDATE oauth_authorization_states SET consumed_at=now()
     WHERE account_label='agent-a';
    BEGIN
        UPDATE oauth_authorization_states SET return_path='/tampered'
         WHERE account_label='agent-a';
        RAISE EXCEPTION 'OAuth state protocol fields unexpectedly mutable';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END $$;

SELECT set_config('app.current_tenant', '00000000-0000-0000-0000-000000000000', true);
SELECT set_config('app.current_role', 'platform_admin', true);

DO $$
BEGIN
    PERFORM purge_expired_platform_data(730, 365);
    PERFORM purge_expired_oauth_states();
    IF (SELECT count(*) FROM source_records WHERE source_key='platform-rls-test') <> 1 THEN
        RAISE EXCEPTION 'platform admin bypass cannot read tenant record';
    END IF;
    IF (SELECT raw_payload->>'retention_status' FROM source_records
         WHERE source_key='platform-rls-test') <> 'purged' THEN
        RAISE EXCEPTION 'platform retention cleanup did not redact expired raw payload';
    END IF;
    IF (SELECT count(*) FROM oauth_authorization_states WHERE account_label='agent-a') <> 0 THEN
        RAISE EXCEPTION 'OAuth retention cleanup did not delete expired state';
    END IF;
END $$;

ROLLBACK;
\echo '0027-0028 platform RLS: all assertions passed.'
