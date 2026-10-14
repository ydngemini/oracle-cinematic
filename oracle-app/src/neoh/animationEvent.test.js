/**
 * AC15.02 — only validated events drive Neoh, and the statechart applies
 * hysteresis without ever showing a state no live event supports.
 */
import { describe, expect, it } from 'vitest';

import { AVATAR_STATES, SUCCESS_HOLD_MS } from './avatarModel';
import {
  createAnimationMachine,
  ENTER_MS,
  ERROR_HOLD_MS,
  EVENT_ORIGINS,
  EVENT_SOURCES,
  EVENT_STAGES,
  ingestEvents,
  MAX_FUTURE_SKEW_MS,
  MAX_STREAMS,
  resolveAnimation,
  STAGE_SOURCES,
  THINKING_VALID_MS,
  validateAnimationEvent,
} from './animationEvent';

const T0 = 1_760_000_000_000;

/** A valid event for a stage, from its first allowed source. */
function event(stage, overrides = {}) {
  const transient = ['success', 'error', 'cancelled'].includes(stage);
  const windows = {
    success: SUCCESS_HOLD_MS, error: ERROR_HOLD_MS, cancelled: SUCCESS_HOLD_MS,
    thinking: THINKING_VALID_MS, acting: THINKING_VALID_MS,
  };
  return {
    id: `${stage}-${overrides.at ?? T0}-${Math.random().toString(36).slice(2, 8)}`,
    origin: 'chat',
    sessionId: 'turn-1',
    stage,
    source: STAGE_SOURCES[stage][0],
    at: T0,
    validForMs: windows[stage] ?? Infinity,
    transient,
    ...overrides,
  };
}

/** Ingest then resolve; returns the next machine and what is shown. */
function step(machine, events, now) {
  const ingested = ingestEvents(machine, events, now);
  const resolved = resolveAnimation(ingested.machine, now);
  return { ...resolved, rejected: ingested.rejected };
}

/** Show a state immediately, skipping hysteresis by resolving twice. */
function settle(machine, events, now) {
  const first = step(machine, events, now);
  return step(first.machine, [], now + ENTER_MS);
}

