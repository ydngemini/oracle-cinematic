// @vitest-environment jsdom
/**
 * Two Neoh surfaces, one set of requests.
 *
 * Every useNeohChannel asked /api/ai/chat/status for itself, and refetched
 * /api/ai/chat/messages on every conversation revision — so each extra
 * surface (and the AI hub's own status probe) was another copy of both.
 */

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmGet } from '../state/useCrmApi';
import { STATUS_RETRY_MS, fetchChatStatus, resetNeohChannelCache, useNeohChannel } from './useNeohChannel';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn(), crmPost: vi.fn() }));
vi.mock('../state', () => ({
  ACTIONS: { AI_CHAT_HYDRATE: 'AI_CHAT_HYDRATE', AI_CHAT_SEND_LOCAL: 'AI_CHAT_SEND_LOCAL' },
  useOracleState: () => globalThis.__chatState,
  useOracleDispatch: () => globalThis.__chatDispatch,
}));

beforeEach(() => {
  vi.clearAllMocks();
  resetNeohChannelCache();
  globalThis.__chatState = { aiChatMessages: [], aiChatRevision: 0, aiChatConnection: 'online' };
  globalThis.__chatDispatch = { dispatch: vi.fn(), wsRef: { current: null } };
  crmGet.mockImplementation((path) => (
    path === '/api/ai/chat/status'
      ? Promise.resolve({ enabled: true })
      : Promise.resolve({ messages: [{ id: 'm1' }] })
  ));
});

afterEach(cleanup);

// Let each rejected check schedule its retry, then fire that retry.
async function runRetries(count) {
  for (const delay of STATUS_RETRY_MS.slice(0, count)) {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(delay + 10);
    });
  }
}

const calls = (prefix) => crmGet.mock.calls.filter(([path]) => path.startsWith(prefix)).length;

describe('useNeohChannel', () => {
  it('asks the status once per session and hydrates once per revision, however many surfaces mount', async () => {
    const a = renderHook(() => useNeohChannel());
    const b = renderHook(() => useNeohChannel({ open: true }));
    await waitFor(() => expect(a.result.current.available).toBe(true));
    await waitFor(() => expect(b.result.current.available).toBe(true));
    await fetchChatStatus();
    await waitFor(() => expect(calls('/api/ai/chat/messages')).toBe(1));
    expect(calls('/api/ai/chat/status')).toBe(1);
    // A third surface later in the session costs nothing either.
    renderHook(() => useNeohChannel());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls('/api/ai/chat/status')).toBe(1);
    expect(calls('/api/ai/chat/messages')).toBe(1);
    expect(globalThis.__chatDispatch.dispatch).toHaveBeenCalledWith({ type: 'AI_CHAT_HYDRATE', payload: [{ id: 'm1' }] });
  });

  it('does not cache a failed status check', async () => {
    crmGet.mockImplementationOnce(() => Promise.reject(new Error('down')));
    await expect(fetchChatStatus()).rejects.toThrow('down');
    await expect(fetchChatStatus()).resolves.toEqual({ enabled: true });
  });

  it('refuses to send while reconnecting, in product language', async () => {
    const { result } = renderHook(() => useNeohChannel());
    expect(result.current.send('hello')).toBe(false);
    await waitFor(() => expect(result.current.notice).toMatch(/^Neoh is reconnecting/));
    expect(result.current.notice).not.toMatch(/private channel/);
  });

  it('treats a failed status check as unreachable and retries — never as "switched off"', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let failures = 2;
      crmGet.mockImplementation((path) => {
        if (path !== '/api/ai/chat/status') return Promise.resolve({ messages: [] });
        if (failures > 0) { failures -= 1; return Promise.reject(Object.assign(new Error('429'), { status: 429 })); }
        return Promise.resolve({ enabled: true });
      });
      const { result } = renderHook(() => useNeohChannel());
      await runRetries(2);
      await waitFor(() => expect(result.current.available).toBe(true));
      expect(result.current.statusFailed).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reports a status check that keeps failing as unreachable, and can try again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      crmGet.mockImplementation((path) => (path === '/api/ai/chat/status'
        ? Promise.reject(new Error('offline')) : Promise.resolve({ messages: [] })));
      const { result } = renderHook(() => useNeohChannel());
      await runRetries(STATUS_RETRY_MS.length);
      await waitFor(() => expect(result.current.statusFailed).toBe(true));
      expect(result.current.available).toBe(false);
      crmGet.mockImplementation(() => Promise.resolve({ enabled: true, messages: [] }));
      result.current.retryStatus();
      await waitFor(() => expect(result.current.available).toBe(true));
      expect(result.current.statusFailed).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
