// @vitest-environment jsdom
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock('../lib/apiClient', () => ({ ...api, ApiError: class ApiError extends Error {} }));

const { useSubscription } = await import('./useSubscription');

afterEach(() => { vi.clearAllMocks(); });

// /billing/status says whether the brokerage ever held an entitled
// subscription, so BillingOverlay can tell 'lapsed' (records to read and
// export) from 'never paid' when the newest subscription is a new, unpaid
// checkout (AC18.07, F18.07).
describe('useSubscription history', () => {
  it('passes on that the brokerage was entitled before', async () => {
    api.apiGet.mockResolvedValueOnce({ active: false, status: 'incomplete', plan: 'none', ever_entitled: true });
    const { result } = renderHook(() => useSubscription());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.status).toBe('incomplete');
    expect(result.current.everEntitled).toBe(true);
  });

  it('reads anything but an explicit true as never entitled', async () => {
    api.apiGet.mockResolvedValueOnce({ active: false, status: 'incomplete', plan: 'none' });
    const { result } = renderHook(() => useSubscription());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.everEntitled).toBe(false);
  });
});
