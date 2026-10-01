// §47 RECONSTRUCTION QUEUE — submissions land on the API replicas (web role,
// no consumer); the single worker must claim every one of them from the
// database. Before Mission 8 they sat in the API replica's memory forever.
//
// Uses whatever provider the target is configured with; the perf topology has
// no GPU credentials, so jobs run the local stub/fail fast — this measures the
// QUEUE (submit → claimed → terminal), not GPU time.
import http from 'k6/http';
import { sleep, check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { SESSIONS, LEADS, call, writeSummary } from '../lib/neoh.js';

const JOBS = Number(__ENV.VUS || 6);
const claimed = new Counter('recon_claimed');
const stuck = new Counter('recon_stuck_queued');
const pickup = new Trend('recon_pickup_ms', true);

export const options = {
  scenarios: { recon: { executor: 'per-vu-iterations', vus: 1, iterations: 1, maxDuration: '5m' } },
  // Every submission must be accepted AND claimed; a run that submits nothing
  // must fail, not pass vacuously.
  thresholds: { tenant_leaks: ['count==0'], recon_stuck_queued: ['count==0'],
                checks: ['rate==1'], recon_claimed: [`count>=${JOBS}`] },
};

export default function () {
  // One owner per tenant, so the per-tenant backlog bound is not what we hit.
  const owners = [];
  const seen = {};
  for (const s of SESSIONS) {
    if (s.user.role === 'broker_owner' && !seen[s.user.tenant_slug]) { seen[s.user.tenant_slug] = 1; owners.push(s); }
  }
  const jobs = [];
  for (let i = 0; i < JOBS; i++) {
    const s = owners[i % owners.length];
    const leadId = (LEADS[s.user.tenant_slug] || [])[i];
    if (!leadId) continue;
    const r = call(s, 'POST', `/api/crm/reconstruction-jobs?lead_id=${leadId}`, null, 'POST /api/crm/reconstruction-jobs');
    if (r.status === 202) jobs.push({ s, id: r.json().job_id, t0: Date.now() });
  }
  check(jobs, { 'every submission accepted': (j) => j.length === JOBS });

  const deadline = Date.now() + 120000;
  const pending = new Set(jobs.map((j) => j.id));
  while (pending.size && Date.now() < deadline) {
    for (const j of jobs) {
      if (!pending.has(j.id)) continue;
      const st = call(j.s, 'GET', `/api/crm/reconstruction-jobs/${j.id}`, null, 'GET /api/crm/reconstruction-jobs/{id}');
      let status = null;
      try { status = st.json().status; } catch (e) { status = null; }
      if (status && status !== 'queued') { pending.delete(j.id); claimed.add(1); pickup.add(Date.now() - j.t0); }
    }
    sleep(1);
  }
  stuck.add(pending.size);
}

export function handleSummary(data) { return writeSummary(data, 'recon_queue'); }
