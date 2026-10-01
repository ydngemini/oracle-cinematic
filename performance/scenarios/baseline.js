// §9 BASELINE — one user, no concurrency, every hot endpoint, per brokerage
// size. You cannot diagnose load degradation without the unloaded number, and
// a latency that already scales with tenant size at ZERO load is a data-shape
// problem, not a capacity problem — the two must not be confused later.
import { sessionsByTenant, call, writeSummary } from '../lib/neoh.js';

export const options = {
  scenarios: { baseline: { executor: 'per-vu-iterations', vus: 1, iterations: 25, maxDuration: '10m' } },
  // A run where requests fail must FAIL. The first version had only the leak
  // threshold and reported "PASSED" with 86% of its requests failing.
  thresholds: { tenant_leaks: ['count==0'], checks: ['rate>0.99'], http_req_failed: ['rate<0.01'] },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export function setup() {
  // One owner per brokerage SHAPE: solo, small, brokerage, large.
  const shapes = {};
  for (const list of Object.values(sessionsByTenant())) {
    const owner = list.find((s) => s.user.role === 'broker_owner');
    if (owner && !shapes[owner.shape]) shapes[owner.shape] = owner;
  }
  return Object.values(shapes);
}

export default function (sessions) {
  for (const s of sessions) {
    const t = `[${s.shape}]`;
    call(s, 'GET', '/version', null, `GET /version ${t}`);
    call(s, 'GET', '/api/command-center', null, `GET /api/command-center ${t}`);
    const list = call(s, 'GET', '/api/crm/clients', null, `GET /api/crm/clients (list) ${t}`);
    call(s, 'GET', '/api/crm/clients?q=Avery', null, `GET /api/crm/clients?q ${t}`);
    let first = null;
    try { const b = list.json(); first = (b.clients || b.items || b)[0]; } catch (e) { first = null; }
    if (first && first.id) call(s, 'GET', `/api/crm/clients/${first.id}`, null, `GET /api/crm/clients/{id} ${t}`);
    call(s, 'GET', '/api/crm/contacts?limit=50', null, `GET /api/crm/contacts ${t}`);
    call(s, 'GET', '/api/mls/regions', null, `GET /api/mls/regions ${t}`);
    // GET is what the UI calls (MlsSearch.jsx); POST is the documented API.
    call(s, 'GET', '/api/mls/search?state=TX', null, `GET /api/mls/search ${t}`);
    call(s, 'POST', '/api/mls/search', { state_codes: ['TX'], limit: 25 }, `POST /api/mls/search ${t}`);
  }
}

export function handleSummary(data) { return writeSummary(data, 'baseline'); }
