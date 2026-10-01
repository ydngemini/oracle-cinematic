// §42 MESSAGING / §43 TELEPHONY / §44 STRIPE WEBHOOK BURSTS
//
// Replays fixtures/webhooks.py's pre-signed deliveries — including provider
// retries (the "-dup" kinds) — at RATE/s, shuffled, so duplicates can land on
// different replicas at nearly the same moment: the hard idempotency case.
// Correctness (one row per unique message/subscription/call) is verified in
// the database afterwards by fixtures/verify_webhooks.sql.
//
// Providers retry on slow or failed webhooks; the latency thresholds are the
// point at which a real provider would start retrying and amplifying load.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
import { BASE, writeSummary } from '../lib/neoh.js';

const RATE = Number(__ENV.RATE || 50);
const fx = JSON.parse(open(__ENV.WEBHOOKS_FILE || '/out/webhooks.json'));
const REQS = fx.requests;
// Deterministic shuffle so a duplicate is not always right behind its original
// — except call status callbacks, which follow their call's answer webhook in
// real life (a shuffled 'completed' before its 'answer' only measured a 404).
const hash = (i) => (i * 2654435761) % 1000003;
const isStatus = (i) => REQS[i].kind === 'plivo-status';
const ORDER = REQS.map((_, i) => i).filter((i) => !isStatus(i)).sort((a, b) => hash(a) - hash(b))
  .concat(REQS.map((_, i) => i).filter(isStatus));
const bad = new Counter('webhook_rejected');

export const options = {
  scenarios: { hooks: { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s',
    duration: `${Math.ceil(REQS.length / RATE)}s`, preAllocatedVUs: 50, maxVUs: 200 } },
  thresholds: {
    tenant_leaks: ['count==0'],
    webhook_rejected: ['count==0'],
    'http_req_duration{kind:telnyx-inbound}': ['p(95)<500'],
    'http_req_duration{kind:stripe}': ['p(95)<500'],
    'http_req_duration{kind:plivo-answer}': ['p(95)<1000'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export default function () {
  const it = exec.scenario.iterationInTest;
  if (it >= REQS.length) return;
  const r = REQS[ORDER[it]];
  const kind = r.kind.replace('-dup', '');
  const res = http.request(r.method, `${BASE}${r.path}`, r.body,
    { headers: r.headers, tags: { name: `webhook ${r.kind}`, kind } });
  // Any 4xx/5xx is a delivery the provider would retry — including a
  // duplicate, which must be ACCEPTED (2xx) and ignored, not refused.
  if (res.status >= 400) bad.add(1, { kind: r.kind, status: String(res.status) });
}

export function handleSummary(data) { return writeSummary(data, `webhook_burst-${RATE}rps`); }