describe('validateAnimationEvent', () => {
  it('accepts a well-formed event for every stage from its allowed source', () => {
    for (const stage of EVENT_STAGES) {
      const result = validateAnimationEvent(event(stage), T0);
      expect(result.ok, stage).toBe(true);
    }
  });

  it('rejects a stage asserted by a source that cannot know it', () => {
    const cases = [
      ['success', 'chat_turn'],          // a finished answer is not a receipt
      ['success', 'command_request'],
      ['speaking', 'chat_turn'],         // text arriving is not audio playing
      ['speaking', 'browser_mic'],
      ['listening', 'chat_turn'],        // only the microphone hears
      ['thinking', 'browser_mic'],
      ['needs_attention', 'chat_turn'],
      ['disconnected', 'chat_turn'],
      ['acting', 'chat_turn'],
    ];
    for (const [stage, source] of cases) {
      expect(validateAnimationEvent(event(stage, { source }), T0), `${stage} from ${source}`)
        .toEqual({ ok: false, reason: 'unverified_source' });
    }
  });

  it('rejects unknown sources, origins and stages', () => {
    expect(validateAnimationEvent(event('thinking', { source: 'llm' }), T0).reason).toBe('unverified_source');
    expect(validateAnimationEvent(event('thinking', { source: 'model_output' }), T0).reason).toBe('unverified_source');
    expect(validateAnimationEvent(event('thinking', { origin: 'model' }), T0).reason).toBe('bad_origin');
    expect(validateAnimationEvent(event('thinking', { stage: 'dancing' }), T0).reason).toBe('bad_stage');
    expect(validateAnimationEvent(event('thinking', { stage: 'happy' }), T0).reason).toBe('bad_stage');
  });

  it('rejects any coordinate, pose or animation parameter', () => {
    for (const key of ['x', 'y', 'z', 'position', 'rotation', 'pose', 'bones', 'gazeX', 'yaw', 'pitch',
      'keyframes', 'visemes', 'blendShapes', 'morph', 'transform', 'quaternion']) {
      expect(validateAnimationEvent({ ...event('thinking'), [key]: 0.5 }, T0).reason, key).toBe('coordinates');
    }
  });

  it('rejects unknown fields and nested values', () => {
    expect(validateAnimationEvent({ ...event('thinking'), mood: 'joy' }, T0).reason).toBe('unknown_field');
    expect(validateAnimationEvent({ ...event('thinking'), reason: { code: 'x' } }, T0).reason).toBe('bad_field');
    expect(validateAnimationEvent({ ...event('thinking'), sessionId: ['a'] }, T0).reason).toBe('bad_field');
  });

  it('rejects missing, malformed and impossible timestamps', () => {
    const noAt = event('thinking');
    delete noAt.at;
    expect(validateAnimationEvent(noAt, T0).reason).toBe('missing_at');
    expect(validateAnimationEvent(event('thinking', { at: Number.NaN }), T0).reason).toBe('bad_timestamp');
    expect(validateAnimationEvent(event('thinking', { at: '2026-10-14' }), T0).reason).toBe('bad_timestamp');
    expect(validateAnimationEvent(event('thinking', { at: -1 }), T0).reason).toBe('bad_timestamp');
    expect(validateAnimationEvent(event('thinking', { at: T0 + MAX_FUTURE_SKEW_MS + 1 }), T0).reason).toBe('future');
  });

  it('rejects expired events', () => {
    const success = event('success');
    expect(validateAnimationEvent(success, T0 + SUCCESS_HOLD_MS - 1).ok).toBe(true);
    expect(validateAnimationEvent(success, T0 + SUCCESS_HOLD_MS).reason).toBe('expired');
    expect(validateAnimationEvent(event('thinking'), T0 + THINKING_VALID_MS).reason).toBe('expired');
  });

  it('refuses a success, error or cancellation that tries to hold longer than its ceiling', () => {
    expect(validateAnimationEvent(event('success', { validForMs: SUCCESS_HOLD_MS + 1 }), T0).reason).toBe('bad_window');
    expect(validateAnimationEvent(event('success', { transient: false }), T0).reason).toBe('bad_window');
    expect(validateAnimationEvent(event('success', { validForMs: Infinity }), T0).reason).toBe('bad_window');
    expect(validateAnimationEvent(event('error', { validForMs: ERROR_HOLD_MS * 2 }), T0).reason).toBe('bad_window');
    // An in-flight state cannot claim to think forever.
    expect(validateAnimationEvent(event('thinking', { validForMs: Infinity }), T0).reason).toBe('bad_window');
    expect(validateAnimationEvent(event('acting', { validForMs: THINKING_VALID_MS + 1 }), T0).reason).toBe('bad_window');
    expect(validateAnimationEvent(event('thinking', { validForMs: 0 }), T0).reason).toBe('bad_window');
  });

  it('allows amplitude, urgency and action type only where they mean something', () => {
    expect(validateAnimationEvent(event('speaking', { amplitude: 0.4 }), T0).ok).toBe(true);
    expect(validateAnimationEvent(event('speaking', { amplitude: 1.4 }), T0).reason).toBe('bad_field');
    expect(validateAnimationEvent(event('thinking', { amplitude: 0.4 }), T0).reason).toBe('bad_field');
    expect(validateAnimationEvent(event('error', { urgency: 'high' }), T0).ok).toBe(true);
    expect(validateAnimationEvent(event('error', { urgency: 'panic' }), T0).reason).toBe('bad_field');
    expect(validateAnimationEvent(event('acting', { actionType: 'call' }), T0).ok).toBe(true);
    expect(validateAnimationEvent(event('acting', { actionType: 'place_call' }), T0).reason).toBe('bad_field');
    expect(validateAnimationEvent(event('idle', { actionType: 'call' }), T0).reason).toBe('bad_field');
    expect(validateAnimationEvent(event('thinking', { priority: 99 }), T0).reason).toBe('bad_field');
  });

  it('never throws on arbitrary input (fuzz)', () => {
    const junk = [undefined, null, 0, 1, '', 'x', true, [], [1], () => {}, Symbol('s'), Number.NaN, {}];
    let seed = 7;
    const rand = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    const pick = (list) => list[Math.floor(rand() * list.length)];
    const values = [...junk, ...EVENT_STAGES, ...EVENT_SOURCES, ...EVENT_ORIGINS, T0, -T0, Infinity, 0.5, 2, 'thinking'];
    const keys = ['id', 'origin', 'stage', 'source', 'at', 'validForMs', 'transient', 'seq', 'priority',
      'amplitude', 'urgency', 'actionType', 'reason', 'sessionId', 'actionId', 'x', 'pose', '__proto__', 'constructor'];
    for (const value of junk) expect(() => validateAnimationEvent(value, T0)).not.toThrow();
    for (let i = 0; i < 3_000; i += 1) {
      const raw = rand() < 0.5 ? event(pick(EVENT_STAGES)) : {};
      const n = Math.floor(rand() * 5);
      for (let k = 0; k < n; k += 1) raw[pick(keys)] = pick(values);
      let result;
      expect(() => { result = validateAnimationEvent(raw, T0); }).not.toThrow();
      if (result.ok) {
        // Anything accepted is genuinely well-formed.
        expect(STAGE_SOURCES[result.event.stage]).toContain(result.event.source);
        expect(Number.isFinite(result.event.at)).toBe(true);
        expect(Object.keys(result.event).some((key) => ['x', 'pose'].includes(key))).toBe(false);
      }
    }
  });
});

