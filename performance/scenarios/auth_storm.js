// §15 AUTHENTICATION STORM — kept apart from application load on purpose.
//
// VUS agents each log in ONCE, all within BURST seconds (a morning arrival).
//   MODE=distinct  every agent on its own network (its own public IP)
//   MODE=nat       every agent of the 25-agent brokerage behind ONE office IP —
//                  the case a per-IP login limit gets wrong
//
// Measured: login latency (scrypt verification is deliberately CPU-heavy),
// successes, and 429s — split, because a 429 for a legitimate colleague is a
// capacity defect, not a security win.
import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';
import { USERS, BASE, writeSummary } from '../lib/neoh.js';

const VUS = Number(__ENV.VUS || 25);
const BURST = Number(__ENV.BURST || 30);
const MODE = __ENV.MODE || 'distinct';

const ok = new Counter('login_ok');
const limited = new Counter('login_429');
const failed = new Counter('login_failed');
const latency = new Trend('login_ms', true);

export const options = {
  scenarios: { storm: { executor: 'per-vu-iterations', vus: VUS, iterations: 1, maxDuration: `${BURST + 120}s` } },
  // No tenant data is returned by login, but the threshold is declared so the
  // harness's "every scenario checks isolation" rule holds uniformly.
  thresholds: { tenant_leaks: ['count==0'], login_failed: ['count==0'] },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

// MODE=nat draws every login from one brokerage; distinct spreads across all.
const POOL = MODE === 'nat'
  ? USERS.filter((u) => u.tenant_slug === 'perf-brokerage-01')
  : USERS;

export default function () {
  const i = exec.vu.idInTest - 1;
  const u = POOL[i % POOL.length];
  const ip = MODE === 'nat' ? '198.51.100.7' : `203.0.113.${(i % 250) + 1}`;
  sleep(Math.random() * BURST);
  const t0 = Date.now();
  const res = http.post(`${BASE}/auth/login`, JSON.stringify({ agent_id: u.agent_id, passphrase: u.password }),
    { headers: { 'Content-Type': 'application/json', 'X-Forwarded-For': ip }, tags: { name: 'POST /auth/login' } });
  latency.add(Date.now() - t0);
  if (res.status === 200) ok.add(1);
  else if (res.status === 429) limited.add(1);
  else failed.add(1, { status: String(res.status) });
}

export function handleSummary(data) { return writeSummary(data, `auth_storm-${MODE}-${VUS}`); }
