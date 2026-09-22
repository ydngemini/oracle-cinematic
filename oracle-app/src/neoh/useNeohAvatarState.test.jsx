// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useNeohAvatarState } from './useNeohAvatarState';

afterEach(cleanup);

/** Render the hook and expose its latest value. */
function probe(props) {
  const seen = { current: null };
  function Probe(facts) {
    seen.current = useNeohAvatarState(facts);
    return null;
  }
  const view = render(<Probe {...props} />);
  return { seen, rerender: (next) => view.rerender(<Probe {...next} />) };
}

describe('useNeohAvatarState', () => {
  it('is idle with nothing happening', () => {
    const { seen } = probe({});
    expect(seen.current.state).toBe('idle');
  });

  it('derives thinking from a real pending message, not a timer', () => {
    const { seen } = probe({ messages: [{ status: 'pending' }] });
    expect(seen.current.state).toBe('thinking');
  });

  it('reports disconnected when the channel is offline', () => {
    const { seen } = probe({ connection: 'offline' });
    expect(seen.current.state).toBe('disconnected');
  });

  it('asks for the person when a command awaits approval', () => {
    const { seen } = probe({ commandStatus: { state: 'awaiting_approval' } });
    expect(seen.current.state).toBe('needs_attention');
    expect(seen.current.attentionLevel).toBe(1);
  });

  it('shows error when a command failed', () => {
    const { seen } = probe({ commandStatus: { state: 'failed' } });
    expect(seen.current.state).toBe('error');
  });

  it('does NOT celebrate merely because a command is running', () => {
    const { seen } = probe({ commandStatus: { state: 'running', detail: 'place_call' } });
    expect(seen.current.state).toBe('acting');
    expect(seen.current.actionType).toBe('call');
  });

  it('celebrates only a backend-confirmed completion, then settles', () => {
    vi.useFakeTimers();
    try {
      const { seen, rerender } = probe({ commandStatus: { state: 'idle' } });
      expect(seen.current.state).toBe('idle');

      act(() => { rerender({ commandStatus: { state: 'completed' } }); });
      expect(seen.current.state).toBe('success');

      // It is an acknowledgement, not a mood: it lets go on its own.
      act(() => { vi.advanceTimersByTime(2_000); });
      expect(seen.current.state).toBe('idle');
    } finally {
      vi.useRealTimers();
    }
  });

  it('zeroes amplitude in states that are not voice states', () => {
    const { seen } = probe({ messages: [{ status: 'pending' }], audioLevel: 0.9 });
    expect(seen.current.state).toBe('thinking');
    expect(seen.current.audioLevel).toBe(0);
  });

  it('passes amplitude through while speaking', () => {
    const { seen } = probe({ speaking: true, audioLevel: 0.7 });
    expect(seen.current.state).toBe('speaking');
    expect(seen.current.audioLevel).toBe(0.7);
  });

  it('passes amplitude through while listening', () => {
    const { seen } = probe({ micActive: true, audioLevel: 0.4 });
    expect(seen.current.state).toBe('listening');
    expect(seen.current.audioLevel).toBe(0.4);
  });

  it('never reports a state that contradicts a dead channel', () => {
    const { seen } = probe({
      connection: 'connecting',
      messages: [{ status: 'pending' }],
      commandStatus: { state: 'running', detail: 'send_sms' },
    });
    expect(seen.current.state).toBe('disconnected');
  });
});