describe('statechart', () => {
  it('rests at idle with nothing live, and on garbage', () => {
    const machine = createAnimationMachine();
    expect(resolveAnimation(machine, T0).view.state).toBe('idle');
    const result = step(machine, [null, 'thinking', { stage: 'thinking' }, event('thinking', { source: 'llm' })], T0);
    expect(result.view.state).toBe('idle');
    expect(result.rejected).toHaveLength(4);
  });

  it('applies the product precedence across streams', () => {
    const events = [
      event('thinking', { sessionId: 'a' }),
      event('acting', { origin: 'command', sessionId: 'cmd', actionType: 'call' }),
      event('needs_attention', { origin: 'command', sessionId: 'approval' }),
    ];
    const shown = settle(createAnimationMachine(), events, T0);
    expect(shown.view.state).toBe('needs_attention');
    const withError = settle(shown.machine, [event('error', { origin: 'channel', sessionId: 'x', at: T0 + 1 })], T0 + 200);
    expect(withError.view.state).toBe('error');
  });

  it('ignores an older event for the same stream (out of order)', () => {
    const done = event('success', { at: T0 + 500 });
    const late = event('thinking', { at: T0 + 100 });
    const result = step(createAnimationMachine(), [done, late], T0 + 600);
    expect(result.view.state).toBe('success');
    expect(result.rejected).toEqual([{ id: late.id, reason: 'out_of_order' }]);
  });

  it('breaks exact ties by seq, then lets an ending beat an activity', () => {
    const a = event('thinking', { seq: 2 });
    const b = event('idle', { seq: 1 });
    expect(step(createAnimationMachine(), [a, b], T0 + ENTER_MS).rejected[0].reason).toBe('out_of_order');
    const streaming = event('thinking');
    const finished = event('idle');
    const result = settle(createAnimationMachine(), [streaming, finished], T0);
    expect(result.view.state).toBe('idle');
  });

  it('remembers an ended stream so a late replay cannot revive it', () => {
    let { machine } = settle(createAnimationMachine(), [event('thinking', { at: T0 })], T0);
    ({ machine } = step(machine, [event('idle', { at: T0 + 1_000 })], T0 + 1_000));
    const replay = step(machine, [event('thinking', { at: T0 + 500 })], T0 + 2_000);
    expect(replay.view.state).toBe('idle');
    expect(replay.rejected[0].reason).toBe('out_of_order');
  });

  it('holds success for exactly its window, then settles, and says when', () => {
    const result = step(createAnimationMachine(), [event('success')], T0);
    expect(result.view.state).toBe('success');
    expect(result.nextAt).toBe(T0 + SUCCESS_HOLD_MS);
    expect(resolveAnimation(result.machine, T0 + SUCCESS_HOLD_MS - 1).view.state).toBe('success');
    expect(resolveAnimation(result.machine, T0 + SUCCESS_HOLD_MS).view.state).toBe('idle');
  });

  it('delays an activity by ENTER_MS but drops it the instant it ends (hysteresis)', () => {
    let result = step(createAnimationMachine(), [event('thinking')], T0);
    expect(result.view.state).toBe('idle');
    expect(result.nextAt).toBe(T0 + ENTER_MS);
    result = step(result.machine, [], T0 + ENTER_MS);
    expect(result.view.state).toBe('thinking');
    result = step(result.machine, [event('idle', { at: T0 + ENTER_MS + 1 })], T0 + ENTER_MS + 1);
    expect(result.view.state).toBe('idle');
  });

  it('never delays an alert or an outcome', () => {
    for (const stage of ['error', 'needs_attention', 'disconnected', 'success']) {
      expect(step(createAnimationMachine(), [event(stage)], T0).view.state, stage).toBe(stage);
    }
  });

  it('keeps showing a still-true state while a new one settles, never a false one', () => {
    let result = settle(createAnimationMachine(), [event('thinking')], T0);
    expect(result.view.state).toBe('thinking');
    // Acting starts while the turn is still thinking: thinking holds (true)…
    result = step(result.machine, [event('acting', { origin: 'command', sessionId: 'c', at: T0 + 200 })], T0 + 200);
    expect(result.view.state).toBe('thinking');
    // …and if thinking ends meanwhile, the face drops to idle, not to a guess.
    result = step(result.machine, [event('idle', { at: T0 + 250 })], T0 + 250);
    expect(result.view.state).toBe('idle');
    result = step(result.machine, [], T0 + 200 + ENTER_MS);
    expect(result.view.state).toBe('acting');
  });

  it('does not flicker under an event storm', () => {
    let machine = createAnimationMachine();
    const seen = [];
    for (let i = 0; i < 200; i += 1) {
      const at = T0 + i * 30; // a fresh state every 30 ms
      const stage = i % 2 ? 'idle' : 'thinking';
      const result = step(machine, [event(stage, { at })], at);
      machine = result.machine;
      seen.push(result.view.state);
    }
    expect(new Set(seen)).toEqual(new Set(['idle']));
  });

  it('only ever shows a state some live event supports (random storm)', () => {
    let seed = 11;
    const rand = () => { seed = (seed * 48271) % 2147483647; return seed / 2147483647; };
    const stages = EVENT_STAGES;
    let machine = createAnimationMachine();
    let now = T0;
    for (let i = 0; i < 2_000; i += 1) {
      now += Math.floor(rand() * 200);
      const batch = [];
      const n = Math.floor(rand() * 4);
      for (let k = 0; k < n; k += 1) {
        const stage = stages[Math.floor(rand() * stages.length)];
        const raw = event(stage, {
          sessionId: `s${Math.floor(rand() * 6)}`,
          at: now - Math.floor(rand() * 3_000), // some arrive late / out of order
        });
        if (rand() < 0.1) raw.source = 'llm';
        if (rand() < 0.05) raw.pose = 'wave';
        batch.push(raw);
      }
      const result = step(machine, batch, now);
      machine = result.machine;
      const { state } = result.view;
      expect(AVATAR_STATES).toContain(state);
      if (state !== 'idle') {
        const live = Object.values(machine.streams).filter((e) =>
          e.stage === state && now < e.at + e.validForMs);
        expect(live.length, `${state} at step ${i}`).toBeGreaterThan(0);
      }
      expect(Object.keys(machine.streams).length).toBeLessThanOrEqual(MAX_STREAMS);
    }
  });

  it('caps remembered streams under a flood of distinct activities', () => {
    const flood = Array.from({ length: 500 }, (_, i) => event('thinking', { sessionId: `s${i}`, at: T0 + i }));
    const result = step(createAnimationMachine(), flood, T0 + 600);
    expect(Object.keys(result.machine.streams).length).toBe(MAX_STREAMS);
  });

  it('lets the person barge in: a newer listening event stops speaking', () => {
    const speaking = event('speaking', { origin: 'audio', sessionId: 'playback', at: T0 });
    let result = settle(createAnimationMachine(), [speaking], T0);
    expect(result.view.state).toBe('speaking');
    // Without barge-in, speaking outranks listening…
    const older = event('listening', { origin: 'voice', sessionId: 'mic', at: T0 - 10 });
    expect(settle(createAnimationMachine(), [older, speaking], T0).view.state).toBe('speaking');
    // …but the person starting to talk over Neoh wins.
    const barge = event('listening', { origin: 'voice', sessionId: 'mic', at: T0 + 500 });
    result = settle(result.machine, [barge], T0 + 500);
    expect(result.view.state).toBe('listening');
  });

  it('shows a cancellation as idle with its reason, not as an error', () => {
    const result = step(createAnimationMachine(), [event('cancelled', { origin: 'command', sessionId: 'command', reason: 'cancelled' })], T0);
    expect(result.view).toMatchObject({ state: 'idle', reason: 'cancelled' });
  });

  it('carries the reason of a failure through to the view', () => {
    const result = step(createAnimationMachine(), [event('error', { reason: 'timeout' })], T0);
    expect(result.view).toMatchObject({ state: 'error', reason: 'timeout' });
  });
});
