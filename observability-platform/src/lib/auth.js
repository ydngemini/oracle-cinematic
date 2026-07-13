// Auth for the AWS observability dashboard. The /api/aws/ws stream is gated to
// platform_admin/broker_owner, so this app must obtain a real JWT before it can
// connect. Mirrors the main Neoh app: POST {VITE_API_BASE}/auth/login with
// { agent_id, passphrase } → { token, role, ... }.
const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');

// Roles the backend WS accepts (must match _OBS_ALLOWED_ROLES in aws_observability.py).
export const OBS_ROLES = new Set(['platform_admin', 'broker_owner']);

const TOKEN_KEY = 'oracle_token';
const ROLE_KEY = 'oracle_role';

// Resolve a token without a login round-trip from this app's sessionStorage.
// This app is served from a different origin than the main Neoh app (different
// port in dev, different subdomain in prod), so localStorage is never shared
// between them. Tokens must never be accepted from URLs or compiled into the
// static Vite bundle.
export function resolveToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export async function login(agentId, passphrase) {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_id: agentId, passphrase }),
  });
  if (!res.ok) {
    // Backend returns 401 for bad creds (deliberately indistinguishable from
    // unknown agent), 429 when rate-limited.
    const msg =
      res.status === 401 ? 'Invalid credentials.'
      : res.status === 429 ? 'Too many attempts — wait and retry.'
      : `Login failed (${res.status}).`;
    throw new Error(msg);
  }
  const body = await res.json();
  if (!OBS_ROLES.has(body.role)) {
    throw new Error('This account has no AWS observability access (platform_admin / broker_owner only).');
  }
  try {
    sessionStorage.setItem(TOKEN_KEY, body.token);
    if (body.role) sessionStorage.setItem(ROLE_KEY, body.role);
  } catch { /* storage disabled — token still returned for in-memory use */ }
  return body;
}

export function logout() {
  try {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(ROLE_KEY);
  } catch { /* ignore */ }
}
