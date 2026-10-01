// Shared k6 helpers for Neoh load scenarios.
//
// The one non-negotiable: EVERY response body is checked for another tenant's
// sentinel. A leak fails the run immediately (abort), because performance is
// irrelevant if isolation fails — and an isolation check that only runs in one
// dedicated scenario proves isolation only for that scenario's requests.

import http from 'k6/http';
import { check, fail } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';
import encoding from 'k6/encoding';

export const BASE = __ENV.TARGET || 'http://oracle-perf-lb:8080';

export const leaks = new Counter('tenant_leaks');
export const replicaHits = new Counter('replica_hits');
export const payloadBytes = new Trend('payload_bytes');

const fixture = JSON.parse(open(__ENV.USERS_FILE || '/out/users.json'));
export const USERS = fixture.users;
const ALL_SENTINELS = [...new Set(USERS.map((u) => u.sentinel))];

// Established sessions (fixtures/sessions.py) — how real users arrive: already
// logged in. Login capacity is measured separately, in auth_storm.js.
export const SESSIONS = JSON.parse(open(__ENV.SESSIONS_FILE || '/out/sessions.json')).sessions
  .map((s) => ({ token: s.token, user: s, shape: s.shape }));

// Minted sessions are 24 h JWTs. Expired ones are not an error k6 can see: every
// socket is closed at auth, no turn ever starts, and a scenario whose thresholds
// are all "count==0" or latency percentiles PASSES with zero work done (Mission
// 8 close-out: 41,519 empty sessions, verdict PASS). Refuse instead, with enough
// margin for a 30-minute soak.
(() => {
  const marginS = Number(__ENV.SESSION_MARGIN_S || 3600);
  const exps = SESSIONS.map((s) => {
    try { return JSON.parse(encoding.b64decode(s.token.split('.')[1], 'rawurl', 's')).exp; } catch (e) { return undefined; }
  }).filter((e) => typeof e === 'number');
  if (!exps.length) return;
  const left = Math.min(...exps) - Math.floor(Date.now() / 1000);
  if (left < marginS) {
    throw new Error(`sessions.json ${left <= 0 ? 'expired' : `expires in ${left}s`} — re-mint: `
      + 'docker exec -i oracle-sypher-docker sh -s < performance/fixtures/mint.sh');
  }
})();

// Seeded lead ids per tenant (fixtures/seed.py write_lead_ids).
export const LEADS = (() => { try { return JSON.parse(open(__ENV.LEADS_FILE || '/out/leads.json')); } catch (e) { return {}; } })();

export function sessionsByTenant() {
  const out = {};
  for (const s of SESSIONS) (out[s.user.tenant_slug] = out[s.user.tenant_slug] || []).push(s);
  return out;
}

// Users grouped by tenant, so scenarios can choose "one per tenant" or "many
// from one tenant" (§13 — both shapes, because they exercise different paths).
export function usersByTenant() {
  const out = {};
  for (const u of USERS) (out[u.tenant_slug] = out[u.tenant_slug] || []).push(u);
  return out;
}

export function login(user) {
  const res = http.post(`${BASE}/auth/login`,
    JSON.stringify({ agent_id: user.agent_id, passphrase: user.password }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /auth/login' } });
  if (res.status !== 200) {
    fail(`login failed for ${user.agent_id}: ${res.status} ${String(res.body).slice(0, 160)}`);
  }
  const body = res.json();
  if (body.policy_acceptance_required) {
    fail(`${user.agent_id} still needs policy acceptance — the seed should have recorded it`);
  }
  const cookie = (res.cookies.oracle_session || [])[0];
  return { token: cookie ? cookie.value : null, user };
}

function csrfFor(session) {
  if (session.csrf) return session.csrf;
  // Read it from the response itself: the jar will not report a Secure cookie
  // for an http URL.
  const prime = http.get(`${BASE}/auth/policy-acceptance`, {
    cookies: { oracle_session: session.token },
    tags: { name: 'GET /auth/policy-acceptance (csrf prime)' },
  });
  const c = (prime.cookies.csrf_token || [])[0];
  session.csrf = c ? c.value : null;
  return session.csrf;
}

// Tenant isolation on EVERY response: no sentinel but this user's own may
// appear. Aborts the whole test on the first leak.
function assertIsolated(res, session, name) {
  const body = String(res.body || '');
  if (body.indexOf('SNTL') === -1) return;
  for (const s of ALL_SENTINELS) {
    if (s !== session.user.sentinel && body.indexOf(s) !== -1) {
      leaks.add(1, { endpoint: name });
      exec.test.abort(`TENANT LEAK: ${session.user.tenant_slug} received ${s} from ${name}`);
    }
  }
}

// A stable public address per signed-in user (TEST-NET-3, RFC 5737 — never
// routable), sent as X-Forwarded-For so per-IP limits see many networks, as
// production does. Scenarios modelling an office NAT pass their own `ip`.
export function ipFor(session) {
  let h = 0;
  for (const ch of session.user.agent_id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `203.0.113.${h % 254 + 1}`;
}

export function call(session, method, path, body, name, extraTags) {
  // The session travels as the HttpOnly cookie, exactly as a browser sends it.
  // (Login returns token:null in its body; the first harness sent "Bearer null"
  // and was silently anonymous — every call 429'd on the per-IP anonymous limit.)
  const params = {
    cookies: { oracle_session: session.token },
    headers: { 'Content-Type': 'application/json', 'X-Forwarded-For': session.ip || ipFor(session) },
    tags: Object.assign({ name: name || `${method} ${path.split('?')[0]}` }, extraTags || {}),
  };
  if (method !== 'GET') {
    const csrf = csrfFor(session);
    if (csrf) {
      // Double-submit: the header AND the cookie. The cookie is Secure, and
      // k6's jar (correctly) will not send a Secure cookie over plain http to
      // the local balancer, so it is supplied explicitly — as the golden E2E
      // does for the same reason.
      params.headers['X-CSRF-Token'] = csrf;
      params.cookies.csrf_token = csrf;
    }
  }
  const res = http.request(method, `${BASE}${path}`, body ? JSON.stringify(body) : null, params);
  const upstream = res.headers['X-Perf-Upstream'];
  if (upstream) replicaHits.add(1, { replica: upstream });
  payloadBytes.add(String(res.body || '').length, { name: params.tags.name });
  assertIsolated(res, session, params.tags.name);
  check(res, { [`${params.tags.name} 2xx`]: (r) => r.status >= 200 && r.status < 300 });
  return res;
}

// A result artifact per run (§70): environment, SHA, scenario, profile and the
// threshold verdicts, next to k6's own metrics. No secrets — tokens and
// passwords never enter it.
export function writeSummary(data, scenario) {
  const meta = {
    scenario,
    target: BASE,
    git_sha: __ENV.GIT_SHA || 'unknown',
    data_profile: fixture.profile,
    tenants: Object.keys(usersByTenant()).length,
    users: USERS.length,
    generated_at: new Date().toISOString(),
    thresholds_passed: Object.values(data.metrics).every(
      (m) => !m.thresholds || Object.values(m.thresholds).every((t) => t.ok !== false)),
  };
  const out = `/out/results/${scenario}-${meta.generated_at.replace(/[:.]/g, '')}.json`;
  return {
    [out]: JSON.stringify({
      meta,
      checks: (data.root_group.checks || []).map((c) => ({ name: c.name, passes: c.passes, fails: c.fails })),
      metrics: data.metrics,
    }, null, 1),
    stdout: `\n${scenario}: thresholds ${meta.thresholds_passed ? 'PASSED' : 'FAILED'} → ${out}\n`,
  };
}
