// §12 CONTROLLED WRITES — correctness, not only latency.
//
// Each iteration: create a client, read it back, move its pipeline stage,
// add a note, create a task — and after every write, READ IT BACK through the
// API and compare. A write that returns 2xx but does not persist (or persists
// into the wrong tenant) counts as write_mismatch and fails the run.
//
// Created rows carry the tenant's sentinel, so the isolation check on every
// response also covers the rows this scenario itself wrote. They live only in
// perf- tenants; `seed.py --cleanup` removes them with the tenant.
import { sleep } from 'k6';
import { Counter } from 'k6/metrics';
import { SESSIONS, call, writeSummary } from '../lib/neoh.js';

const VUS = Number(__ENV.VUS || 20);
const DURATION = __ENV.DURATION || '2m';
const mismatch = new Counter('write_mismatch');
const writes = new Counter('writes_verified');

export const options = {
  scenarios: { writes: { executor: 'constant-vus', vus: VUS, duration: DURATION, gracefulStop: '20s' } },
  thresholds: {
    tenant_leaks: ['count==0'],
    write_mismatch: ['count==0'],
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:write}': ['p(95)<800'],
    'http_req_duration{kind:readback}': ['p(95)<500'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

function j(res) { try { return res.json(); } catch (e) { return null; } }
function bad(what) { mismatch.add(1, { what }); }

export default function () {
  const s = SESSIONS[((__VU - 1) * 37) % SESSIONS.length];
  const tag = `${s.user.sentinel} w${__VU}-${__ITER}-${Date.now()}`;

  const created = j(call(s, 'POST', '/api/crm/clients', { full_name: `Perf Write ${tag}`, client_type: 'buyer' },
    'POST /api/crm/clients', { kind: 'write' }));
  const id = created && (created.id || (created.client && created.client.id));
  if (!id) { bad('create'); sleep(2); return; }
  const back = j(call(s, 'GET', `/api/crm/clients/${id}`, null, 'GET /api/crm/clients/{id}', { kind: 'readback' }));
  const name = back && (back.full_name || (back.client && back.client.full_name));
  if (name !== `Perf Write ${tag}`) bad('create-readback'); else writes.add(1);
  sleep(1);

  call(s, 'PATCH', `/api/crm/clients/${id}`, { stage: 'active' }, 'PATCH /api/crm/clients/{id}', { kind: 'write' });
  const after = j(call(s, 'GET', `/api/crm/clients/${id}`, null, 'GET /api/crm/clients/{id}', { kind: 'readback' }));
  const stage = after && (after.stage || (after.client && after.client.stage));
  if (stage !== 'active') bad('stage'); else writes.add(1);
  sleep(1);

  const noteBody = `note ${tag}`;
  call(s, 'POST', `/api/crm/clients/${id}/notes`, { body: noteBody }, 'POST /api/crm/clients/{id}/notes', { kind: 'write' });
  const notes = call(s, 'GET', `/api/crm/clients/${id}/notes`, null, 'GET /api/crm/clients/{id}/notes', { kind: 'readback' });
  if (String(notes.body).indexOf(noteBody) === -1) bad('note'); else writes.add(1);
  sleep(1);

  const title = `task ${tag}`;
  const task = call(s, 'POST', `/api/crm/clients/${id}/tasks`, { title }, 'POST /api/crm/clients/{id}/tasks', { kind: 'write' });
  if (task.status >= 300 || String(task.body).indexOf(title) === -1) bad('task'); else writes.add(1);
  sleep(2 + Math.random() * 3);
}

export function handleSummary(data) { return writeSummary(data, `write_load-${VUS}vu`); }
