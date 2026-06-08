// Single source of truth for operator + tenant identity across the whole app.
// Before this, the WebSocket used only user_id (tenant resolved server-side to
// ORACLE_DEMO_TENANT_ID) while billing used a separate 'default' tenant — so the
// DealPipeline (leads, scoped by tenant via RLS) and the subscription gate keyed
// off *different* tenants. Everything now flows through these two resolvers.
//
// Resolution order: build-time env → localStorage (post-login) → demo default.

// Must equal backend ORACLE_DEMO_TENANT_ID (.env) so leads ingested by the
// harvesters under the demo tenant are visible to a tokenless dev client.
const DEMO_TENANT_ID = 'a1b2c3d4-0000-4000-8000-000000000001';
const DEMO_USER_ID = 'demo-operator';

function fromStorage(key) {
  return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
}

export function getUserId() {
  return import.meta.env.VITE_USER_ID || fromStorage('oracle_user_id') || DEMO_USER_ID;
}

export function getTenantId() {
  return import.meta.env.VITE_TENANT_ID || fromStorage('oracle_tenant_id') || DEMO_TENANT_ID;
}
