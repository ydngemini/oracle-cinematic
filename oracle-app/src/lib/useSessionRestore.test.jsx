// @vitest-environment jsdom
/**
 * Found by the Mission 3 browser suite: one 429 from GET /auth/session put a
 * signed-in person back on the sign-in form. Only an authentication answer
 * may sign them out; anything else is retried and then reported as
 * unreachable, with the session left alone.
 */
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ apiGet: vi.fn() }));
vi.mock('./apiClient', () => api);
const { SESSION_RETRY_MS, useSessionRestore } = await import('./useSessionRestore');

const fail = (status) => Object.assign(new Error(String(status)), { status });

async function runRetries(count) {
  for (const delay of SESSION_RETRY_MS.slice(0, count)) {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(delay + 10);
    });
  }
}

beforeEach(() => { api.apiGet.mockReset(); vi.useFakeTimers({ shouldAdvanceTime: true }); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('useSessionRestore', () => {
  it('restores a valid session and hands the identity over', async () => {
    api.apiGet.mockResolvedValue({ authenticated: true, role: 'agent' });
    const onIdentity = vi.fn();
    const { result } = renderHook(() => useSessionRestore({ onIdentity }));
    await waitFor(() => expect(result.current.authed).toBe(true));
    expect(onIdentity).toHaveBeenCalledWith({ authenticated: true, role: 'agent' });
  });

  it('signs out only on an authentication answer', async () => {
    api.apiGet.mockRejectedValue(fail(401));
    const { result } = renderHook(() => useSessionRestore());
    await waitFor(() => expect(result.current.authed).toBe(false));
    expect(api.apiGet).toHaveBeenCalledTimes(1);
  });

  it('retries a 429 instead of showing the sign-in form', async () => {
    api.apiGet.mockRejectedValueOnce(fail(429)).mockResolvedValue({ authenticated: true });
    const { result } = renderHook(() => useSessionRestore());
    await runRetries(1);
    await waitFor(() => expect(result.current.authed).toBe(true));
    expect(result.current.unreachable).toBe(false);
  });

  it('reports unreachable after the retries, never signed out, and can try again', async () => {
    api.apiGet.mockRejectedValue(fail(503));
    const { result } = renderHook(() => useSessionRestore());
    await runRetries(SESSION_RETRY_MS.length);
    await waitFor(() => expect(result.current.unreachable).toBe(true));
    expect(result.current.authed).toBe(null);
    api.apiGet.mockResolvedValue({ authenticated: true });
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.authed).toBe(true));
  });
});
