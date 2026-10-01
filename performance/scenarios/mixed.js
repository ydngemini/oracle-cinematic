// §65 FIRST 10 BROKERAGES (and §58 soak / §57 burst when run longer or shaped)
//
// Everything a live pilot does at once, from the SAME scenario code the
// single-purpose tests use, so each part is measured the same way:
//
//   browse   BROWSE agents working the CRM and MLS (read_load.js)
//   write    WRITE agents creating/updating clients, notes, tasks (write_load.js)
//   sockets  SOCKETS open app tabs holding /ws for the whole run (ws_load.js)
//   chat     CHAT agents in Neoh text conversations (ai_chat.js, mock model)
//
// first10.sh runs voice calls, a paced MLS delta sync and background jobs
// beside it. Defaults are the first-10-brokerages profile in
// docs/capacity-plan.md: 25 browsing, 3 writing, 50 tabs, 10 chatting.
//
//   DURATION=15m  (soak: DURATION=30m or more)
import { sleep } from 'k6';
import browseFn from './read_load.js';
import writeFn from './write_load.js';
import chatFn from './ai_chat.js';
import socketFn from './ws_load.js';
import { writeSummary } from '../lib/neoh.js';

const DURATION = __ENV.DURATION || '15m';
const BROWSE = Number(__ENV.BROWSE || 25);
const WRITE = Number(__ENV.WRITE || 3);
const SOCKETS = Number(__ENV.SOCKETS || 50);
const CHAT = Number(__ENV.CHAT || 10);

export const options = {
  scenarios: {
    browse: { executor: 'constant-vus', vus: BROWSE, duration: DURATION, exec: 'browse', gracefulStop: '20s' },
    write: { executor: 'constant-vus', vus: WRITE, duration: DURATION, exec: 'write', gracefulStop: '20s' },
    sockets: { executor: 'constant-vus', vus: SOCKETS, duration: DURATION, exec: 'sockets', gracefulStop: '70s' },
    chat: { executor: 'constant-vus', vus: CHAT, duration: DURATION, exec: 'chat', gracefulStop: '100s' },
  },
  thresholds: {
    tenant_leaks: ['count==0'],
    write_mismatch: ['count==0'],
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:worklist}': ['p(95)<800'],
    'http_req_duration{kind:list}': ['p(95)<500'],
    'http_req_duration{kind:item}': ['p(95)<300'],
    'http_req_duration{kind:search}': ['p(95)<800'],
    'http_req_duration{kind:write}': ['p(95)<800'],
    ws_abnormal_close: ['count==0'],
    ai_errored: ['count==0'],
    ai_turn_timeout: ['count==0'],
    ai_first_token_ms: ['p(95)<10000'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export function browse() { browseFn(); }
export function write() { writeFn(); }
export function chat() { chatFn(); }
// ws_load holds each socket for HOLD seconds (default 60) and then the VU
// reconnects — a tab's socket churn over a long session, not one connect.
export function sockets() { socketFn(); sleep(1); }

export function handleSummary(data) { return writeSummary(data, `mixed-${DURATION}`); }
