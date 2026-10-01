// §40 MLS SEARCH (alone, or WHILE mls_ingest.py writes) / §41 BUYER MATCHING /
// §50 PAGINATION
//
// Agents search MLS the way MlsSearch.jsx does (GET, 24 per page), page deep
// (pages 1, 10, 50 — offset pagination), filter, and open listings — the
// detail view runs the deterministic buyer match for the listing.
import { sleep } from 'k6';
import { SESSIONS, call, writeSummary } from '../lib/neoh.js';

const VUS = Number(__ENV.VUS || 25);
const DURATION = __ENV.DURATION || '2m';

export const options = {
  scenarios: { search: { executor: 'constant-vus', vus: VUS, duration: DURATION, gracefulStop: '15s' } },
  thresholds: {
    tenant_leaks: ['count==0'],
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:search}': ['p(95)<800'],
    'http_req_duration{kind:deep_page}': ['p(95)<1200'],
    'http_req_duration{kind:detail}': ['p(95)<1500'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

const QUERIES = ['state=TX', 'state=TX&city=Austin', 'state=TX&min_price=300000&max_price=600000',
  'state=TX&beds=3', 'state=TX&zip=78701'];

export default function () {
  const s = SESSIONS[((__VU - 1) * 37) % SESSIONS.length];
  const q = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = call(s, 'GET', `/api/mls/search?${q}`, null, 'GET /api/mls/search (page 1)', { kind: 'search' });
  sleep(1 + Math.random() * 2);
  const page = [10, 50][Math.floor(Math.random() * 2)];
  call(s, 'GET', `/api/mls/search?${q}&page=${page}`, null, `GET /api/mls/search (page ${page})`, { kind: 'deep_page' });
  sleep(1 + Math.random() * 2);
  let ids = [];
  try { ids = (res.json().listings || []).map((l) => l.id || l.listing_id).filter(Boolean); } catch (e) { ids = []; }
  if (ids.length) {
    call(s, 'GET', `/api/mls/listings/${ids[Math.floor(Math.random() * ids.length)]}`, null,
      'GET /api/mls/listings/{id} (+buyer match)', { kind: 'detail' });
  }
  sleep(2 + Math.random() * 3);
}

export function handleSummary(data) { return writeSummary(data, `mls_search-${VUS}vu`); }
