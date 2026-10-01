// §24 CROSS-REPLICA FAN-OUT (PostgreSQL LISTEN/NOTIFY)
//
// Listeners: CONNS sockets spread over every tenant and (by the balancer)
// both API replicas. Writer: RATE events/s, each a PATCH that moves one of a
// tenant's leads to a dossier status — one UPDATE plus a tenant broadcast
// (DOSSIER_MOVED). A listener on the SAME replica as the PATCH is delivered
// locally; one on the OTHER replica only through NOTIFY.
//
// Every send and every receipt is emitted as a sample carrying its wall-clock
// time; fanout_analyze.py joins them to give delivery latency (local vs
// cross-replica), missed deliveries and duplicates. Cycling each tenant's 50
// seeded leads keeps a lead's repeats ≥ 25 s apart at 20 events/s, so the join
// is unambiguous.
import ws from 'k6/ws';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
import { SESSIONS, LEADS, ipFor, call, writeSummary } from '../lib/neoh.js';

const CONNS = Number(__ENV.CONNS || 200);
const RATE = Number(__ENV.RATE || 10);
const DURATION = __ENV.DURATION || '2m';
const ORIGIN = __ENV.ORIGIN || 'http://localhost:5173';
const WS_BASE = (__ENV.TARGET || 'http://oracle-perf-lb:8080').replace(/^http/, 'ws');

const sent = new Counter('fanout_sent');
const recv = new Counter('fanout_recv');
const listener = new Counter('fanout_listener');

export const options = {
  scenarios: {
    listeners: { executor: 'per-vu-iterations', vus: CONNS, iterations: 1, maxDuration: '30m', exec: 'listen' },
    writer: { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 20, maxVUs: 60, startTime: '20s', exec: 'write' },
  },
  thresholds: { tenant_leaks: ['count==0'], checks: ['rate>0.99'] },
};

const TENANTS = Object.keys(LEADS);
const OWNERS = {};
for (const s of SESSIONS) if (s.user.role === 'broker_owner') OWNERS[s.user.tenant_slug] = s;
// Listeners cover every tenant evenly, so each event has several receivers.
const LISTENERS = [];
for (let i = 0; i < CONNS; i++) {
  const tenant = TENANTS[i % TENANTS.length];
  const pool = SESSIONS.filter((s) => s.user.tenant_slug === tenant);
  LISTENERS.push(pool[Math.floor(i / TENANTS.length) % pool.length]);
}
const STATUSES = ['draft', 'marketing', 'assigned'];

export function listen() {
  const s = LISTENERS[exec.vu.idInTest - 1];
  const tenant = s.user.tenant_slug;
  const holdMs = (20 + parseDuration(DURATION) + 20) * 1000;
  const res = ws.connect(`${WS_BASE}/ws`, {
    headers: { Origin: ORIGIN, Cookie: `oracle_session=${s.token}`, 'X-Forwarded-For': ipFor(s) },
    tags: { name: 'WS /ws (fanout)' },
  }, (socket) => {
    socket.on('message', (raw) => {
      if (raw.indexOf('PING') !== -1) { socket.send('{"type":"PONG"}'); return; }
      if (raw.indexOf('DOSSIER_MOVED') === -1) return;
      const m = JSON.parse(raw);
      recv.add(Date.now(), { lead: m.lead_id, tenant, vu: String(exec.vu.idInTest) });
    });
    socket.setTimeout(() => socket.close(1000), holdMs);
  });
  const upstream = res && res.headers ? (res.headers['X-Perf-Upstream'] || '?') : '?';
  // Emitted at the end because the upgrade response (and its replica header)
  // is only returned when the socket closes.
  listener.add(1, { tenant, vu: String(exec.vu.idInTest), replica: upstream });
}

export function write() {
  // Deterministic over the whole test, not per VU: concurrent writer VUs with
  // their own counters picked the same lead milliseconds apart, and a receipt
  // then matched the later send (101 "missed" == 101 "duplicates" — a join
  // artefact). Now a (tenant, lead) pair repeats only every T×50 events.
  const it = exec.scenario.iterationInTest;
  const tenant = TENANTS[it % TENANTS.length];
  const leads = LEADS[tenant];
  const owner = OWNERS[tenant];
  const lead = leads[Math.floor(it / TENANTS.length) % leads.length];
  const status = STATUSES[Math.floor(it / (TENANTS.length * leads.length)) % STATUSES.length];
  const t0 = Date.now();
  const r = call(owner, 'PATCH', `/api/leads/${lead}/dossier-status`, { status },
    'PATCH /api/leads/{id}/dossier-status');
  sent.add(t0, { lead, tenant, replica: r.headers['X-Perf-Upstream'] || '?', ok: String(r.status === 200) });
}

function parseDuration(d) {
  const m = /^(\d+)(s|m)$/.exec(d);
  return m ? Number(m[1]) * (m[2] === 'm' ? 60 : 1) : 120;
}

export function handleSummary(data) { return writeSummary(data, `ws_fanout-${CONNS}c-${RATE}rps`); }
