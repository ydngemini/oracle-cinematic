// Auth for the AWS observability dashboard. The /api/aws/ws stream is gated to
// platform_admin/broker_owner, so this app must obtain a real JWT before it can
// connect. Mirrors the main Neoh app: POST {VITE_API_BASE}/auth/login with
// { agent_id, passphrase } → { token, role, ... }.
const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

// Roles the backend WS accepts (must match _OBS_ALLOWED_ROLES in aws_observability.py).
export const OBS_ROLES = new Set(['platform_admin', 'broker_owner']);

const TOKEN_KEY = 'oracle_token';
const ROLE_KEY = 'oracle_role';

// Resolve a token without a login round-trip: an explicit ?token= URL param, a
// build-time VITE_AWS_WS_TOKEN, then our own stored session (sessionStorage),
// then localStorage (same-origin share with the main app). First hit wins.
export function resolveToken() {
  try {
    const urlToken = new URLSearchParams(window.location.search).get('token');
    if (urlToken) return urlToken;
  } catch { /* non-browser env */ }
  if (import.meta.env.VITE_AWS_WS_TOKEN) return import.meta.env.VITE_AWS_WS_TOKEN;
  try {
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || '';
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
    localStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore */ }
}
