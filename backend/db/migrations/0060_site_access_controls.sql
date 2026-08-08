-- Personal/team site visibility with explicit edit and publish collaborators.

BEGIN;

ALTER TABLE hyperlocal_sites
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'personal';
ALTER TABLE hyperlocal_sites
    DROP CONSTRAINT IF EXISTS hyperlocal_sites_scope_chk;
ALTER TABLE hyperlocal_sites
    ADD CONSTRAINT hyperlocal_sites_scope_chk CHECK (scope IN ('personal','team'));

CREATE TABLE IF NOT EXISTS hyperlocal_site_collaborators (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_id     uuid NOT NULL,
    agent_id    text NOT NULL,
    can_edit    boolean NOT NULL DEFAULT true,
    can_publish boolean NOT NULL DEFAULT false,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,site_id,agent_id),
    CONSTRAINT hyperlocal_site_collaborators_site_fk
        FOREIGN KEY (tenant_id,site_id)
        REFERENCES hyperlocal_sites(tenant_id,id) ON DELETE CASCADE,
    CONSTRAINT hyperlocal_site_collaborators_capability_chk
        CHECK (can_edit OR can_publish)
);

CREATE INDEX IF NOT EXISTS idx_hyperlocal_site_collaborators_agent
    ON hyperlocal_site_collaborators (tenant_id,agent_id,site_id);

DROP TRIGGER IF EXISTS trg_hyperlocal_site_collaborators_updated
    ON hyperlocal_site_collaborators;
CREATE TRIGGER trg_hyperlocal_site_collaborators_updated
    BEFORE UPDATE ON hyperlocal_site_collaborators
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE hyperlocal_site_collaborators ENABLE ROW LEVEL SECURITY;
ALTER TABLE hyperlocal_site_collaborators FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hyperlocal_site_collaborators_tenant_isolation
    ON hyperlocal_site_collaborators;
CREATE POLICY hyperlocal_site_collaborators_tenant_isolation
    ON hyperlocal_site_collaborators
    USING (app_is_platform_admin() OR tenant_id=app_current_tenant())
    WITH CHECK (app_is_platform_admin() OR tenant_id=app_current_tenant());

GRANT SELECT,INSERT,UPDATE,DELETE ON hyperlocal_site_collaborators TO oracle_app;

COMMIT;
