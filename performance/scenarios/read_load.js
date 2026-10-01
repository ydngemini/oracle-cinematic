// §10/§13 CONCURRENT READS — N signed-in agents from ALL tenants at once, each
// working the way a person does: open the Work list, open a client, glance at
// contacts or MLS, then think. Many VUs share a tenant (the 100-agent
// brokerage contributes most of them), so this exercises both "many tenants"
// and "many users of one tenant" in the same run.
//
// Run at a constant concurrency level per invocation (VUS=25|75|169 …) rather
// than one ramp, so each level yields its own clean latency distribution.
import { sleep } from 'k6';
import { SESSIONS, call, writeSummary } from '../lib/neoh.js';

const VUS = Number(__ENV.VUS || 25);
const DURATION = __ENV.DURATION || '2m';
const THINK_MIN = Number(__ENV.THINK_MIN || 1);
const THINK_MAX = Number(__ENV.THINK_MAX || 4);

export const options = {
  scenarios: { reads: { executor: 'constant-vus', vus: VUS, duration: DURATION, gracefulStop: '15s' } },
  // Thresholds mirror docs/performance-targets.md (interactive reads).
  thresholds: {
    tenant_leaks: ['count==0'],
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    'http_req_duration{kind:worklist}': ['p(95)<800'],
    'http_req_duration{kind:list}': ['p(95)<500'],
    'http_req_duration{kind:item}': ['p(95)<300'],
    'http_req_duration{kind:search}': ['p(95)<800'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

function think() { sleep(THINK_MIN + Math.random() * (THINK_MAX - THINK_MIN)); }

function tagged(s, method, path, body, name, kind) {
  // `kind` groups endpoints for thresholds; `name` keeps per-endpoint detail.
  return call(s, method, path, body, name, { kind });
}

let started = false;

export default function () {
  // Real people do not all click at the same millisecond. Without this, every
  // VU's first Work-list request lands together and the first 10 s measure a
  // burst (p50 1,024 ms at 25 VUs) rather than the concurrency level. The
  // burst is real and is measured on purpose in spike.js.
  if (!started) { started = true; sleep(Math.random() * THINK_MAX * 2); }
  // Stable VU → session mapping: each VU owns one user, so per-principal rate
  // limits apply exactly as they would to real people. Sessions are ordered
  // solo…large; a stride coprime to their count spreads any VU level across
  // every tenant size (in plain order, 25 VUs never reached the large tenant).
  const s = SESSIONS[((__VU - 1) * 37) % SESSIONS.length];
  const shape = `[${s.shape}]`;

  const list = tagged(s, 'GET', '/api/crm/clients', null, `GET /api/crm/clients (list) ${shape}`, 'worklist');
  think();

  let ids = [];
  try { ids = (list.json().clients || []).map((c) => c.id).filter(Boolean); } catch (e) { ids = []; }
  if (ids.length) {
    const id = ids[Math.floor(Math.random() * Math.min(ids.length, 50))];
    tagged(s, 'GET', `/api/crm/clients/${id}`, null, `GET /api/crm/clients/{id} ${shape}`, 'item');
    think();
  }

  const r = Math.random();
  if (r < 0.35) {
    tagged(s, 'GET', '/api/command-center', null, `GET /api/command-center ${shape}`, 'list');
  } else if (r < 0.6) {
    tagged(s, 'GET', '/api/crm/contacts?limit=50', null, `GET /api/crm/contacts ${shape}`, 'list');
  } else if (r < 0.85) {
    tagged(s, 'GET', '/api/mls/search?state=TX', null, `GET /api/mls/search ${shape}`, 'search');
  } else {
    tagged(s, 'GET', '/api/crm/clients?q=Avery', null, `GET /api/crm/clients?q ${shape}`, 'search');
  }
  think();
}

export function handleSummary(data) { return writeSummary(data, `read_load-${VUS}vu`); }
