// Authenticated REST calls to the Neoh backend's /api/aws/* surface. Same-origin
// on obs.neoh.app (ALB routes /api/* to the backend), so a Bearer header is all
// that's needed — no CORS. Token comes from the same place the WS uses.
import { resolveToken } from './auth';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function authedGet(path) {
  const token = resolveToken();
  const res = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return res.json();
}

// Real historical CloudWatch series for one resource. type: ec2|rds|lambda.
export function fetchMetricHistory(type, id, range = '1h') {
  const q = new URLSearchParams({ type, id, range }).toString();
  return authedGet(`/api/aws/metrics/history?${q}`);
}
