import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  AFTER_CALL_MINUTES, CLOSED_RECENT_DAYS, DORMANT_DAYS, ENGAGED_DAYS, LIVING_STATES,
  ago, composeLiving, livingLine,
} from './livingModel';

const here = dirname(fileURLToPath(import.meta.url));
const livingPy = resolve(here, '../../../backend/living_state.py');

const NOW = Date.parse('2026-09-05T12:00:00Z');
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const MIN = 60_000;
const DAY = 86_400_000;

describe('the vocabulary and thresholds are shared with the server', () => {
  const py = readFileSync(livingPy, 'utf8');

  it('declares the same states in the same precedence', () => {
    const m = py.match(/STATES\s*=\s*\(([^)]*)\)/);
    expect(m, 'living_state.py must declare STATES').toBeTruthy();
    const pyStates = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
    expect(pyStates).toEqual([...LIVING_STATES]);
  });

  it.each([
    ['ENGAGED_DAYS', ENGAGED_DAYS],
    ['DORMANT_DAYS', DORMANT_DAYS],
    ['AFTER_CALL_MINUTES', AFTER_CALL_MINUTES],
    ['CLOSED_RECENT_DAYS', CLOSED_RECENT_DAYS],
  ])('%s matches', (name, value) => {
    const m = py.match(new RegExp(`^${name}\\s*=\\s*(\\d+)`, 'm'));
    expect(m, `${name} must be declared in living_state.py`).toBeTruthy();
    expect(Number(m[1])).toBe(value);
  });
});

describe('composeLiving: the browser only adds what it alone knows', () => {
  const server = { state: 'engaged', signals_7d: 3, last_activity_at: iso(2 * DAY) };

  it('passes the server state through untouched when there is no presence', () => {
    expect(composeLiving(server, null, NOW)).toEqual({ ...server, local: false });
  });

  it('a live softphone call wins over anything the server said', () => {
    const p = { state: 'connected', startedAt: iso(90_000) };
    const out = composeLiving({ state: 'under_contract' }, p, NOW);
    expect(out.state).toBe('calling');
    expect(out.local).toBe(true);
  });

  it('a call that just ended reads as after_call, then expires', () => {
    expect(composeLiving(server, { state: 'idle', endedAt: iso(5 * MIN) }, NOW).state).toBe('after_call');
    expect(composeLiving(server, { state: 'idle', endedAt: iso((AFTER_CALL_MINUTES + 1) * MIN) }, NOW).state)
      .toBe('engaged');
  });

  it('never invents a state from nothing', () => {
    expect(composeLiving(null, null, NOW)).toBeNull();
    expect(composeLiving({ state: 'vibing' }, null, NOW)).toBeNull();
  });

  it('does not mutate the server payload', () => {
    const frozen = Object.freeze({ state: 'quiet' });
    expect(() => composeLiving(frozen, { state: 'ringing' }, NOW)).not.toThrow();
  });
});

describe('livingLine: every sentence is a recorded time or count', () => {
  it('calling carries a running duration', () => {
    expect(livingLine({ state: 'calling', since: iso(134_000) }, NOW)).toBe('On a call · 2:14');
  });
  it('after_call says when', () => {
    expect(livingLine({ state: 'after_call', since: iso(4 * MIN) }, NOW)).toBe('Call ended 4 min ago');
  });
  it('under contract names the closing when known', () => {
    const t = { closing_deadline: '2026-10-12' };
    expect(livingLine({ state: 'under_contract', transaction: t }, NOW)).toMatch(/^Under contract · closing Oct 1[12]$/);
    expect(livingLine({ state: 'under_contract' }, NOW)).toBe('Under contract');
  });
  it('engaged counts, and never says "very active"', () => {
    const line = livingLine({ state: 'engaged', signals_7d: 3, last_activity_at: iso(3 * 60 * MIN) }, NOW);
    expect(line).toBe('3 signals this week · last 3 h ago');
    expect(line).not.toMatch(/very|hot|warm/i);
  });
  it('dormant with no history says so plainly', () => {
    expect(livingLine({ state: 'dormant' }, NOW)).toBe('Never heard from');
    expect(livingLine({ state: 'dormant', last_activity_at: iso(90 * DAY) }, NOW)).toBe('Last heard from 3 months ago');
  });
  it('ago handles garbage without throwing', () => {
    expect(ago('not a date', NOW)).toBe('a while ago');
    expect(ago(iso(0), NOW)).toBe('just now');
  });
});
