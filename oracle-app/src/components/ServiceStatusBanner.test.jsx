// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
const { ServiceStatusBanner, STATUS_UNKNOWN, UNKNOWN_AFTER_FAILURES } = await import('./ServiceStatusBanner');

afterEach(cleanup);

describe('ServiceStatusBanner', () => {
  it('shows the product-language messages the server sends', async () => {
    api.crmGet.mockResolvedValue({ state: 'DEGRADED', messages: ['Listing data may be out of date.'] });
    render(<ServiceStatusBanner />);
    expect(await screen.findByText('Listing data may be out of date.')).toBeTruthy();
  });

  it('marks itself so the shell can place it below the fixed header', async () => {
    api.crmGet.mockResolvedValue({ state: 'DEGRADED', messages: ['Some emails are waiting to send.'] });
    render(<ServiceStatusBanner />);
    const banner = await screen.findByRole('status');
    expect(banner.hasAttribute('data-service-banner')).toBe(true);
  });

  it('renders nothing when everything is healthy', async () => {
    api.crmGet.mockResolvedValue({ state: 'HEALTHY', messages: [] });
    const { container } = render(<ServiceStatusBanner />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe('');
  });

  it('stays out of the way if the status call itself fails', async () => {
    api.crmGet.mockRejectedValue(new Error('503'));
    const { container } = render(<ServiceStatusBanner />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe('');
  });

  it('says the status cannot be checked after repeated failures, and clears on success (UX-001)', async () => {
    vi.useFakeTimers();
    try {
      api.crmGet.mockRejectedValue(new Error('503'));
      const { container } = render(<ServiceStatusBanner />);
      await act(async () => { await Promise.resolve(); });
      expect(container.textContent).toBe('');                     // one blip: quiet
      for (let i = 1; i < UNKNOWN_AFTER_FAILURES; i += 1) {
        await act(async () => { vi.advanceTimersByTime(75_000); await Promise.resolve(); });
      }
      expect(container.textContent).toBe(STATUS_UNKNOWN);
      api.crmGet.mockResolvedValue({ state: 'HEALTHY', messages: [] });
      await act(async () => { vi.advanceTimersByTime(75_000); await Promise.resolve(); });
      expect(container.textContent).toBe('');
    } finally {
      vi.useRealTimers();
    }
  });
});
