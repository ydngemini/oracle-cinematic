// §23 WEBSOCKET CAPACITY / §25 RECONNECT STORM
//
// Each VU is one browser tab: it opens /ws with the session cookie and an
// allowed Origin (exactly what useOracleWebSocket.js sends), waits for the
// server's first frame (session restore), answers PING with PONG, and holds
// the socket for HOLD seconds.
//
//   CONNS=250 HOLD=120 RAMP=60   ramp to 250 sockets over 60 s, hold 2 min
//   STORM=1                      every socket opens in the same second — a
//                                replica restart dropping all its clients
//                                (the frontend reconnect had no jitter)
//
// Measured: connect latency, time to first server frame, sockets refused at
// capacity (close 1013), abnormal closes, and the socket count the server
// reached. The per-replica cap ORACLE_WS_MAX_CONNECTIONS is what refuses.
import ws from 'k6/ws';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { SESSIONS, ipFor, writeSummary } from '../lib/neoh.js';

const CONNS = Number(__ENV.CONNS || 100);
const HOLD = Number(__ENV.HOLD || 60);
const RAMP = Number(__ENV.RAMP || 30);
const STORM = __ENV.STORM === '1';
const ORIGIN = __ENV.ORIGIN || 'http://localhost:5173';
const WS_BASE = (__ENV.TARGET || 'http://oracle-perf-lb:8080').replace(/^http/, 'ws');

const connectMs = new Trend('ws_connect_ms', true);
const firstFrameMs = new Trend('ws_first_frame_ms', true);
const refused = new Counter('ws_refused_capacity');
const abnormal = new Counter('ws_abnormal_close');
const opened = new Counter('ws_opened');
const pings = new Counter('ws_pings');
const noFirstFrame = new Counter('ws_no_first_frame');

export const options = {
  scenarios: {
    sockets: STORM
      ? { executor: 'per-vu-iterations', vus: CONNS, iterations: 1, maxDuration: `${HOLD + 120}s` }
      : { executor: 'ramping-vus', startVUs: 0, stages: [{ duration: `${RAMP}s`, target: CONNS },
        { duration: `${HOLD}s`, target: CONNS }], gracefulRampDown: '5s', gracefulStop: `${HOLD + 30}s` },
  },
  thresholds: {
    tenant_leaks: ['count==0'],
    ws_abnormal_close: ['count==0'],
    ws_no_first_frame: ['count==0'],
    ws_first_frame_ms: ['p(95)<2000'],
  },
  summaryTrendStats: ['min', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export default function () {
  const s = SESSIONS[((__VU - 1) * 37) % SESSIONS.length];
  const url = `${WS_BASE}/ws`;
  const params = {
    headers: { Origin: ORIGIN, Cookie: `oracle_session=${s.token}`, 'X-Forwarded-For': ipFor(s) },
    tags: { name: 'WS /ws' },
  };
  const t0 = Date.now();
  let gotFirst = false;
  let capacity = false;
  const res = ws.connect(url, params, (socket) => {
    socket.on('open', () => { connectMs.add(Date.now() - t0); opened.add(1); });
    socket.on('message', (raw) => {
      if (!gotFirst) { gotFirst = true; firstFrameMs.add(Date.now() - t0); }
      if (raw.indexOf('SNTL') !== -1) {
        // Isolation holds on sockets too: no other tenant's sentinel, ever.
        for (const o of SESSIONS) {
          if (o.user.sentinel !== s.user.sentinel && raw.indexOf(o.user.sentinel) !== -1) {
            socket.close();
            throw new Error(`TENANT LEAK over WebSocket: ${s.user.tenant_slug} got ${o.user.sentinel}`);
          }
        }
      }
      if (raw.indexOf('"PING"') !== -1) { pings.add(1); socket.send(JSON.stringify({ type: 'PONG' })); }
    });
    socket.on('close', (code) => {
      if (code === 1013) { capacity = true; refused.add(1); }
    });
    socket.setTimeout(() => socket.close(1000), HOLD * 1000);
  });
  if (capacity) { sleep(5); return; }  // a clean, documented refusal — counted, not "abnormal"
  // A failed connect pauses before the VU retries: a harness must not turn one
  // bad token into a reconnect flood (the first run made 26,285 attempts).
  if (!res || (res.status !== 101)) { abnormal.add(1); sleep(5); return; }
  if (!gotFirst) noFirstFrame.add(1);
  check(res, { 'ws upgraded': (r) => r && r.status === 101 });
}

export function handleSummary(data) {
  return writeSummary(data, `ws_load-${CONNS}${STORM ? '-storm' : ''}`);
}
