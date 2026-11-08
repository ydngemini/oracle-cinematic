// Authenticated REST calls to the Neoh backend's /api/aws/* surface. Same-origin
// on obs.neohrs.com (ALB routes /api/* to the backend), so a Bearer header is all
// that's needed — no CORS. Token comes from the same place the WS uses.
// Explicit extensions and `import.meta.env?.`: Node's own test runner loads
// this module as it is (no Vite), as src/lib/copilot.test.js does.
import { resolveToken } from './auth.js';
import { copilotError } from './copilotError.js';

const API_BASE = (import.meta.env?.VITE_API_BASE || '').replace(/\/+$/, '');

function authHeaders(extra = {}) {
  const token = resolveToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function authedGet(path) {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return res.json();
}

// Real LLM copilot (Bedrock, server-side). Returns { reply, model } or throws
// an Error carrying the status and the server's detail (copilotError), so a
// budget refusal can be told apart from an unreachable model.
export async function fetchCopilotReply(message, context) {
  const res = await fetch(`${API_BASE}/api/aws/copilot/query`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ message, context }),
  });
  if (!res.ok) throw await copilotError(res);
  return res.json();
}

// Real historical CloudWatch series for one resource. type: ec2|rds|lambda.
export function fetchMetricHistory(type, id, range = '1h') {
  const q = new URLSearchParams({ type, id, range }).toString();
  return authedGet(`/api/aws/metrics/history?${q}`);
}

// Latest detailed CloudWatch metrics for one resource. type: ec2|rds|lambda|ebs|s3.
export function fetchResourceMetrics(type, id) {
  return authedGet(`/api/aws/${type}/${encodeURIComponent(id)}/metrics`);
}
