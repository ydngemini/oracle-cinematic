// §26 NEOH TEXT CONVERSATIONS / §28 AI CONCURRENCY
//
// Each VU is an agent in a conversation with Neoh over the real path: the /ws
// socket, AI_CHAT_SEND, the durable ai_chat:response job on the WORKER, the
// model call, then AI_CHAT_START / DELTA / COMPLETE back over the socket.
//
// The model is the local provider mock by default (MOCK_LLM_LATENCY_MS ≈ a
// real completion) — this measures Neoh's infrastructure, not a provider's
// bill. Real-provider runs go through ai_real_sample.js with a hard cap.
//
//   VUS=10 DURATION=3m THINK_MIN=10 THINK_MAX=20
//
// Measured per turn: admission (SEND → ACCEPTED), time to first token
// (SEND → first DELTA), completion (SEND → COMPLETE), and rejections by code.
import ws from 'k6/ws';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { SESSIONS, ipFor, writeSummary } from '../lib/neoh.js';

const VUS = Number(__ENV.VUS || 10);
const DURATION = __ENV.DURATION || '3m';
const THINK_MIN = Number(__ENV.THINK_MIN || 10);
const THINK_MAX = Number(__ENV.THINK_MAX || 20);
const TURN_TIMEOUT = Number(__ENV.TURN_TIMEOUT || 90);
const ORIGIN = __ENV.ORIGIN || 'http://localhost:5173';
const WS_BASE = (__ENV.TARGET || 'http://oracle-perf-lb:8080').replace(/^http/, 'ws');

const admit = new Trend('ai_admit_ms', true);
const ttft = new Trend('ai_first_token_ms', true);
const complete = new Trend('ai_complete_ms', true);
const turns = new Counter('ai_turns_completed');
const rejected = new Counter('ai_rejected');
const errored = new Counter('ai_errored');
const timedOut = new Counter('ai_turn_timeout');

export const options = {
  scenarios: { chat: { executor: 'constant-vus', vus: VUS, duration: DURATION, gracefulStop: `${TURN_TIMEOUT + 10}s` } },
  thresholds: {
    tenant_leaks: ['count==0'],
    ai_errored: ['count==0'],
    ai_turn_timeout: ['count==0'],
    ai_admit_ms: ['p(95)<1000'],
    // A fast ACCEPTED proves nothing if the turn then waits for a job slot:
    // the 50-VU run passed admission at 14 ms while first token reached 13 s.
    ai_first_token_ms: ['p(95)<10000'],
    // Liveness: percentiles over zero samples are 0 and pass.
    ai_turns_completed: ['count>0'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

function uuid4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const PROMPTS = [
  'Which of my buyers are most active this week?',
  'Summarize my pipeline and what needs attention today.',
  'Draft a short follow-up for my newest seller lead.',
  'What changed in my market since last week?',
];

export default function () {
  // Distinct agents per VU: the per-agent limits (2 active, 20/min) are real
  // and apply per person, as they would in an office.
  const s = SESSIONS[((__VU - 1) * 37) % SESSIONS.length];
  const holdMs = 1000 * 60 * 30;
  ws.connect(`${WS_BASE}/ws`, {
    headers: { Origin: ORIGIN, Cookie: `oracle_session=${s.token}`, 'X-Forwarded-For': ipFor(s) },
    tags: { name: 'WS /ws (ai_chat)' },
  }, (socket) => {
    // One turn at a time. k6's socket.setTimeout cannot be cancelled, so each
    // timer carries the turn number it belongs to and does nothing if that
    // turn already finished — otherwise a completed turn's deadline still
    // fired, counted a phantom timeout and started a second send loop.
    let t0 = 0; let rid = null; let gotFirst = false; let turn = 0;
    function send(forTurn) {
      if (forTurn !== turn) return;
      rid = uuid4(); gotFirst = false; t0 = Date.now();
      socket.send(JSON.stringify({ type: 'AI_CHAT_SEND', version: 1, request_id: rid,
        content: PROMPTS[Math.floor(Math.random() * PROMPTS.length)], context: null, attachment_ids: [] }));
      const mine = turn;
      socket.setTimeout(() => { if (turn === mine && rid) { timedOut.add(1); next(); } }, TURN_TIMEOUT * 1000);
    }
    function next() {
      rid = null; turn += 1;
      const mine = turn;
      socket.setTimeout(() => send(mine), 1000 * (THINK_MIN + Math.random() * (THINK_MAX - THINK_MIN)));
    }
    socket.on('open', () => socket.setTimeout(() => send(0), Math.random() * THINK_MAX * 1000));
    socket.on('message', (raw) => {
      if (raw.indexOf('"PING"') !== -1) { socket.send('{"type":"PONG"}'); return; }
      if (raw.indexOf('AI_CHAT_') === -1) return;
      const m = JSON.parse(raw);
      if (!rid || (m.request_id && m.request_id !== rid)) return;
      if (m.type === 'AI_CHAT_ACCEPTED' && m.request_id === rid) admit.add(Date.now() - t0);
      else if (m.type === 'AI_CHAT_REJECTED' && m.request_id === rid) {
        rejected.add(1, { code: m.code || '?' }); next();
      } else if (m.type === 'AI_CHAT_DELTA' && !gotFirst) {
        gotFirst = true; ttft.add(Date.now() - t0);
      } else if (m.type === 'AI_CHAT_COMPLETE') {
        complete.add(Date.now() - t0); turns.add(1); next();
      } else if (m.type === 'AI_CHAT_ERROR') {
        errored.add(1, { code: m.code || '?' }); next();
      }
    });
    socket.setTimeout(() => socket.close(1000), holdMs);
  });
}

export function handleSummary(data) { return writeSummary(data, `ai_chat-${VUS}vu`); }
