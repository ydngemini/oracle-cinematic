// @vitest-environment jsdom
/**
 * Two Neoh surfaces, one set of requests.
 *
 * Every useNeohChannel asked /api/ai/chat/status for itself, and refetched
 * /api/ai/chat/messages on every conversation revision — so each extra
 * surface (and the AI hub's own status probe) was another copy of both.
 */

import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmGet } from '../state/useCrmApi';
import { fetchChatStatus, resetNeohChannelCache, useNeohChannel } from './useNeohChannel';

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
});
