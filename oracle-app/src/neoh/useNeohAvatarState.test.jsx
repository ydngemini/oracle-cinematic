// @vitest-environment jsdom
/**
 * AC15.03 — Neoh's states tell the truth.
 *
 * Every fact here looks like what the app really holds: chat turns carry a
 * request id and the local time their last frame arrived (`observed_at`);
 * command statuses carry the time they were set (`at`, AssistantContext).
 * Activity states wait ENTER_MS before showing (hysteresis), so tests that
 * expect thinking/acting/listening/speaking let that pass first.
 */
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SUCCESS_HOLD_MS } from './avatarModel';
import { ENTER_MS, ERROR_HOLD_MS, THINKING_VALID_MS } from './animationEvent';
import { useNeohAvatarState } from './useNeohAvatarState';

const T0 = 1_760_000_000_000;

beforeEach(() => {
  vi.useFakeTimers({ now: T0 });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** Render the hook and expose its latest value. */
function probe(props) {
  const seen = { current: null };
  function Probe(facts) {
    seen.current = useNeohAvatarState(facts);
    return null;
  }
  const view = render(<Probe {...props} />);
  return { seen, rerender: (next) => act(() => { view.rerender(<Probe {...next} />); }) };
}

const wait = (ms) => act(() => { vi.advanceTimersByTime(ms); });
const settle = () => wait(ENTER_MS);

const turn = (status, extra = {}) => ({
  id: 'm-1', request_id: 'r-1', role: 'assistant', status, observed_at: Date.now(), ...extra,
});
const status = (state, extra = {}) => ({ state, at: Date.now(), seq: 1, ...extra });

describe('useNeohAvatarState', () => {
  it('is idle with nothing happening', () => {
    const { seen } = probe({});
    expect(seen.current.state).toBe('idle');
  });

  describe('thinking', () => {
    it('derives thinking from a real pending turn, not a timer', () => {
      const { seen } = probe({ messages: [turn('pending')] });
      settle();
      expect(seen.current.state).toBe('thinking');
    });

    it('stops claiming a turn with no backend activity past the ceiling', () => {
      const { seen } = probe({ messages: [turn('streaming')] });
      settle();
      expect(seen.current.state).toBe('thinking');
      wait(THINKING_VALID_MS);
      expect(seen.current.state).toBe('idle');
    });

    it('does not think for a pending turn left in old history', () => {
      const old = turn('pending', { observed_at: undefined, updated_at: new Date(T0 - 10 * 60_000).toISOString() });
      const { seen } = probe({ messages: [old] });
      settle();
      expect(seen.current.state).toBe('idle');
    });

    it('refuses a turn with no timestamp at all', () => {
      const { seen } = probe({ messages: [{ role: 'assistant', request_id: 'r', status: 'pending' }] });
      settle();
      expect(seen.current.state).toBe('idle');
    });

    it('stops thinking the moment the turn completes — no hold', () => {
      const { seen, rerender } = probe({ messages: [turn('streaming')] });
      settle();
      wait(1);
      rerender({ messages: [turn('completed')] });
      expect(seen.current.state).toBe('idle');
    });

    it('stops when a turn is cleared from the thread', () => {
      const { seen, rerender } = probe({ messages: [turn('pending')] });
      settle();
      rerender({ messages: [] });
      expect(seen.current.state).toBe('idle');
    });
  });

  describe('success', () => {
    it('celebrates a chat turn only when it carries an applied receipt, then settles', () => {
      const { seen, rerender } = probe({ messages: [turn('streaming')] });
      settle();
      wait(1);
      rerender({ messages: [turn('completed', { actions: [{ ok: true, action_id: 'a-1' }] })] });
      expect(seen.current.state).toBe('success');
      wait(SUCCESS_HOLD_MS);
      expect(seen.current.state).toBe('idle');
    });

    it('does not celebrate an answer with no action, or an undone action', () => {
      const plain = probe({ messages: [turn('completed', { actions: [] })] });
      expect(plain.seen.current.state).toBe('idle');
      cleanup();
      const undone = probe({ messages: [turn('completed', { actions: [{ action_id: 'a', status: 'undone', ok: true }] })] });
      expect(undone.seen.current.state).toBe('idle');
      cleanup();
      const readOnly = probe({ messages: [turn('completed', { actions: [{ ok: true }] })] });
      expect(readOnly.seen.current.state).toBe('idle');
    });

    it('celebrates a command only with the execute receipt, never "analysis complete"', () => {
      const analysis = probe({ commandStatus: status('completed') });
      expect(analysis.seen.current.state).toBe('idle');
      cleanup();
      const queued = probe({ commandStatus: status('queued') });
      expect(queued.seen.current.state).toBe('idle');
      cleanup();
      const { seen, rerender } = probe({ commandStatus: status('idle') });
      rerender({ commandStatus: status('completed', { receipt: true, seq: 2 }) });
      expect(seen.current.state).toBe('success');
      wait(SUCCESS_HOLD_MS + 1);
      expect(seen.current.state).toBe('idle');
    });

    it('never replays an old receipt on mount (stale success)', () => {
      const old = { state: 'completed', receipt: true, at: T0 - 60_000, seq: 4 };
      const { seen } = probe({ commandStatus: old });
      expect(seen.current.state).toBe('idle');
      const oldTurn = turn('completed', { observed_at: T0 - 60_000, actions: [{ ok: true, action_id: 'a' }] });
      cleanup();
      const chat = probe({ messages: [oldTurn] });
      expect(chat.seen.current.state).toBe('idle');
    });

    it('does not trust a command status that was never stamped', () => {
      const { seen } = probe({ commandStatus: { state: 'completed', receipt: true } });
      expect(seen.current.state).toBe('idle');
    });
  });

  describe('failures', () => {
    it('shows a failed turn as an error with its reason, then settles; the text keeps it', () => {
      const { seen } = probe({ messages: [turn('failed', { error_code: 'AI_RESPONSE_INTERRUPTED' })] });
      expect(seen.current).toMatchObject({ state: 'error', reason: 'interrupted' });
      wait(ERROR_HOLD_MS);
      expect(seen.current.state).toBe('idle');
    });

    it('names an unavailable provider differently from a timeout', () => {
      const unavailable = probe({ messages: [turn('failed', { error_code: 'AI_RESPONSE_UNAVAILABLE' })] });
      expect(unavailable.seen.current.reason).toBe('unavailable');
      cleanup();
      const timeout = probe({ messages: [turn('failed', { error_code: 'AI_RESPONSE_TIMEOUT' })] });
      expect(timeout.seen.current.reason).toBe('timeout');
    });

    it('shows error when a command failed', () => {
      const { seen } = probe({ commandStatus: status('failed') });
      expect(seen.current).toMatchObject({ state: 'error', reason: 'failed' });
    });

    it('shows a refused send as an error', () => {
      const { seen } = probe({ failed: true });
      expect(seen.current).toMatchObject({ state: 'error', reason: 'send_refused' });
    });

    it('treats a cancellation as idle with a reason — not an error', () => {
      const { seen } = probe({ commandStatus: status('cancelled') });
      expect(seen.current).toMatchObject({ state: 'idle', reason: 'cancelled' });
    });
  });

  describe('approval', () => {
    it('asks for the person while a command awaits approval, for as long as it does', () => {
      const { seen, rerender } = probe({ commandStatus: status('awaiting_approval') });
      expect(seen.current).toMatchObject({ state: 'needs_attention', reason: 'approval', attentionLevel: 1 });
      wait(10 * 60_000);
      expect(seen.current.state).toBe('needs_attention');
      rerender({ commandStatus: status('idle', { seq: 2 }) });
      expect(seen.current.state).toBe('idle');
    });
  });

  describe('acting', () => {
    it('does NOT celebrate merely because a command is running', () => {
      const { seen } = probe({ commandStatus: status('running', { detail: 'place_call' }) });
      settle();
      expect(seen.current.state).toBe('acting');
      expect(seen.current.actionType).toBe('call');
    });

    it('shows authorizing as acting on the staged intent', () => {
      const { seen } = probe({ commandStatus: status('authorizing', { detail: 'send_sms' }) });
      settle();
      expect(seen.current).toMatchObject({ state: 'acting', actionType: 'message' });
    });
  });

  describe('voice', () => {
    it('is listening only while the microphone is capturing', () => {
      const { seen, rerender } = probe({ micActive: true, audioLevel: 0.4 });
      settle();
      expect(seen.current.state).toBe('listening');
      expect(seen.current.audioLevel).toBe(0.4);
      rerender({ micActive: false, audioLevel: 0.4 });
      expect(seen.current.state).toBe('idle');
      expect(seen.current.audioLevel).toBe(0);
    });

    it('passes amplitude through only while speaking (audio playing)', () => {
      const { seen } = probe({ speaking: true, audioLevel: 0.7 });
      settle();
      expect(seen.current.state).toBe('speaking');
      expect(seen.current.audioLevel).toBe(0.7);
    });

    it('zeroes amplitude in states that are not voice states', () => {
      const { seen } = probe({ messages: [turn('pending')], audioLevel: 0.9 });
      settle();
      expect(seen.current.state).toBe('thinking');
      expect(seen.current.audioLevel).toBe(0);
    });

    it('stops speaking when the person barges in', () => {
      const { seen, rerender } = probe({ speaking: true });
      settle();
      expect(seen.current.state).toBe('speaking');
      wait(5);
      rerender({ speaking: true, micActive: true });
      settle();
      expect(seen.current.state).toBe('listening');
    });

    it('stops speaking the instant playback stops', () => {
      const { seen, rerender } = probe({ speaking: true });
      settle();
      rerender({ speaking: false });
      expect(seen.current.state).toBe('idle');
    });
  });

  it('never reports a state that contradicts a dead channel', () => {
    const { seen } = probe({
      connection: 'connecting',
      messages: [turn('pending')],
      commandStatus: status('running', { detail: 'send_sms' }),
    });
    settle();
    expect(seen.current.state).toBe('disconnected');
  });

  it('does not flicker when a request toggles faster than the eye', () => {
    const { seen, rerender } = probe({ asking: false });
    const states = new Set();
    for (let i = 0; i < 40; i += 1) {
      wait(20);
      rerender({ asking: i % 2 === 0 });
      states.add(seen.current.state);
    }
    expect([...states]).toEqual(['idle']);
  });
});
