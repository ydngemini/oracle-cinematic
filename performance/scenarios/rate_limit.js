// §16 DISTRIBUTED RATE LIMITING across API replicas.
//
// hammer:    ONE signed-in agent sends RATE req/s for DURATION through the
//            balancer, so requests alternate between both replicas. The
//            authenticated allowance is 600/min per principal; if the limit
//            were per replica, ~1,200/min would get through.
// colleague: a DIFFERENT agent behind the SAME office IP, at a human 1 req/s.
//            It must never be throttled — limits are per principal, not per IP.
//
// fanout_analyze-style verdict is computed from counters: allowed per minute
// for the hammer, and 429s for the colleague (must be 0).
import { Counter } from 'k6/metrics';
import { SESSIONS, call, writeSummary } from '../lib/neoh.js';

const RATE = Number(__ENV.RATE || 25);
const DURATION = __ENV.DURATION || '130s';
const OFFICE_IP = '198.51.100.20';

const hammerOk = new Counter('rl_hammer_allowed');
const hammer429 = new Counter('rl_hammer_429');
const colleague429 = new Counter('rl_colleague_429');
const colleagueOk = new Counter('rl_colleague_allowed');

export const options = {
  scenarios: {
    hammer: { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 20, maxVUs: 50, exec: 'hammer' },
    colleague: { executor: 'constant-arrival-rate', rate: 1, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 2, exec: 'colleague' },
  },
  thresholds: { tenant_leaks: ['count==0'], rl_colleague_429: ['count==0'] },
};

const A = Object.assign({}, SESSIONS[5], { ip: OFFICE_IP });
const B = Object.assign({}, SESSIONS[6], { ip: OFFICE_IP });

export function hammer() {
  const r = call(A, 'GET', '/api/crm/contacts?limit=5', null, 'GET /api/crm/contacts (hammer)');
  if (r.status === 429) hammer429.add(1); else if (r.status < 300) hammerOk.add(1);
}

export function colleague() {
  const r = call(B, 'GET', '/api/crm/contacts?limit=5', null, 'GET /api/crm/contacts (colleague)');
  if (r.status === 429) colleague429.add(1); else if (r.status < 300) colleagueOk.add(1);
}

export function handleSummary(data) { return writeSummary(data, `rate_limit-${RATE}rps`); }
